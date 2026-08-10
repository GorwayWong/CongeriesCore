"""Generic deny-by-default Scope authorization contracts."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, require_utc
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import PrincipalId, ResourceId, RunId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.scope import ScopeRef

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


class CorePrincipalKind(StrEnum):
    RUN = "run"
    AGENT = "agent"
    WORKFLOW_NODE = "workflow_node"
    PLUGIN = "plugin"
    CORE_SERVICE = "core_service"


@dataclass(frozen=True, slots=True)
class RuntimePrincipal:
    namespace: str
    kind: str
    id: PrincipalId

    def __post_init__(self) -> None:
        _require_name(self.namespace, "principal namespace")
        _require_name(self.kind, "principal kind")

    @classmethod
    def core(cls, kind: CorePrincipalKind, id: PrincipalId) -> RuntimePrincipal:
        return cls("core", kind.value, id)

    def to_data(self) -> dict[str, str]:
        return {"namespace": self.namespace, "kind": self.kind, "id": self.id.value}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> RuntimePrincipal:
        return cls(
            namespace=str(data["namespace"]),
            kind=str(data["kind"]),
            id=PrincipalId(str(data["id"])),
        )


@dataclass(frozen=True, slots=True)
class ActionRef:
    namespace: str
    name: str
    version: str

    def __post_init__(self) -> None:
        _require_name(self.namespace, "action namespace")
        _require_name(self.name, "action name")
        if not self.version or self.version != self.version.strip():
            raise ValueError("action version must be non-empty and trimmed")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.namespace, self.name, self.version

    def to_data(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ActionRef:
        return cls(str(data["namespace"]), str(data["name"]), str(data["version"]))


@dataclass(frozen=True, slots=True)
class ResourceRef:
    namespace: str
    kind: str
    id: ResourceId
    owning_extension: str | None = None

    def __post_init__(self) -> None:
        _require_name(self.namespace, "resource namespace")
        _require_name(self.kind, "resource kind")
        if self.owning_extension is not None:
            _require_name(self.owning_extension, "owning extension")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.namespace, self.kind, self.id.value

    def to_data(self) -> dict[str, str | None]:
        return {
            "namespace": self.namespace,
            "kind": self.kind,
            "id": self.id.value,
            "owning_extension": self.owning_extension,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ResourceRef:
        owning_extension = data.get("owning_extension")
        return cls(
            namespace=str(data["namespace"]),
            kind=str(data["kind"]),
            id=ResourceId(str(data["id"])),
            owning_extension=(
                str(owning_extension) if owning_extension is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class AccessRequest:
    principal: RuntimePrincipal
    action: ActionRef
    resource: ResourceRef
    scope: ScopeRef
    context: RuntimeCallContext
    constraints: Mapping[str, JsonValue] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "constraints", MappingProxyType(dict(self.constraints))
        )


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class Grant:
    principal: RuntimePrincipal
    action: ActionRef
    resource: ResourceRef
    source_scope: ScopeRef
    effective_scope: ScopeRef
    constraints: Mapping[str, JsonValue]
    issued_at: datetime
    expires_at: datetime | None
    policy_version: str
    audit_correlation: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "constraints", MappingProxyType(dict(self.constraints))
        )
        object.__setattr__(self, "issued_at", require_utc(self.issued_at, "issued_at"))
        if self.expires_at:
            object.__setattr__(
                self, "expires_at", require_utc(self.expires_at, "expires_at")
            )
            if self.expires_at <= self.issued_at:
                raise ValueError("grant expiration must be after issue time")
        if not self.policy_version or not self.audit_correlation:
            raise ValueError("grant policy version and audit correlation are required")

    @property
    def cross_scope(self) -> bool:
        return self.source_scope.key != self.effective_scope.key


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    grant: Grant | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.effect is PolicyEffect.ALLOW and self.grant is None:
            raise ValueError("ALLOW decision requires a grant")
        if self.effect is not PolicyEffect.ALLOW and self.grant is not None:
            raise ValueError("only ALLOW decision may carry a grant")
        if self.effect is not PolicyEffect.ALLOW and not self.reason_code:
            raise ValueError("DENY or INDETERMINATE decision requires reason_code")

    @classmethod
    def allow(cls, grant: Grant) -> PolicyDecision:
        return cls(PolicyEffect.ALLOW, grant=grant)

    @classmethod
    def deny(cls, reason_code: str) -> PolicyDecision:
        return cls(PolicyEffect.DENY, reason_code=reason_code)

    @classmethod
    def indeterminate(cls, reason_code: str) -> PolicyDecision:
        return cls(PolicyEffect.INDETERMINATE, reason_code=reason_code)


class AuthorizationPolicy(Protocol):
    async def authorize(self, request: AccessRequest) -> PolicyDecision: ...


class DenyAllPolicy:
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        del request
        return PolicyDecision.deny("default_deny")


class ActionRegistry:
    def __init__(self, actions: Collection[ActionRef] = ()) -> None:
        self._actions = {action.key: action for action in actions}

    def register(self, action: ActionRef) -> None:
        existing = self._actions.get(action.key)
        if existing is not None:
            raise core_error(
                ErrorCategory.CONFLICT,
                "action_already_registered",
                "action is already registered",
            )
        self._actions[action.key] = action

    def contains(self, action: ActionRef) -> bool:
        return action.key in self._actions


class AuthorizationAuditPublisher(Protocol):
    async def authorization_denied(
        self, request: AccessRequest, decision: PolicyDecision
    ) -> None: ...

    async def cross_scope_granted(
        self, request: AccessRequest, grant: Grant
    ) -> None: ...


class AuditFailureHandler(Protocol):
    async def handle(self, run_id: RunId, error: ErrorDetail) -> None: ...


@dataclass(frozen=True, slots=True)
class AuthorizedCall:
    request: AccessRequest
    grant: Grant
    context: RuntimeCallContext


type AuthorizedOperation[ResultT] = Callable[[AuthorizedCall], Awaitable[ResultT]]


class AuthorizedDispatcher[ResultT]:
    """The single Core gateway for protected capability invocation."""

    def __init__(
        self,
        *,
        action_registry: ActionRegistry,
        audit_publisher: AuthorizationAuditPublisher,
        audit_failure_handler: AuditFailureHandler,
        clock: Clock,
        policy: AuthorizationPolicy | None = None,
    ) -> None:
        self._actions = action_registry
        self._audit = audit_publisher
        self._audit_failure = audit_failure_handler
        self._clock = clock
        self._policy = policy

    async def dispatch(
        self, request: AccessRequest, operation: AuthorizedOperation[ResultT]
    ) -> ResultT:
        request.context.check_active(self._clock)
        if not self._actions.contains(request.action):
            decision = PolicyDecision.deny("unknown_action")
            await self._deny(request, decision)
        elif self._policy is None:
            decision = PolicyDecision.indeterminate("policy_missing")
            await self._deny(request, decision)
        else:
            decision = await self._policy.authorize(request)
            if decision.effect is not PolicyEffect.ALLOW:
                await self._deny(request, decision)

        if decision.grant is None:
            raise AssertionError("ALLOW decision must have a grant")
        grant = decision.grant
        self._validate_grant(request, grant)
        if grant.expires_at and self._clock.now() >= grant.expires_at:
            await self._deny(request, PolicyDecision.deny("grant_expired"))
        if grant.cross_scope:
            try:
                await self._audit.cross_scope_granted(request, grant)
            except CoreError as error:
                await self._audit_failure.handle(request.context.run_id, error.detail)
                raise
        authorized_context = replace(request.context, scope=grant.effective_scope)
        return await operation(AuthorizedCall(request, grant, authorized_context))

    async def _deny(self, request: AccessRequest, decision: PolicyDecision) -> None:
        try:
            await self._audit.authorization_denied(request, decision)
        except CoreError as error:
            await self._audit_failure.handle(request.context.run_id, error.detail)
            raise
        raise core_error(
            ErrorCategory.DENIED,
            decision.reason_code or "authorization_denied",
            "authorization denied",
        )

    def _validate_grant(self, request: AccessRequest, grant: Grant) -> None:
        if grant.principal != request.principal:
            self._invalid_grant("grant principal does not match request")
        if grant.action != request.action:
            self._invalid_grant("grant action does not match request")
        if grant.resource != request.resource:
            self._invalid_grant("grant resource does not match request")
        if grant.source_scope.key != request.context.scope.key:
            self._invalid_grant("grant source Scope does not match call context")
        if grant.effective_scope.key != request.scope.key:
            self._invalid_grant("grant effective Scope does not match request")
        if not grant.cross_scope:
            grant.effective_scope.require_narrower_than(request.context.scope)

    def _invalid_grant(self, message: str) -> None:
        raise core_error(
            ErrorCategory.DENIED,
            "invalid_grant",
            message,
        )


def _require_name(value: str, field_name: str) -> None:
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")
