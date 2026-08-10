"""Deterministic fake collaborators for Provider and Agent contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRegistry,
    AuthorizedDispatcher,
    Grant,
    PolicyDecision,
)
from congeries_core.provider import provider_actions
from congeries_core.provider.context import (
    ContextCapabilities,
    ContextProvider,
    ContextRequest,
    ContextResult,
)
from congeries_core.provider.model import (
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelSelector,
)
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.json_types import JsonValue

from .support import NOW, FixedClock


@dataclass(slots=True)
class RecordingPolicy:
    constraints: Mapping[str, JsonValue] = field(default_factory=dict)
    denied_actions: set[str] = field(default_factory=set)
    requests: list[AccessRequest] = field(default_factory=list)

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        if request.action.name in self.denied_actions:
            return PolicyDecision.deny("test_denied")
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints=self.constraints,
                issued_at=NOW,
                expires_at=None,
                policy_version="provider-test-1",
                audit_correlation="provider-audit",
            )
        )


@dataclass(slots=True)
class AuditRecorder:
    denied: list[AccessRequest] = field(default_factory=list)
    cross_scope: list[AccessRequest] = field(default_factory=list)
    fail: bool = False

    async def authorization_denied(
        self, request: AccessRequest, decision: PolicyDecision
    ) -> None:
        del decision
        self.denied.append(request)
        if self.fail:
            from congeries_core.runtime.errors import ErrorCategory, core_error

            raise core_error(ErrorCategory.UNAVAILABLE, "audit_failed", "audit failed")

    async def cross_scope_granted(self, request: AccessRequest, grant: Grant) -> None:
        del grant
        self.cross_scope.append(request)


@dataclass(slots=True)
class FailureRecorder:
    errors: list[ErrorDetail] = field(default_factory=list)

    async def handle(self, run_id: object, error: ErrorDetail) -> None:
        del run_id
        self.errors.append(error)


def authorized_dispatcher(
    policy: RecordingPolicy | None = None,
    *,
    audit: AuditRecorder | None = None,
    failures: FailureRecorder | None = None,
    known_actions: bool = True,
) -> AuthorizedDispatcher[object]:
    return AuthorizedDispatcher(
        action_registry=ActionRegistry(provider_actions() if known_actions else ()),
        audit_publisher=audit or AuditRecorder(),
        audit_failure_handler=failures or FailureRecorder(),
        clock=FixedClock(),
        policy=policy,
    )


@dataclass(slots=True)
class ProviderEventRecorder:
    events: list[tuple[str, Mapping[str, JsonValue]]] = field(default_factory=list)
    fail: bool = False

    async def provider_event(
        self, event_type: str, context: object, payload: Mapping[str, JsonValue]
    ) -> None:
        del context
        if self.fail:
            raise RuntimeError("observability unavailable")
        self.events.append((event_type, dict(payload)))


@dataclass(slots=True)
class FakeContextProvider(ContextProvider):
    declared_capabilities: ContextCapabilities
    result: ContextResult | Exception
    capability_calls: int = 0
    provide_calls: list[ContextRequest] = field(default_factory=list)

    async def capabilities(self, context: object) -> ContextCapabilities:
        del context
        self.capability_calls += 1
        return self.declared_capabilities

    async def provide(self, request: ContextRequest) -> ContextResult:
        self.provide_calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass(slots=True)
class FakeModelProvider(ModelProvider):
    declared_capabilities: ModelCapabilities
    response: ModelResponse | Exception
    stream_events: tuple[ModelEvent, ...]
    capability_calls: int = 0
    generate_calls: list[ModelRequest] = field(default_factory=list)
    stream_calls: list[ModelRequest] = field(default_factory=list)
    stream_closed: bool = False

    async def capabilities(
        self, selector: ModelSelector, context: object
    ) -> ModelCapabilities:
        del selector, context
        self.capability_calls += 1
        return self.declared_capabilities

    async def generate(self, request: ModelRequest, context: object) -> ModelResponse:
        del context
        self.generate_calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def stream(
        self, request: ModelRequest, context: object
    ) -> AsyncIterator[ModelEvent]:
        del context
        self.stream_calls.append(request)
        try:
            for event in self.stream_events:
                yield event
        finally:
            self.stream_closed = True


class AlternateFakeModelProvider(FakeModelProvider):
    """A second implementation used by the shared ModelProvider contract suite."""

    async def capabilities(
        self, selector: ModelSelector, context: object
    ) -> ModelCapabilities:
        del selector, context
        self.capability_calls += 1
        return self.declared_capabilities

    async def generate(self, request: ModelRequest, context: object) -> ModelResponse:
        del context
        self.generate_calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def stream(
        self, request: ModelRequest, context: object
    ) -> AsyncIterator[ModelEvent]:
        del context
        self.stream_calls.append(request)
        try:
            for event in self.stream_events:
                yield event
        finally:
            self.stream_closed = True


class StringObjectValidator:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), str):
            raise ValueError("expected an object with a string value")


class SumMergePolicy:
    def merge(self, values: tuple[JsonValue, ...]) -> JsonValue:
        if not all(isinstance(value, int) for value in values):
            raise ValueError("integer values required")
        return sum(value for value in values if isinstance(value, int))


def constrained_policy(
    base: RecordingPolicy, constraints: Mapping[str, JsonValue]
) -> RecordingPolicy:
    return replace(base, constraints=constraints)
