"""Authorized ContextProvider contracts and deterministic resolution."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, require_utc
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import (
    DefinitionId,
    PrincipalId,
    ProviderId,
    ResourceId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from ._control import await_provider
from .events import NullProviderEventPublisher, ProviderEventPublisher

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")

CONTEXT_CAPABILITIES_ACTION = ActionRef("core", "context.capabilities", "1")
CONTEXT_PROVIDE_ACTION = ActionRef("core", "context.provide", "1")

CONTEXT_RESOLUTION_STARTED = "core.context.resolution_started"
CONTEXT_PROVIDER_SELECTED = "core.context.provider_selected"
CONTEXT_RESOLUTION_COMPLETED = "core.context.resolution_completed"
CONTEXT_RESOLUTION_FAILED = "core.context.resolution_failed"


def context_actions() -> tuple[ActionRef, ActionRef]:
    return CONTEXT_CAPABILITIES_ACTION, CONTEXT_PROVIDE_ACTION


@dataclass(frozen=True, slots=True, order=True)
class ContextKey:
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.namespace):
            raise ValueError("context key namespace is invalid")
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("context key name is invalid")

    @property
    def wire_name(self) -> str:
        return f"{self.namespace}:{self.name}"

    def to_data(self) -> dict[str, str]:
        return {"namespace": self.namespace, "name": self.name}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextKey:
        return cls(str(data["namespace"]), str(data["name"]))


class ContextCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ContextCompletenessPolicy(StrEnum):
    REQUIRE_COMPLETE = "require_complete"
    ALLOW_PARTIAL = "allow_partial"


class ContextMergeStrategy(StrEnum):
    SINGLE = "single"
    FIRST_SUCCESS = "first_success"
    MERGE = "merge"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_bytes: int | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_bytes", self.max_bytes),
            ("max_tokens", self.max_tokens),
        ):
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ValueError(f"context {name} must be positive")

    def to_data(self) -> dict[str, int | None]:
        return {"max_bytes": self.max_bytes, "max_tokens": self.max_tokens}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextBudget:
        raw_bytes = data.get("max_bytes")
        raw_tokens = data.get("max_tokens")
        return cls(
            max_bytes=as_int(raw_bytes, "max_bytes") if raw_bytes is not None else None,
            max_tokens=(
                as_int(raw_tokens, "max_tokens") if raw_tokens is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextUsage:
    byte_count: int
    token_count: int | None = None

    def __post_init__(self) -> None:
        if self.byte_count < 0 or (
            self.token_count is not None and self.token_count < 0
        ):
            raise ValueError("context usage cannot be negative")

    def to_data(self) -> dict[str, int | None]:
        return {"byte_count": self.byte_count, "token_count": self.token_count}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextUsage:
        raw_tokens = data.get("token_count")
        return cls(
            as_int(data["byte_count"], "byte_count"),
            as_int(raw_tokens, "token_count") if raw_tokens is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ContextRequirement:
    key: ContextKey
    schema: SchemaRef

    def to_data(self) -> dict[str, object]:
        return {"key": self.key.to_data(), "schema": self.schema.to_data()}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextRequirement:
        return cls(
            ContextKey.from_data(as_object(data["key"], "context key")),
            SchemaRef.from_data(as_object(data["schema"], "context schema")),
        )


@dataclass(frozen=True, slots=True)
class ContextEntry:
    key: ContextKey
    schema: SchemaRef
    value: JsonValue
    provenance: tuple[str, ...]
    fresh_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", as_json_value(self.value, "context entry"))
        if not self.provenance or any(
            not item or item != item.strip() for item in self.provenance
        ):
            raise ValueError("context entry requires trimmed provenance references")
        if self.fresh_at is not None:
            object.__setattr__(
                self, "fresh_at", require_utc(self.fresh_at, "context fresh_at")
            )
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", require_utc(self.expires_at, "context expires_at")
            )
        if self.fresh_at and self.expires_at and self.expires_at <= self.fresh_at:
            raise ValueError("context expiration must be after freshness time")

    def to_data(self) -> dict[str, object]:
        return {
            "key": self.key.to_data(),
            "schema": self.schema.to_data(),
            "value": self.value,
            "provenance": list(self.provenance),
            "fresh_at": self.fresh_at.isoformat() if self.fresh_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextEntry:
        raw_fresh = data.get("fresh_at")
        raw_expires = data.get("expires_at")
        return cls(
            key=ContextKey.from_data(as_object(data["key"], "context key")),
            schema=SchemaRef.from_data(as_object(data["schema"], "context schema")),
            value=as_json_value(data.get("value"), "context entry"),
            provenance=tuple(
                str(item) for item in as_array(data["provenance"], "context provenance")
            ),
            fresh_at=(datetime.fromisoformat(str(raw_fresh)) if raw_fresh else None),
            expires_at=(
                datetime.fromisoformat(str(raw_expires)) if raw_expires else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextWarning:
    code: str
    message: str
    key: ContextKey | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("context warning code and message are required")

    def to_data(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "key": self.key.to_data() if self.key else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextWarning:
        raw_key = data.get("key")
        return cls(
            code=str(data["code"]),
            message=str(data["message"]),
            key=(
                ContextKey.from_data(as_object(raw_key, "context warning key"))
                if raw_key is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextRequest:
    definition_id: DefinitionId
    context: RuntimeCallContext
    requirements: tuple[ContextRequirement, ...]
    completeness_policy: ContextCompletenessPolicy
    budget: ContextBudget = field(default_factory=ContextBudget)

    def __post_init__(self) -> None:
        keys = tuple(item.key for item in self.requirements)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("context requirements must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class ContextResult:
    provider_id: ProviderId
    contract_version: str
    entries: tuple[ContextEntry, ...]
    completeness: ContextCompleteness
    missing_keys: tuple[ContextKey, ...] = field(default_factory=tuple)
    warnings: tuple[ContextWarning, ...] = field(default_factory=tuple)
    usage: ContextUsage = field(default_factory=lambda: ContextUsage(0))

    def __post_init__(self) -> None:
        if not self.contract_version:
            raise ValueError("context provider contract version is required")
        if self.completeness is ContextCompleteness.COMPLETE and self.missing_keys:
            raise ValueError("complete context cannot report missing keys")
        if self.completeness is ContextCompleteness.PARTIAL and not self.missing_keys:
            raise ValueError("partial context must report missing keys")

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "entries": [entry.to_data() for entry in self.entries],
            "completeness": self.completeness.value,
            "missing_keys": [key.to_data() for key in self.missing_keys],
            "warnings": [warning.to_data() for warning in self.warnings],
            "usage": self.usage.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextResult:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            contract_version=str(data["contract_version"]),
            entries=tuple(
                ContextEntry.from_data(as_object(item, "context entry"))
                for item in as_array(data["entries"], "context entries")
            ),
            completeness=ContextCompleteness(str(data["completeness"])),
            missing_keys=tuple(
                ContextKey.from_data(as_object(item, "context missing key"))
                for item in as_array(data["missing_keys"], "context missing keys")
            ),
            warnings=tuple(
                ContextWarning.from_data(as_object(item, "context warning"))
                for item in as_array(data["warnings"], "context warnings")
            ),
            usage=ContextUsage.from_data(as_object(data["usage"], "context usage")),
        )


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    entries: tuple[ContextEntry, ...]
    completeness: ContextCompleteness
    missing_keys: tuple[ContextKey, ...]
    warnings: tuple[ContextWarning, ...]
    selected_providers: tuple[ProviderId, ...]
    usage: ContextUsage

    def __post_init__(self) -> None:
        if not self.selected_providers:
            raise ValueError("resolved context requires at least one provider")
        if self.completeness is ContextCompleteness.COMPLETE and self.missing_keys:
            raise ValueError("complete resolved context cannot report missing keys")
        if self.completeness is ContextCompleteness.PARTIAL and not self.missing_keys:
            raise ValueError("partial resolved context must report missing keys")


@dataclass(frozen=True, slots=True)
class ContextScopePattern:
    namespace: str
    kind: str

    def matches(self, context: RuntimeCallContext) -> bool:
        return (
            context.scope.namespace == self.namespace
            and context.scope.kind == self.kind
        )


@dataclass(frozen=True, slots=True)
class ContextCapabilities:
    provider_id: ProviderId
    contract_version: str
    supported: tuple[ContextRequirement, ...]
    supports_partial: bool
    maximum_budget: ContextBudget = field(default_factory=ContextBudget)
    scope_patterns: tuple[ContextScopePattern, ...] = field(default_factory=tuple)

    def supports(
        self, requirement: ContextRequirement, request: ContextRequest
    ) -> bool:
        if requirement not in self.supported:
            return False
        if self.scope_patterns and not any(
            pattern.matches(request.context) for pattern in self.scope_patterns
        ):
            return False
        if (
            request.budget.max_bytes is not None
            and self.maximum_budget.max_bytes is not None
            and request.budget.max_bytes > self.maximum_budget.max_bytes
        ):
            return False
        return not (
            request.budget.max_tokens is not None
            and self.maximum_budget.max_tokens is not None
            and request.budget.max_tokens > self.maximum_budget.max_tokens
        )

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "supported": [item.to_data() for item in self.supported],
            "supports_partial": self.supports_partial,
            "maximum_budget": self.maximum_budget.to_data(),
            "scope_patterns": [
                {"namespace": item.namespace, "kind": item.kind}
                for item in self.scope_patterns
            ],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextCapabilities:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            contract_version=str(data["contract_version"]),
            supported=tuple(
                ContextRequirement.from_data(as_object(item, "context requirement"))
                for item in as_array(data["supported"], "supported context")
            ),
            supports_partial=bool(data["supports_partial"]),
            maximum_budget=ContextBudget.from_data(
                as_object(data["maximum_budget"], "maximum context budget")
            ),
            scope_patterns=tuple(
                ContextScopePattern(
                    str(as_object(item, "context scope pattern")["namespace"]),
                    str(as_object(item, "context scope pattern")["kind"]),
                )
                for item in as_array(data["scope_patterns"], "context scope patterns")
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextBinding:
    provider_ids: tuple[ProviderId, ...]
    requirements: tuple[ContextRequirement, ...]
    merge_strategy: ContextMergeStrategy = ContextMergeStrategy.SINGLE
    completeness_policy: ContextCompletenessPolicy = (
        ContextCompletenessPolicy.REQUIRE_COMPLETE
    )
    budget: ContextBudget = field(default_factory=ContextBudget)

    def __post_init__(self) -> None:
        if not self.provider_ids or len(set(self.provider_ids)) != len(
            self.provider_ids
        ):
            raise ValueError("context provider references must be non-empty and unique")
        keys = tuple(item.key for item in self.requirements)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError(
                "context binding requirements must be non-empty and unique"
            )

    def request(
        self, definition_id: DefinitionId, context: RuntimeCallContext
    ) -> ContextRequest:
        return ContextRequest(
            definition_id=definition_id,
            context=context,
            requirements=self.requirements,
            completeness_policy=self.completeness_policy,
            budget=self.budget,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "provider_ids": [item.value for item in self.provider_ids],
            "requirements": [item.to_data() for item in self.requirements],
            "merge_strategy": self.merge_strategy.value,
            "completeness_policy": self.completeness_policy.value,
            "budget": self.budget.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextBinding:
        return cls(
            provider_ids=tuple(
                ProviderId(str(item))
                for item in as_array(data["provider_ids"], "context provider ids")
            ),
            requirements=tuple(
                ContextRequirement.from_data(as_object(item, "context requirement"))
                for item in as_array(data["requirements"], "context requirements")
            ),
            merge_strategy=ContextMergeStrategy(str(data["merge_strategy"])),
            completeness_policy=ContextCompletenessPolicy(
                str(data["completeness_policy"])
            ),
            budget=ContextBudget.from_data(as_object(data["budget"], "context budget")),
        )


class ContextProvider(Protocol):
    async def provide(self, request: ContextRequest) -> ContextResult: ...

    async def capabilities(
        self, context: RuntimeCallContext
    ) -> ContextCapabilities: ...


class ContextMergePolicy(Protocol):
    def merge(self, values: tuple[JsonValue, ...]) -> JsonValue: ...


class ContextMergeRegistry:
    def __init__(self) -> None:
        self._policies: dict[tuple[str, str, str], ContextMergePolicy] = {}

    def register(self, schema: SchemaRef, policy: ContextMergePolicy) -> None:
        if schema.key in self._policies:
            raise core_error(
                ErrorCategory.CONFLICT,
                "context_merge_policy_already_registered",
                "context merge policy is already registered",
            )
        self._policies[schema.key] = policy

    def merge(self, schema: SchemaRef, values: tuple[JsonValue, ...]) -> JsonValue:
        if all(value == values[0] for value in values[1:]):
            return values[0]
        policy = self._policies.get(schema.key)
        if policy is None:
            raise core_error(
                ErrorCategory.CONFLICT,
                "context_merge_conflict",
                "context values conflict without an explicit merge policy",
            )
        try:
            return as_json_value(policy.merge(values), "merged context value")
        except CoreError:
            raise
        except (TypeError, ValueError) as error:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "context_merge_policy_failure",
                "context merge policy returned an invalid value",
                cause_id=type(error).__name__,
            ) from error


class ContextProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderId, ContextProvider] = {}

    def register(self, provider_id: ProviderId, provider: ContextProvider) -> None:
        if provider_id in self._providers:
            raise core_error(
                ErrorCategory.CONFLICT,
                "context_provider_already_registered",
                "context provider is already registered",
            )
        self._providers[provider_id] = provider

    def get(self, provider_id: ProviderId) -> ContextProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "context_provider_not_registered",
                "context provider is not registered",
                retryable=True,
            )
        return provider


class ContextResolver:
    def __init__(
        self,
        *,
        providers: ContextProviderRegistry,
        schemas: SchemaRegistry,
        merges: ContextMergeRegistry,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._providers = providers
        self._schemas = schemas
        self._merges = merges
        self._dispatcher = dispatcher
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def resolve(
        self, request: ContextRequest, binding: ContextBinding
    ) -> ResolvedContext:
        if request.requirements != binding.requirements:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "context_binding_mismatch",
                "context request does not match its binding",
            )
        started_at = self._clock.now()
        await self._emit(
            CONTEXT_RESOLUTION_STARTED,
            request.context,
            {
                "key_count": len(request.requirements),
                "strategy": binding.merge_strategy.value,
            },
        )
        try:
            capabilities = await self._load_capabilities(request, binding.provider_ids)
            groups = self._select(request, binding, capabilities)
            results = await self._invoke_groups(request, binding, groups)
            resolved = self._combine(request, binding, results)
            await self._emit(
                CONTEXT_RESOLUTION_COMPLETED,
                request.context,
                {
                    "provider_count": len(resolved.selected_providers),
                    "entry_count": len(resolved.entries),
                    "completeness": resolved.completeness.value,
                    "byte_count": resolved.usage.byte_count,
                    "latency_ms": self._elapsed_ms(started_at),
                    "outcome": "completed",
                },
            )
            return resolved
        except CoreError as error:
            await self._emit(
                CONTEXT_RESOLUTION_FAILED,
                request.context,
                {
                    "error_code": error.detail.code,
                    "category": error.detail.category.value,
                    "latency_ms": self._elapsed_ms(started_at),
                    "outcome": "failed",
                },
            )
            raise

    async def _load_capabilities(
        self, request: ContextRequest, provider_ids: tuple[ProviderId, ...]
    ) -> dict[ProviderId, ContextCapabilities]:
        result: dict[ProviderId, ContextCapabilities] = {}
        for provider_id in provider_ids:
            provider = self._providers.get(provider_id)
            access = self._access_request(
                request.context,
                provider_id,
                CONTEXT_CAPABILITIES_ACTION,
                {
                    "keys": [item.key.wire_name for item in request.requirements],
                    "max_bytes": request.budget.max_bytes,
                    "max_tokens": request.budget.max_tokens,
                },
            )

            raw = await self._dispatcher.dispatch(
                access, self._capability_operation(provider, request)
            )
            if not isinstance(raw, ContextCapabilities):
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "invalid_context_capabilities",
                    "context provider returned invalid capabilities",
                )
            capabilities = raw
            if capabilities.provider_id != provider_id:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "context_provider_identity_mismatch",
                    "context provider returned a mismatched identity",
                )
            result[provider_id] = capabilities
        return result

    def _select(
        self,
        request: ContextRequest,
        binding: ContextBinding,
        capabilities: Mapping[ProviderId, ContextCapabilities],
    ) -> tuple[tuple[ProviderId, tuple[ContextRequirement, ...]], ...]:
        if binding.merge_strategy is ContextMergeStrategy.FIRST_SUCCESS:
            first_groups: list[tuple[ProviderId, tuple[ContextRequirement, ...]]] = []
            for provider_id in binding.provider_ids:
                supported = tuple(
                    item
                    for item in request.requirements
                    if capabilities[provider_id].supports(item, request)
                )
                if supported:
                    first_groups.append((provider_id, supported))
            if not first_groups:
                self._unavailable()
            return tuple(first_groups)

        assignments: dict[ProviderId, list[ContextRequirement]] = {}
        for requirement in request.requirements:
            matching = tuple(
                provider_id
                for provider_id in binding.provider_ids
                if capabilities[provider_id].supports(requirement, request)
            )
            if (
                binding.merge_strategy is ContextMergeStrategy.SINGLE
                and len(matching) > 1
            ):
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "context_single_provider_conflict",
                    "single resolution matched more than one provider for a key",
                )
            for provider_id in matching:
                assignments.setdefault(provider_id, []).append(requirement)
        groups = tuple(
            (provider_id, tuple(assignments[provider_id]))
            for provider_id in binding.provider_ids
            if provider_id in assignments
        )
        if not groups:
            self._unavailable()
        return groups

    async def _invoke_groups(
        self,
        request: ContextRequest,
        binding: ContextBinding,
        groups: tuple[tuple[ProviderId, tuple[ContextRequirement, ...]], ...],
    ) -> tuple[ContextResult, ...]:
        results: list[ContextResult] = []
        for provider_id, requirements in groups:
            try:
                result = await self._invoke(request, provider_id, requirements)
            except CoreError as error:
                if (
                    binding.merge_strategy is ContextMergeStrategy.FIRST_SUCCESS
                    and error.detail.category
                    in {ErrorCategory.UNAVAILABLE, ErrorCategory.UNSUPPORTED_CAPABILITY}
                ):
                    continue
                raise
            results.append(result)
            if binding.merge_strategy is ContextMergeStrategy.FIRST_SUCCESS and (
                result.completeness is ContextCompleteness.COMPLETE
                or request.completeness_policy
                is ContextCompletenessPolicy.ALLOW_PARTIAL
            ):
                break
        if not results:
            self._unavailable()
        if binding.merge_strategy is ContextMergeStrategy.FIRST_SUCCESS:
            results.sort(key=lambda item: len(item.entries), reverse=True)
            return (results[0],)
        return tuple(results)

    async def _invoke(
        self,
        request: ContextRequest,
        provider_id: ProviderId,
        requirements: tuple[ContextRequirement, ...],
    ) -> ContextResult:
        provider = self._providers.get(provider_id)
        provider_request = replace(request, requirements=requirements)
        access = self._access_request(
            request.context,
            provider_id,
            CONTEXT_PROVIDE_ACTION,
            {
                "keys": [item.key.wire_name for item in requirements],
                "max_bytes": request.budget.max_bytes,
                "max_tokens": request.budget.max_tokens,
            },
        )
        await self._emit(
            CONTEXT_PROVIDER_SELECTED,
            request.context,
            {"provider_id": provider_id.value, "key_count": len(requirements)},
        )

        async def operation(call: AuthorizedCall) -> object:
            constrained = self._constrain_request(provider_request, call)
            return await self._provider_result(provider, constrained)

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, ContextResult):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "invalid_context_result",
                "context provider returned an invalid result",
            )
        result = raw
        self._validate_provider_result(provider_id, provider_request, result)
        return result

    async def _provider_capabilities(
        self, provider: ContextProvider, context: RuntimeCallContext
    ) -> ContextCapabilities:
        try:
            return await await_provider(
                provider.capabilities(context), context, self._clock
            )
        except CoreError:
            raise
        except Exception as error:
            raise self._provider_failure(error) from error

    def _capability_operation(
        self, provider: ContextProvider, request: ContextRequest
    ) -> Callable[[AuthorizedCall], Awaitable[object]]:
        async def operation(call: AuthorizedCall) -> object:
            constrained = self._constrain_request(request, call)
            return await self._provider_capabilities(provider, constrained.context)

        return operation

    async def _provider_result(
        self, provider: ContextProvider, request: ContextRequest
    ) -> ContextResult:
        try:
            return await await_provider(
                provider.provide(request), request.context, self._clock
            )
        except CoreError:
            raise
        except Exception as error:
            raise self._provider_failure(error) from error

    def _validate_provider_result(
        self,
        provider_id: ProviderId,
        request: ContextRequest,
        result: ContextResult,
    ) -> None:
        if result.provider_id != provider_id:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "context_provider_identity_mismatch",
                "context provider returned a mismatched identity",
            )
        required = {item.key: item.schema for item in request.requirements}
        seen: set[ContextKey] = set()
        for entry in result.entries:
            expected = required.get(entry.key)
            if expected is None or expected != entry.schema or entry.key in seen:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "invalid_context_result",
                    "context provider returned an unexpected or duplicate entry",
                )
            self._schemas.validate(entry.schema, entry.value)
            seen.add(entry.key)
        actual_missing = tuple(
            item.key for item in request.requirements if item.key not in seen
        )
        if set(actual_missing) != set(result.missing_keys):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "invalid_context_completeness",
                "context provider completeness does not match returned entries",
            )

    def _combine(
        self,
        request: ContextRequest,
        binding: ContextBinding,
        results: tuple[ContextResult, ...],
    ) -> ResolvedContext:
        by_key: dict[ContextKey, list[ContextEntry]] = {}
        warnings: list[ContextWarning] = []
        for result in results:
            warnings.extend(result.warnings)
            for entry in result.entries:
                by_key.setdefault(entry.key, []).append(entry)
        entries: list[ContextEntry] = []
        for requirement in request.requirements:
            candidates = by_key.get(requirement.key, [])
            if binding.merge_strategy is ContextMergeStrategy.ALL:
                entries.extend(candidates)
            elif candidates:
                values = tuple(item.value for item in candidates)
                merged = self._merges.merge(requirement.schema, values)
                provenance = tuple(
                    reference for item in candidates for reference in item.provenance
                )
                entries.append(
                    replace(candidates[0], value=merged, provenance=provenance)
                )
        present = {entry.key for entry in entries}
        missing = tuple(
            item.key for item in request.requirements if item.key not in present
        )
        completeness = (
            ContextCompleteness.PARTIAL if missing else ContextCompleteness.COMPLETE
        )
        encoded = json.dumps(
            [entry.value for entry in entries],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        token_counts = tuple(result.usage.token_count for result in results)
        token_count = (
            sum(cast(int, value) for value in token_counts)
            if all(value is not None for value in token_counts)
            else None
        )
        usage = ContextUsage(len(encoded), token_count)
        self._enforce_budget(request.budget, usage)
        return ResolvedContext(
            entries=tuple(entries),
            completeness=completeness,
            missing_keys=missing,
            warnings=tuple(warnings),
            selected_providers=tuple(result.provider_id for result in results),
            usage=usage,
        )

    def _constrain_request(
        self, request: ContextRequest, call: AuthorizedCall
    ) -> ContextRequest:
        constraints = call.grant.constraints
        allowed = {"keys", "max_bytes", "max_tokens"}
        if set(constraints).difference(allowed):
            self._invalid_grant("context grant contains unknown constraints")
        requirements = request.requirements
        raw_keys = constraints.get("keys")
        if raw_keys is not None:
            if not isinstance(raw_keys, list) or not all(
                isinstance(item, str) for item in raw_keys
            ):
                self._invalid_grant("context key constraint is invalid")
            key_names = cast(list[str], raw_keys)
            requested = {item.key.wire_name for item in requirements}
            if not set(key_names).issubset(requested):
                self._invalid_grant("context grant broadens requested keys")
            requirements = tuple(
                item for item in requirements if item.key.wire_name in key_names
            )
            if not requirements:
                self._invalid_grant("context grant permits no requested keys")
        budget = ContextBudget(
            max_bytes=self._narrow_limit(
                "max_bytes", request.budget.max_bytes, constraints.get("max_bytes")
            ),
            max_tokens=self._narrow_limit(
                "max_tokens", request.budget.max_tokens, constraints.get("max_tokens")
            ),
        )
        return replace(
            request, context=call.context, requirements=requirements, budget=budget
        )

    def _narrow_limit(
        self, name: str, requested: int | None, raw_granted: JsonValue | None
    ) -> int | None:
        if raw_granted is None:
            return requested
        if isinstance(raw_granted, bool) or not isinstance(raw_granted, int):
            self._invalid_grant(f"context {name} constraint is invalid")
        granted = cast(int, raw_granted)
        if granted < 1 or (requested is not None and granted > requested):
            self._invalid_grant(f"context {name} grant broadens the request")
        return granted

    def _enforce_budget(self, budget: ContextBudget, usage: ContextUsage) -> None:
        if budget.max_bytes is not None and usage.byte_count > budget.max_bytes:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "context_byte_budget_exceeded",
                "resolved context exceeds its byte budget",
            )
        if budget.max_tokens is not None:
            if usage.token_count is None:
                raise core_error(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "context_token_usage_unavailable",
                    "token budget requires provider token usage reporting",
                )
            if usage.token_count > budget.max_tokens:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "context_token_budget_exceeded",
                    "resolved context exceeds its token budget",
                )

    def _access_request(
        self,
        context: RuntimeCallContext,
        provider_id: ProviderId,
        action: ActionRef,
        constraints: Mapping[str, JsonValue],
    ) -> AccessRequest:
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core", "context_provider", ResourceId(provider_id.value)
            ),
            scope=context.scope,
            context=context,
            constraints=constraints,
        )

    async def _emit(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with suppress(Exception):
            await self._events.provider_event(event_type, context, payload)

    def _provider_failure(self, error: Exception) -> CoreError:
        return core_error(
            ErrorCategory.UNAVAILABLE,
            "context_provider_failure",
            "context provider failed",
            retryable=True,
            cause_id=type(error).__name__,
        )

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((self._clock.now() - started_at).total_seconds() * 1_000))

    def _invalid_grant(self, message: str) -> None:
        raise core_error(ErrorCategory.DENIED, "invalid_grant", message)

    def _unavailable(self) -> None:
        raise core_error(
            ErrorCategory.UNAVAILABLE,
            "compatible_context_provider_unavailable",
            "no compatible context provider is available",
            retryable=True,
        )
