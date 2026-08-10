"""Provider-neutral MemoryProvider contracts and authorized gateway."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
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
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import (
    IdempotencyKey,
    MemoryId,
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
from congeries_core.runtime.scope import ScopeRef

from ._control import await_provider
from .events import NullProviderEventPublisher, ProviderEventPublisher

MEMORY_CAPABILITIES_ACTION = ActionRef("core", "memory.capabilities", "1")
MEMORY_RETRIEVE_ACTION = ActionRef("core", "memory.retrieve", "1")
MEMORY_REMEMBER_ACTION = ActionRef("core", "memory.remember", "1")
MEMORY_FORGET_ACTION = ActionRef("core", "memory.forget", "1")
MEMORY_CONSOLIDATE_ACTION = ActionRef("core", "memory.consolidate", "1")

MEMORY_OPERATION_STARTED = "core.memory.operation_started"
MEMORY_OPERATION_COMPLETED = "core.memory.operation_completed"
MEMORY_OPERATION_FAILED = "core.memory.operation_failed"


def memory_actions() -> tuple[ActionRef, ActionRef, ActionRef, ActionRef, ActionRef]:
    return (
        MEMORY_CAPABILITIES_ACTION,
        MEMORY_RETRIEVE_ACTION,
        MEMORY_REMEMBER_ACTION,
        MEMORY_FORGET_ACTION,
        MEMORY_CONSOLIDATE_ACTION,
    )


class MemoryOperation(StrEnum):
    RETRIEVE = "retrieve"
    REMEMBER = "remember"
    FORGET = "forget"
    CONSOLIDATE = "consolidate"


class MemoryCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ForgetOutcome(StrEnum):
    DELETED = "deleted"
    ALREADY_ABSENT = "already_absent"


class ConsolidationOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class MemoryWarning:
    code: str
    message: str

    def __post_init__(self) -> None:
        _require_text(self.code, "memory warning code")
        _require_text(self.message, "memory warning message")

    def to_data(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryWarning:
        return cls(str(data["code"]), str(data["message"]))


@dataclass(frozen=True, slots=True)
class MemoryCursor:
    provider_id: ProviderId
    contract_version: str
    value: str
    query_fingerprint: str

    def __post_init__(self) -> None:
        _require_text(self.contract_version, "memory cursor contract version")
        _require_text(self.value, "memory cursor value")
        if len(self.query_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_fingerprint
        ):
            raise ValueError(
                "memory cursor query fingerprint must be lowercase SHA-256"
            )

    def to_data(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "value": self.value,
            "query_fingerprint": self.query_fingerprint,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryCursor:
        return cls(
            ProviderId(str(data["provider_id"])),
            str(data["contract_version"]),
            str(data["value"]),
            str(data["query_fingerprint"]),
        )


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    scope: ScopeRef
    content: ContentBlock
    schema: SchemaRef
    filters: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    limit: int = 50
    cursor: MemoryCursor | None = None
    projection: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or self.limit < 1:
            raise ValueError("memory query limit must be positive")
        object.__setattr__(self, "filters", _freeze_mapping(self.filters, "filters"))
        _require_unique_names(self.projection, "memory query projection")

    @property
    def query_fingerprint(self) -> str:
        encoded = json.dumps(
            self._fingerprint_data(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _fingerprint_data(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_data(),
            "content": self.content.to_data(),
            "schema": self.schema.to_data(),
            "filters": dict(self.filters),
            "limit": self.limit,
            "projection": list(self.projection),
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self._fingerprint_data(),
            "cursor": self.cursor.to_data() if self.cursor else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryQuery:
        raw_cursor = data.get("cursor")
        return cls(
            scope=ScopeRef.from_data(as_object(data["scope"], "memory query scope")),
            content=ContentBlock.from_data(
                as_object(data["content"], "memory query content")
            ),
            schema=SchemaRef.from_data(
                as_object(data["schema"], "memory query schema")
            ),
            filters=_json_mapping(data.get("filters", {}), "memory query filters"),
            limit=as_int(data["limit"], "memory query limit"),
            cursor=(
                MemoryCursor.from_data(as_object(raw_cursor, "memory cursor"))
                if raw_cursor is not None
                else None
            ),
            projection=tuple(
                str(item)
                for item in as_array(data.get("projection", []), "memory projection")
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryRef:
    provider_id: ProviderId
    memory_id: MemoryId
    scope: ScopeRef
    version: int

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("memory version must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "memory_id": self.memory_id.value,
            "scope": self.scope.to_data(),
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryRef:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            memory_id=MemoryId(str(data["memory_id"])),
            scope=ScopeRef.from_data(
                as_object(data["scope"], "memory reference scope")
            ),
            version=as_int(data["version"], "memory version"),
        )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    ref: MemoryRef
    content: ContentBlock
    schema: SchemaRef
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    provenance: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        _require_unique_names(self.provenance, "memory record provenance")

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_data(),
            "content": self.content.to_data(),
            "schema": self.schema.to_data(),
            "metadata": dict(self.metadata),
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryRecord:
        return cls(
            ref=MemoryRef.from_data(as_object(data["ref"], "memory record reference")),
            content=ContentBlock.from_data(
                as_object(data["content"], "memory record content")
            ),
            schema=SchemaRef.from_data(
                as_object(data["schema"], "memory record schema")
            ),
            metadata=_json_mapping(data.get("metadata", {}), "memory record metadata"),
            provenance=tuple(
                str(item)
                for item in as_array(data.get("provenance", []), "memory provenance")
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryPage:
    provider_id: ProviderId
    contract_version: str
    records: tuple[MemoryRecord, ...]
    next_cursor: MemoryCursor | None = None
    completeness: MemoryCompleteness = MemoryCompleteness.COMPLETE
    warnings: tuple[MemoryWarning, ...] = field(default_factory=tuple)
    provenance: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.contract_version, "memory page contract version")
        identities = tuple(record.ref.memory_id for record in self.records)
        if len(set(identities)) != len(identities):
            raise ValueError("memory page record identities must be unique")
        _require_unique_names(self.provenance, "memory page provenance")

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "records": [record.to_data() for record in self.records],
            "next_cursor": self.next_cursor.to_data() if self.next_cursor else None,
            "completeness": self.completeness.value,
            "warnings": [warning.to_data() for warning in self.warnings],
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryPage:
        raw_cursor = data.get("next_cursor")
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            contract_version=str(data["contract_version"]),
            records=tuple(
                MemoryRecord.from_data(as_object(item, "memory record"))
                for item in as_array(data["records"], "memory records")
            ),
            next_cursor=(
                MemoryCursor.from_data(as_object(raw_cursor, "memory cursor"))
                if raw_cursor is not None
                else None
            ),
            completeness=MemoryCompleteness(str(data["completeness"])),
            warnings=tuple(
                MemoryWarning.from_data(as_object(item, "memory warning"))
                for item in as_array(data.get("warnings", []), "memory warnings")
            ),
            provenance=tuple(
                str(item)
                for item in as_array(data.get("provenance", []), "memory provenance")
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryItem:
    scope: ScopeRef
    content: ContentBlock
    schema: SchemaRef
    idempotency_key: IdempotencyKey
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    provenance: tuple[str, ...] = field(default_factory=tuple)
    retention: ResourceRef | None = None
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        _require_unique_names(self.provenance, "memory item provenance")
        if self.max_bytes is not None and (
            isinstance(self.max_bytes, bool) or self.max_bytes < 1
        ):
            raise ValueError("memory item max_bytes must be positive")
        if self.max_bytes is not None and self.content_bytes > self.max_bytes:
            raise ValueError("memory item content exceeds max_bytes")

    @property
    def content_bytes(self) -> int:
        return len(
            json.dumps(
                self.content.to_data(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )

    def to_data(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_data(),
            "content": self.content.to_data(),
            "schema": self.schema.to_data(),
            "idempotency_key": self.idempotency_key.value,
            "metadata": dict(self.metadata),
            "provenance": list(self.provenance),
            "retention": self.retention.to_data() if self.retention else None,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryItem:
        raw_retention = data.get("retention")
        raw_max = data.get("max_bytes")
        return cls(
            scope=ScopeRef.from_data(as_object(data["scope"], "memory item scope")),
            content=ContentBlock.from_data(
                as_object(data["content"], "memory item content")
            ),
            schema=SchemaRef.from_data(as_object(data["schema"], "memory item schema")),
            idempotency_key=IdempotencyKey(str(data["idempotency_key"])),
            metadata=_json_mapping(data.get("metadata", {}), "memory item metadata"),
            provenance=tuple(
                str(item)
                for item in as_array(data.get("provenance", []), "memory provenance")
            ),
            retention=(
                ResourceRef.from_data(as_object(raw_retention, "memory retention"))
                if raw_retention is not None
                else None
            ),
            max_bytes=(
                as_int(raw_max, "memory item max_bytes")
                if raw_max is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    ref: MemoryRef
    expected_scope: ScopeRef
    expected_version: int

    def __post_init__(self) -> None:
        if self.expected_scope.key != self.ref.scope.key:
            raise ValueError("forget expected Scope must match MemoryRef Scope")
        if isinstance(self.expected_version, bool) or self.expected_version < 1:
            raise ValueError("forget expected version must be positive")
        if self.expected_version != self.ref.version:
            raise ValueError("forget expected version must match MemoryRef version")

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_data(),
            "expected_scope": self.expected_scope.to_data(),
            "expected_version": self.expected_version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ForgetRequest:
        return cls(
            ref=MemoryRef.from_data(as_object(data["ref"], "forget reference")),
            expected_scope=ScopeRef.from_data(
                as_object(data["expected_scope"], "forget expected Scope")
            ),
            expected_version=as_int(data["expected_version"], "expected version"),
        )


@dataclass(frozen=True, slots=True)
class ForgetResult:
    ref: MemoryRef
    outcome: ForgetOutcome

    def to_data(self) -> dict[str, object]:
        return {"ref": self.ref.to_data(), "outcome": self.outcome.value}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ForgetResult:
        return cls(
            MemoryRef.from_data(as_object(data["ref"], "forget result reference")),
            ForgetOutcome(str(data["outcome"])),
        )


@dataclass(frozen=True, slots=True)
class ConsolidateRequest:
    scope: ScopeRef
    policy: ResourceRef
    selection: Mapping[str, JsonValue] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selection", _freeze_mapping(self.selection, "selection")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_data(),
            "policy": self.policy.to_data(),
            "selection": dict(self.selection),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ConsolidateRequest:
        return cls(
            ScopeRef.from_data(as_object(data["scope"], "consolidation Scope")),
            ResourceRef.from_data(as_object(data["policy"], "consolidation policy")),
            _json_mapping(data.get("selection", {}), "consolidation selection"),
        )


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    provider_id: ProviderId
    contract_version: str
    policy: ResourceRef
    scope: ScopeRef
    affected: tuple[MemoryRef, ...]
    skipped: tuple[MemoryRef, ...]
    outcome: ConsolidationOutcome
    warnings: tuple[MemoryWarning, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.contract_version, "consolidation contract version")
        identities = [item.memory_id for item in (*self.affected, *self.skipped)]
        if len(set(identities)) != len(identities):
            raise ValueError("consolidation references must be unique")

    @property
    def success(self) -> bool:
        return self.outcome in {
            ConsolidationOutcome.COMPLETE,
            ConsolidationOutcome.NOOP,
        }

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "policy": self.policy.to_data(),
            "scope": self.scope.to_data(),
            "affected": [item.to_data() for item in self.affected],
            "skipped": [item.to_data() for item in self.skipped],
            "outcome": self.outcome.value,
            "warnings": [warning.to_data() for warning in self.warnings],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ConsolidationReport:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            contract_version=str(data["contract_version"]),
            policy=ResourceRef.from_data(
                as_object(data["policy"], "consolidation policy")
            ),
            scope=ScopeRef.from_data(as_object(data["scope"], "consolidation Scope")),
            affected=tuple(
                MemoryRef.from_data(as_object(item, "affected memory reference"))
                for item in as_array(data["affected"], "affected references")
            ),
            skipped=tuple(
                MemoryRef.from_data(as_object(item, "skipped memory reference"))
                for item in as_array(data["skipped"], "skipped references")
            ),
            outcome=ConsolidationOutcome(str(data["outcome"])),
            warnings=tuple(
                MemoryWarning.from_data(as_object(item, "memory warning"))
                for item in as_array(data.get("warnings", []), "memory warnings")
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryCapabilities:
    provider_id: ProviderId
    contract_version: str
    operations: frozenset[MemoryOperation]
    query_schemas: frozenset[SchemaRef]
    item_schemas: frozenset[SchemaRef]
    record_schemas: frozenset[SchemaRef]
    content_kinds: frozenset[ContentKind]
    maximum_result_limit: int
    projections: frozenset[str]
    versioned: bool
    consolidation_policies: tuple[ResourceRef, ...] = field(default_factory=tuple)
    maximum_item_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.contract_version, "memory provider contract version")
        if isinstance(self.maximum_result_limit, bool) or self.maximum_result_limit < 1:
            raise ValueError("memory maximum result limit must be positive")
        if self.maximum_item_bytes is not None and (
            isinstance(self.maximum_item_bytes, bool) or self.maximum_item_bytes < 1
        ):
            raise ValueError("memory maximum item bytes must be positive")
        _require_unique_names(tuple(self.projections), "memory projections")
        policy_keys = tuple(item.key for item in self.consolidation_policies)
        if len(set(policy_keys)) != len(policy_keys):
            raise ValueError("memory consolidation policies must be unique")
        if (
            MemoryOperation.CONSOLIDATE not in self.operations
            and self.consolidation_policies
        ):
            raise ValueError(
                "memory consolidation policies require consolidate support"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "contract_version": self.contract_version,
            "operations": sorted(item.value for item in self.operations),
            "query_schemas": [item.to_data() for item in sorted(self.query_schemas)],
            "item_schemas": [item.to_data() for item in sorted(self.item_schemas)],
            "record_schemas": [item.to_data() for item in sorted(self.record_schemas)],
            "content_kinds": sorted(item.value for item in self.content_kinds),
            "maximum_result_limit": self.maximum_result_limit,
            "projections": sorted(self.projections),
            "versioned": self.versioned,
            "consolidation_policies": [
                item.to_data() for item in self.consolidation_policies
            ],
            "maximum_item_bytes": self.maximum_item_bytes,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> MemoryCapabilities:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            contract_version=str(data["contract_version"]),
            operations=frozenset(
                MemoryOperation(str(item))
                for item in as_array(data["operations"], "memory operations")
            ),
            query_schemas=frozenset(
                SchemaRef.from_data(as_object(item, "memory query schema"))
                for item in as_array(data["query_schemas"], "memory query schemas")
            ),
            item_schemas=frozenset(
                SchemaRef.from_data(as_object(item, "memory item schema"))
                for item in as_array(data["item_schemas"], "memory item schemas")
            ),
            record_schemas=frozenset(
                SchemaRef.from_data(as_object(item, "memory record schema"))
                for item in as_array(data["record_schemas"], "memory record schemas")
            ),
            content_kinds=frozenset(
                ContentKind(str(item))
                for item in as_array(data["content_kinds"], "memory content kinds")
            ),
            maximum_result_limit=as_int(
                data["maximum_result_limit"], "memory maximum result limit"
            ),
            projections=frozenset(
                str(item)
                for item in as_array(data["projections"], "memory projections")
            ),
            versioned=bool(data["versioned"]),
            consolidation_policies=tuple(
                ResourceRef.from_data(as_object(item, "consolidation policy"))
                for item in as_array(
                    data.get("consolidation_policies", []),
                    "memory consolidation policies",
                )
            ),
            maximum_item_bytes=(
                as_int(data["maximum_item_bytes"], "memory maximum item bytes")
                if data.get("maximum_item_bytes") is not None
                else None
            ),
        )


class MemoryProvider(Protocol):
    async def retrieve(
        self, query: MemoryQuery, context: RuntimeCallContext
    ) -> MemoryPage: ...

    async def remember(
        self, item: MemoryItem, context: RuntimeCallContext
    ) -> MemoryRef: ...

    async def forget(
        self, request: ForgetRequest, context: RuntimeCallContext
    ) -> ForgetResult: ...

    async def consolidate(
        self, request: ConsolidateRequest, context: RuntimeCallContext
    ) -> ConsolidationReport: ...

    async def capabilities(self, context: RuntimeCallContext) -> MemoryCapabilities: ...


class MemoryProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderId, MemoryProvider] = {}

    def register(self, provider_id: ProviderId, provider: MemoryProvider) -> None:
        if provider_id in self._providers:
            raise core_error(
                ErrorCategory.CONFLICT,
                "memory_provider_already_registered",
                "memory provider is already registered",
            )
        self._providers[provider_id] = provider

    def get(self, provider_id: ProviderId) -> MemoryProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "memory_provider_not_registered",
                "memory provider is not registered",
                retryable=True,
            )
        return provider


class MemoryGateway:
    def __init__(
        self,
        *,
        providers: MemoryProviderRegistry,
        schemas: SchemaRegistry,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._providers = providers
        self._schemas = schemas
        self._dispatcher = dispatcher
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def capabilities(
        self, provider_id: ProviderId, context: RuntimeCallContext
    ) -> MemoryCapabilities:
        return await self._discover(provider_id, context, context.scope)

    async def retrieve(
        self,
        provider_id: ProviderId,
        query: MemoryQuery,
        context: RuntimeCallContext,
    ) -> MemoryPage:
        query.scope.require_narrower_than(context.scope)
        self._prevalidate_cursor(provider_id, query)
        started_at = self._clock.now()
        await self._emit_started(MemoryOperation.RETRIEVE, provider_id, context)
        try:
            capabilities = await self._discover(provider_id, context, query.scope)
            self._validate_query(query, capabilities)
            provider = self._providers.get(provider_id)
            effective_query = query
            access = self._access_request(
                provider_id,
                context,
                query.scope,
                MEMORY_RETRIEVE_ACTION,
                self._retrieve_constraints(query),
            )

            async def operation(call: AuthorizedCall) -> object:
                nonlocal effective_query
                effective_query = self._constrain_query(query, call)
                if (
                    effective_query.cursor is not None
                    and effective_query.cursor.query_fingerprint
                    != effective_query.query_fingerprint
                ):
                    raise core_error(
                        ErrorCategory.INVALID_REQUEST,
                        "memory_cursor_query_drift",
                        "memory cursor cannot be reused with a changed query",
                    )
                self._schemas.validate(
                    effective_query.schema, effective_query.content.value
                )
                try:
                    return await await_provider(
                        provider.retrieve(effective_query, call.context),
                        call.context,
                        self._clock,
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise self._provider_failure(error) from error

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, MemoryPage):
                self._protocol_failure("memory provider returned an invalid page")
            page = cast(MemoryPage, raw)
            self._validate_page(provider_id, effective_query, capabilities, page)
            await self._emit_completed(
                MemoryOperation.RETRIEVE,
                provider_id,
                context,
                started_at,
                record_count=len(page.records),
                outcome=page.completeness.value,
            )
            return page
        except CoreError as error:
            await self._emit_failure(
                MemoryOperation.RETRIEVE, provider_id, context, started_at, error.detail
            )
            raise

    async def remember(
        self,
        provider_id: ProviderId,
        item: MemoryItem,
        context: RuntimeCallContext,
    ) -> MemoryRef:
        item.scope.require_narrower_than(context.scope)
        if (
            context.idempotency_key is None
            or item.idempotency_key != context.idempotency_key
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "memory_idempotency_key_mismatch",
                "memory item idempotency key must match RuntimeCallContext",
            )
        started_at = self._clock.now()
        await self._emit_started(MemoryOperation.REMEMBER, provider_id, context)
        try:
            capabilities = await self._discover(provider_id, context, item.scope)
            self._validate_item(item, capabilities)
            provider = self._providers.get(provider_id)
            access = self._access_request(
                provider_id,
                context,
                item.scope,
                MEMORY_REMEMBER_ACTION,
                self._remember_constraints(item, capabilities),
            )

            async def operation(call: AuthorizedCall) -> object:
                constrained = self._constrain_item(item, capabilities, call)
                self._schemas.validate(constrained.schema, constrained.content.value)
                try:
                    return await await_provider(
                        provider.remember(constrained, call.context),
                        call.context,
                        self._clock,
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise self._provider_failure(error) from error

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, MemoryRef):
                self._protocol_failure("memory provider returned an invalid reference")
            ref = cast(MemoryRef, raw)
            if ref.provider_id != provider_id or ref.scope.key != item.scope.key:
                self._protocol_failure("remember returned a mismatched MemoryRef")
            await self._emit_completed(
                MemoryOperation.REMEMBER,
                provider_id,
                context,
                started_at,
                affected_count=1,
                outcome="remembered",
            )
            return ref
        except CoreError as error:
            await self._emit_failure(
                MemoryOperation.REMEMBER, provider_id, context, started_at, error.detail
            )
            raise

    async def forget(
        self,
        provider_id: ProviderId,
        request: ForgetRequest,
        context: RuntimeCallContext,
    ) -> ForgetResult:
        request.expected_scope.require_narrower_than(context.scope)
        if request.ref.provider_id != provider_id:
            self._protocol_failure("forget Provider identity does not match MemoryRef")
        started_at = self._clock.now()
        await self._emit_started(MemoryOperation.FORGET, provider_id, context)
        try:
            capabilities = await self._discover(
                provider_id, context, request.expected_scope
            )
            self._require_operation(capabilities, MemoryOperation.FORGET)
            if not capabilities.versioned:
                raise core_error(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "memory_versioning_unsupported",
                    "memory provider does not support versioned forget",
                )
            provider = self._providers.get(provider_id)
            access = self._access_request(
                provider_id,
                context,
                request.expected_scope,
                MEMORY_FORGET_ACTION,
                {
                    "memory_id": request.ref.memory_id.value,
                    "expected_version": request.expected_version,
                },
            )

            async def operation(call: AuthorizedCall) -> object:
                self._validate_forget_grant(request, call)
                try:
                    return await await_provider(
                        provider.forget(request, call.context),
                        call.context,
                        self._clock,
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise self._provider_failure(error) from error

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, ForgetResult):
                self._protocol_failure(
                    "memory provider returned an invalid forget result"
                )
            result = cast(ForgetResult, raw)
            if result.ref != request.ref:
                self._protocol_failure("forget result does not match MemoryRef")
            await self._emit_completed(
                MemoryOperation.FORGET,
                provider_id,
                context,
                started_at,
                affected_count=(1 if result.outcome is ForgetOutcome.DELETED else 0),
                outcome=result.outcome.value,
            )
            return result
        except CoreError as error:
            await self._emit_failure(
                MemoryOperation.FORGET, provider_id, context, started_at, error.detail
            )
            raise

    async def consolidate(
        self,
        provider_id: ProviderId,
        request: ConsolidateRequest,
        context: RuntimeCallContext,
    ) -> ConsolidationReport:
        request.scope.require_narrower_than(context.scope)
        started_at = self._clock.now()
        await self._emit_started(MemoryOperation.CONSOLIDATE, provider_id, context)
        try:
            capabilities = await self._discover(provider_id, context, request.scope)
            self._require_operation(capabilities, MemoryOperation.CONSOLIDATE)
            if request.policy.key not in {
                policy.key for policy in capabilities.consolidation_policies
            }:
                raise core_error(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "memory_consolidation_policy_unsupported",
                    "memory consolidation policy is not supported",
                )
            provider = self._providers.get(provider_id)
            constraints: Mapping[str, JsonValue] = {
                "policy": _resource_name(request.policy),
                "selection_keys": _json_string_list(sorted(request.selection)),
            }
            access = self._access_request(
                provider_id,
                context,
                request.scope,
                MEMORY_CONSOLIDATE_ACTION,
                constraints,
            )

            async def operation(call: AuthorizedCall) -> object:
                self._validate_consolidate_grant(request, call)
                try:
                    return await await_provider(
                        provider.consolidate(request, call.context),
                        call.context,
                        self._clock,
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise self._provider_failure(error) from error

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, ConsolidationReport):
                self._protocol_failure(
                    "memory provider returned an invalid consolidation report"
                )
            report = cast(ConsolidationReport, raw)
            self._validate_report(provider_id, request, capabilities, report)
            await self._emit_completed(
                MemoryOperation.CONSOLIDATE,
                provider_id,
                context,
                started_at,
                affected_count=len(report.affected),
                outcome=report.outcome.value,
            )
            return report
        except CoreError as error:
            await self._emit_failure(
                MemoryOperation.CONSOLIDATE,
                provider_id,
                context,
                started_at,
                error.detail,
            )
            raise

    async def _discover(
        self,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        scope: ScopeRef,
    ) -> MemoryCapabilities:
        scope.require_narrower_than(context.scope)
        provider = self._providers.get(provider_id)
        access = self._access_request(
            provider_id, context, scope, MEMORY_CAPABILITIES_ACTION, {}
        )

        async def operation(call: AuthorizedCall) -> object:
            if call.grant.constraints:
                self._invalid_grant(
                    "memory capability grant contains unknown constraints"
                )
            try:
                return await await_provider(
                    provider.capabilities(call.context), call.context, self._clock
                )
            except CoreError:
                raise
            except Exception as error:
                raise self._provider_failure(error) from error

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, MemoryCapabilities):
            self._protocol_failure("memory provider returned invalid capabilities")
        capabilities = cast(MemoryCapabilities, raw)
        if capabilities.provider_id != provider_id:
            self._protocol_failure("memory capabilities returned a mismatched Provider")
        return capabilities

    def _prevalidate_cursor(self, provider_id: ProviderId, query: MemoryQuery) -> None:
        if query.cursor is None:
            return
        if query.cursor.provider_id != provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "memory_cursor_provider_mismatch",
                "memory cursor Provider does not match the request",
            )

    def _validate_query(
        self, query: MemoryQuery, capabilities: MemoryCapabilities
    ) -> None:
        self._require_operation(capabilities, MemoryOperation.RETRIEVE)
        if query.schema not in capabilities.query_schemas:
            self._unsupported("memory query schema is not supported")
        if query.content.kind not in capabilities.content_kinds:
            self._unsupported("memory query content kind is not supported")
        if query.limit > capabilities.maximum_result_limit:
            self._unsupported("memory query limit exceeds Provider capability")
        if not set(query.projection).issubset(capabilities.projections):
            self._unsupported("memory query projection is not supported")
        if (
            query.cursor is not None
            and query.cursor.contract_version != capabilities.contract_version
        ):
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "memory_cursor_version_mismatch",
                "memory cursor contract version does not match Provider",
            )
        self._schemas.validate(query.schema, query.content.value)

    def _validate_item(
        self, item: MemoryItem, capabilities: MemoryCapabilities
    ) -> None:
        self._require_operation(capabilities, MemoryOperation.REMEMBER)
        if item.schema not in capabilities.item_schemas:
            self._unsupported("memory item schema is not supported")
        if item.content.kind not in capabilities.content_kinds:
            self._unsupported("memory item content kind is not supported")
        if (
            capabilities.maximum_item_bytes is not None
            and item.content_bytes > capabilities.maximum_item_bytes
        ):
            self._unsupported("memory item exceeds Provider byte capability")
        self._schemas.validate(item.schema, item.content.value)

    def _validate_page(
        self,
        provider_id: ProviderId,
        query: MemoryQuery,
        capabilities: MemoryCapabilities,
        page: MemoryPage,
    ) -> None:
        if (
            page.provider_id != provider_id
            or page.contract_version != capabilities.contract_version
            or len(page.records) > query.limit
        ):
            self._protocol_failure("memory page identity, version, or size is invalid")
        for record in page.records:
            if (
                record.ref.provider_id != provider_id
                or record.ref.scope.key != query.scope.key
                or record.schema not in capabilities.record_schemas
                or record.content.kind not in capabilities.content_kinds
            ):
                self._protocol_failure(
                    "memory record identity, Scope, or schema is invalid"
                )
            self._schemas.validate(record.schema, record.content.value)
        if page.next_cursor is not None and (
            page.next_cursor.provider_id != provider_id
            or page.next_cursor.contract_version != capabilities.contract_version
            or page.next_cursor.query_fingerprint != query.query_fingerprint
        ):
            self._protocol_failure("memory page returned an invalid cursor")

    def _validate_report(
        self,
        provider_id: ProviderId,
        request: ConsolidateRequest,
        capabilities: MemoryCapabilities,
        report: ConsolidationReport,
    ) -> None:
        if (
            report.provider_id != provider_id
            or report.contract_version != capabilities.contract_version
            or report.policy != request.policy
            or report.scope.key != request.scope.key
        ):
            self._protocol_failure("consolidation report identity is invalid")
        if any(
            ref.provider_id != provider_id or ref.scope.key != request.scope.key
            for ref in (*report.affected, *report.skipped)
        ):
            self._protocol_failure("consolidation report contains invalid references")

    def _constrain_query(self, query: MemoryQuery, call: AuthorizedCall) -> MemoryQuery:
        constraints = call.grant.constraints
        allowed = {"schema", "filter_keys", "projection", "limit"}
        if set(constraints).difference(allowed):
            self._invalid_grant("memory retrieve grant contains unknown constraints")
        if constraints.get("schema", query.schema.to_data()) != query.schema.to_data():
            self._invalid_grant("memory retrieve grant changes the query schema")
        self._require_exact_keys(
            constraints.get("filter_keys"), query.filters, "filter"
        )
        projection = query.projection
        raw_projection = constraints.get("projection")
        if raw_projection is not None:
            names = self._string_list(raw_projection, "memory projection constraint")
            if not set(names).issubset(query.projection):
                self._invalid_grant("memory retrieve grant broadens projection")
            projection = tuple(name for name in query.projection if name in names)
        limit = self._narrow_limit("limit", query.limit, constraints.get("limit"))
        return replace(query, projection=projection, limit=limit)

    def _constrain_item(
        self,
        item: MemoryItem,
        capabilities: MemoryCapabilities,
        call: AuthorizedCall,
    ) -> MemoryItem:
        constraints = call.grant.constraints
        allowed = {"schema", "metadata_keys", "max_bytes"}
        if set(constraints).difference(allowed):
            self._invalid_grant("memory remember grant contains unknown constraints")
        if constraints.get("schema", item.schema.to_data()) != item.schema.to_data():
            self._invalid_grant("memory remember grant changes the item schema")
        metadata = item.metadata
        raw_keys = constraints.get("metadata_keys")
        if raw_keys is not None:
            names = self._string_list(raw_keys, "memory metadata key constraint")
            if not set(names).issubset(item.metadata):
                self._invalid_grant("memory remember grant broadens metadata keys")
            metadata = {
                key: value for key, value in item.metadata.items() if key in names
            }
        requested_max = item.max_bytes or capabilities.maximum_item_bytes
        max_bytes = self._narrow_optional_limit(
            "max_bytes", requested_max, constraints.get("max_bytes")
        )
        if max_bytes is not None and item.content_bytes > max_bytes:
            self._invalid_grant("memory remember grant is smaller than item content")
        return replace(item, metadata=metadata, max_bytes=max_bytes)

    def _validate_forget_grant(
        self, request: ForgetRequest, call: AuthorizedCall
    ) -> None:
        constraints = call.grant.constraints
        allowed = {"memory_id", "expected_version"}
        if set(constraints).difference(allowed):
            self._invalid_grant("memory forget grant contains unknown constraints")
        if (
            constraints.get("memory_id", request.ref.memory_id.value)
            != request.ref.memory_id.value
        ):
            self._invalid_grant("memory forget grant changes Memory identity")
        if (
            constraints.get("expected_version", request.expected_version)
            != request.expected_version
        ):
            self._invalid_grant("memory forget grant changes expected version")

    def _validate_consolidate_grant(
        self, request: ConsolidateRequest, call: AuthorizedCall
    ) -> None:
        constraints = call.grant.constraints
        allowed = {"policy", "selection_keys"}
        if set(constraints).difference(allowed):
            self._invalid_grant("memory consolidate grant contains unknown constraints")
        if constraints.get("policy", _resource_name(request.policy)) != _resource_name(
            request.policy
        ):
            self._invalid_grant("memory consolidate grant changes policy identity")
        self._require_exact_keys(
            constraints.get("selection_keys"), request.selection, "selection"
        )

    def _retrieve_constraints(self, query: MemoryQuery) -> Mapping[str, JsonValue]:
        return {
            "schema": _schema_data(query.schema),
            "filter_keys": _json_string_list(sorted(query.filters)),
            "projection": _json_string_list(query.projection),
            "limit": query.limit,
        }

    def _remember_constraints(
        self, item: MemoryItem, capabilities: MemoryCapabilities
    ) -> Mapping[str, JsonValue]:
        return {
            "schema": _schema_data(item.schema),
            "metadata_keys": _json_string_list(sorted(item.metadata)),
            "max_bytes": item.max_bytes or capabilities.maximum_item_bytes,
        }

    def _access_request(
        self,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        scope: ScopeRef,
        action: ActionRef,
        constraints: Mapping[str, JsonValue],
    ) -> AccessRequest:
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core", "memory_provider", ResourceId(provider_id.value)
            ),
            scope=scope,
            context=context,
            constraints=constraints,
        )

    async def _emit_started(
        self,
        operation: MemoryOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
    ) -> None:
        await self._emit(
            MEMORY_OPERATION_STARTED,
            context,
            {"operation": operation.value, "provider_id": provider_id.value},
        )

    async def _emit_completed(
        self,
        operation: MemoryOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        started_at: datetime,
        *,
        record_count: int = 0,
        affected_count: int = 0,
        outcome: str,
    ) -> None:
        await self._emit(
            MEMORY_OPERATION_COMPLETED,
            context,
            {
                "operation": operation.value,
                "provider_id": provider_id.value,
                "record_count": record_count,
                "affected_count": affected_count,
                "outcome": outcome,
                "latency_ms": self._elapsed_ms(started_at),
            },
        )

    async def _emit_failure(
        self,
        operation: MemoryOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        started_at: datetime,
        error: ErrorDetail,
    ) -> None:
        await self._emit(
            MEMORY_OPERATION_FAILED,
            context,
            {
                "operation": operation.value,
                "provider_id": provider_id.value,
                "error_code": error.code,
                "category": error.category.value,
                "outcome": "failed",
                "latency_ms": self._elapsed_ms(started_at),
            },
        )

    async def _emit(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with suppress(Exception):
            await self._events.provider_event(event_type, context, payload)

    def _require_operation(
        self, capabilities: MemoryCapabilities, operation: MemoryOperation
    ) -> None:
        if operation not in capabilities.operations:
            self._unsupported(f"memory Provider does not support {operation.value}")

    def _require_exact_keys(
        self,
        raw: JsonValue | None,
        requested: Mapping[str, JsonValue],
        name: str,
    ) -> None:
        if raw is None:
            return
        names = self._string_list(raw, f"memory {name} key constraint")
        if set(names) != set(requested):
            self._invalid_grant(f"memory grant changes {name} keys")

    def _string_list(self, value: JsonValue, name: str) -> list[str]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            self._invalid_grant(f"{name} is invalid")
        return cast(list[str], value)

    def _narrow_limit(self, name: str, requested: int, raw: JsonValue | None) -> int:
        narrowed = self._narrow_optional_limit(name, requested, raw)
        if narrowed is None:
            raise AssertionError("required memory limit cannot be None")
        return narrowed

    def _narrow_optional_limit(
        self, name: str, requested: int | None, raw: JsonValue | None
    ) -> int | None:
        if raw is None:
            return requested
        if isinstance(raw, bool) or not isinstance(raw, int):
            self._invalid_grant(f"memory {name} constraint is invalid")
        granted = cast(int, raw)
        if granted < 1 or (requested is not None and granted > requested):
            self._invalid_grant(f"memory {name} grant broadens the request")
        return granted

    def _provider_failure(self, error: Exception) -> CoreError:
        return core_error(
            ErrorCategory.UNAVAILABLE,
            "memory_provider_failure",
            "memory provider failed",
            retryable=True,
            cause_id=type(error).__name__,
        )

    def _protocol_failure(self, message: str) -> None:
        raise core_error(
            ErrorCategory.PROTOCOL_FAILURE,
            "memory_provider_protocol_failure",
            message,
        )

    def _unsupported(self, message: str) -> None:
        raise core_error(
            ErrorCategory.UNSUPPORTED_CAPABILITY,
            "memory_capability_unsupported",
            message,
        )

    def _invalid_grant(self, message: str) -> None:
        raise core_error(ErrorCategory.DENIED, "invalid_grant", message)

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((self._clock.now() - started_at).total_seconds() * 1_000))


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")


def _require_unique_names(values: tuple[str, ...], name: str) -> None:
    if any(not value or value != value.strip() for value in values):
        raise ValueError(f"{name} values must be non-empty and trimmed")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} values must be unique")


def _freeze_mapping(
    values: Mapping[str, JsonValue], name: str
) -> Mapping[str, JsonValue]:
    normalized = _json_mapping(dict(values), name)
    if any(
        "." not in key or key.startswith(".") or key.endswith(".") for key in normalized
    ):
        raise ValueError(f"memory {name} keys must be namespaced")
    return MappingProxyType(normalized)


def _json_mapping(value: object, name: str) -> dict[str, JsonValue]:
    raw = as_object(value, name)
    return {key: as_json_value(item, name) for key, item in raw.items()}


def _resource_name(resource: ResourceRef) -> str:
    return ":".join(resource.key)


def _schema_data(schema: SchemaRef) -> dict[str, JsonValue]:
    return {
        "namespace": schema.namespace,
        "name": schema.name,
        "version": schema.version,
    }


def _json_string_list(values: tuple[str, ...] | list[str]) -> list[JsonValue]:
    return [value for value in values]
