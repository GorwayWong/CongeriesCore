"""Versioned Workspace and immutable Artifact storage contracts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock
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
from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, require_utc
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import (
    ArtifactId,
    PrincipalId,
    ProviderId,
    ResourceId,
    WorkspaceId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.scope import ScopeRef
from congeries_core.state.workspace import WorkspaceState

from .events import NullProviderEventPublisher, ProviderEventPublisher

STORAGE_CAPABILITIES_ACTION = ActionRef("core", "storage.capabilities", "1")
WORKSPACE_CREATE_ACTION = ActionRef("core", "storage.workspace.create", "1")
WORKSPACE_GET_ACTION = ActionRef("core", "storage.workspace.get", "1")
WORKSPACE_COMPARE_AND_SET_ACTION = ActionRef(
    "core", "storage.workspace.compare_and_set", "1"
)
ARTIFACT_PUT_ACTION = ActionRef("core", "storage.artifact.put", "1")
ARTIFACT_GET_ACTION = ActionRef("core", "storage.artifact.get", "1")
ARTIFACT_LIST_ACTION = ActionRef("core", "storage.artifact.list", "1")

STORAGE_OPERATION_STARTED = "core.storage.operation_started"
STORAGE_OPERATION_COMPLETED = "core.storage.operation_completed"
STORAGE_OPERATION_FAILED = "core.storage.operation_failed"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def storage_actions() -> tuple[ActionRef, ...]:
    return (
        STORAGE_CAPABILITIES_ACTION,
        WORKSPACE_CREATE_ACTION,
        WORKSPACE_GET_ACTION,
        WORKSPACE_COMPARE_AND_SET_ACTION,
        ARTIFACT_PUT_ACTION,
        ARTIFACT_GET_ACTION,
        ARTIFACT_LIST_ACTION,
    )


class StorageOperation(StrEnum):
    CAPABILITIES = "capabilities"
    WORKSPACE_CREATE = "workspace_create"
    WORKSPACE_GET = "workspace_get"
    WORKSPACE_COMPARE_AND_SET = "workspace_compare_and_set"
    ARTIFACT_PUT = "artifact_put"
    ARTIFACT_GET = "artifact_get"
    ARTIFACT_LIST = "artifact_list"


@dataclass(frozen=True, slots=True)
class StorageCapabilities:
    provider_id: ProviderId
    max_artifact_bytes: int
    contract_version: str = "1"
    supports_workspace_cas: bool = True
    supports_artifact_pagination: bool = True

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "storage capabilities")
        if self.max_artifact_bytes < 1:
            raise ValueError("maximum Artifact bytes must be positive")
        if not self.supports_workspace_cas:
            raise ValueError("storage contract version 1 requires Workspace CAS")
        if not self.supports_artifact_pagination:
            raise ValueError("storage contract version 1 requires Artifact pagination")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id.value,
            "max_artifact_bytes": self.max_artifact_bytes,
            "supports_workspace_cas": self.supports_workspace_cas,
            "supports_artifact_pagination": self.supports_artifact_pagination,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> StorageCapabilities:
        _require_keys(
            data,
            {
                "contract_version",
                "provider_id",
                "max_artifact_bytes",
                "supports_workspace_cas",
                "supports_artifact_pagination",
            },
            "storage capabilities",
        )
        return cls(
            contract_version=_version_value(data["contract_version"], "capabilities"),
            provider_id=ProviderId(str(data["provider_id"])),
            max_artifact_bytes=as_int(
                data["max_artifact_bytes"], "maximum Artifact bytes"
            ),
            supports_workspace_cas=_bool_value(
                data["supports_workspace_cas"], "Workspace CAS support"
            ),
            supports_artifact_pagination=_bool_value(
                data["supports_artifact_pagination"], "Artifact pagination support"
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    provider_id: ProviderId
    artifact_id: ArtifactId
    workspace_id: WorkspaceId
    scope: ScopeRef
    media_type: str
    byte_length: int
    sha256: str
    created_at: datetime
    name: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    contract_version: str = "1"

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "Artifact record")
        if not self.media_type or self.media_type != self.media_type.strip():
            raise ValueError("Artifact media type must be non-empty and trimmed")
        if self.byte_length < 0:
            raise ValueError("Artifact byte length must not be negative")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("Artifact SHA-256 must be 64 lowercase hex characters")
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, "Artifact created_at")
        )
        if self.name is not None and (not self.name or self.name != self.name.strip()):
            raise ValueError("Artifact name must be non-empty and trimmed")
        normalized = as_json_value(self.metadata, "Artifact metadata")
        if not isinstance(normalized, dict):
            raise ValueError("Artifact metadata must be an object")
        object.__setattr__(self, "metadata", MappingProxyType(normalized))

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id.value,
            "artifact_id": self.artifact_id.value,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
            "name": self.name,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactRecord:
        _require_keys(
            data,
            {
                "contract_version",
                "provider_id",
                "artifact_id",
                "workspace_id",
                "scope",
                "media_type",
                "byte_length",
                "sha256",
                "created_at",
                "name",
                "metadata",
            },
            "Artifact record",
        )
        name = data["name"]
        return cls(
            contract_version=_version_value(data["contract_version"], "Artifact"),
            provider_id=ProviderId(str(data["provider_id"])),
            artifact_id=ArtifactId(str(data["artifact_id"])),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "Artifact scope")),
            media_type=str(data["media_type"]),
            byte_length=as_int(data["byte_length"], "Artifact byte length"),
            sha256=str(data["sha256"]),
            created_at=_datetime_value(data["created_at"], "Artifact created_at"),
            name=str(name) if name is not None else None,
            metadata={
                key: as_json_value(value, f"Artifact metadata {key}")
                for key, value in as_object(
                    data["metadata"], "Artifact metadata"
                ).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ArtifactValue:
    record: ArtifactRecord
    content: bytes

    def __post_init__(self) -> None:
        content = bytes(self.content)
        object.__setattr__(self, "content", content)
        if len(content) != self.record.byte_length:
            raise ValueError("Artifact content length does not match its record")
        if hashlib.sha256(content).hexdigest() != self.record.sha256:
            raise ValueError("Artifact content digest does not match its record")

    def to_data(self) -> dict[str, object]:
        return {
            "record": self.record.to_data(),
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactValue:
        _require_keys(data, {"record", "content_base64"}, "Artifact value")
        encoded = data["content_base64"]
        if not isinstance(encoded, str):
            raise ValueError("Artifact content_base64 must be a string")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Artifact content_base64 is invalid") from error
        if base64.b64encode(content).decode("ascii") != encoded:
            raise ValueError("Artifact content_base64 is not canonical")
        return cls(
            ArtifactRecord.from_data(as_object(data["record"], "Artifact record")),
            content,
        )


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    provider_id: ProviderId
    artifact_id: ArtifactId
    workspace_id: WorkspaceId
    scope: ScopeRef
    sha256: str
    contract_version: str = "1"

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "Artifact reference")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("Artifact reference SHA-256 is invalid")

    @classmethod
    def from_record(cls, record: ArtifactRecord) -> ArtifactReference:
        return cls(
            record.provider_id,
            record.artifact_id,
            record.workspace_id,
            record.scope,
            record.sha256,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id.value,
            "artifact_id": self.artifact_id.value,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
            "sha256": self.sha256,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactReference:
        _require_keys(
            data,
            {
                "contract_version",
                "provider_id",
                "artifact_id",
                "workspace_id",
                "scope",
                "sha256",
            },
            "Artifact reference",
        )
        return cls(
            contract_version=_version_value(
                data["contract_version"], "Artifact reference"
            ),
            provider_id=ProviderId(str(data["provider_id"])),
            artifact_id=ArtifactId(str(data["artifact_id"])),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            scope=ScopeRef.from_data(
                as_object(data["scope"], "Artifact reference scope")
            ),
            sha256=str(data["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ArtifactCursor:
    provider_id: ProviderId
    workspace_id: WorkspaceId
    scope: ScopeRef
    limit: int
    query_fingerprint: str
    before_created_at: datetime
    before_artifact_id: ArtifactId
    contract_version: str = "1"

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "Artifact cursor")
        if self.limit < 1:
            raise ValueError("Artifact cursor limit must be positive")
        if not _SHA256_PATTERN.fullmatch(self.query_fingerprint):
            raise ValueError("Artifact cursor query fingerprint is invalid")
        object.__setattr__(
            self,
            "before_created_at",
            require_utc(self.before_created_at, "Artifact cursor created_at"),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id.value,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
            "limit": self.limit,
            "query_fingerprint": self.query_fingerprint,
            "before_created_at": self.before_created_at.isoformat(),
            "before_artifact_id": self.before_artifact_id.value,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactCursor:
        _require_keys(
            data,
            {
                "contract_version",
                "provider_id",
                "workspace_id",
                "scope",
                "limit",
                "query_fingerprint",
                "before_created_at",
                "before_artifact_id",
            },
            "Artifact cursor",
        )
        return cls(
            contract_version=_version_value(
                data["contract_version"], "Artifact cursor"
            ),
            provider_id=ProviderId(str(data["provider_id"])),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "Artifact cursor scope")),
            limit=as_int(data["limit"], "Artifact cursor limit"),
            query_fingerprint=str(data["query_fingerprint"]),
            before_created_at=_datetime_value(
                data["before_created_at"], "Artifact cursor created_at"
            ),
            before_artifact_id=ArtifactId(str(data["before_artifact_id"])),
        )


@dataclass(frozen=True, slots=True)
class ArtifactQuery:
    provider_id: ProviderId
    workspace_id: WorkspaceId
    scope: ScopeRef
    limit: int = 100
    cursor: ArtifactCursor | None = None
    contract_version: str = "1"

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "Artifact query")
        if self.limit < 1:
            raise ValueError("Artifact query limit must be positive")
        if self.cursor is not None:
            _validate_cursor(self.cursor, self)

    @property
    def query_fingerprint(self) -> str:
        payload = {
            "provider_id": self.provider_id.value,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id.value,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
            "limit": self.limit,
            "cursor": self.cursor.to_data() if self.cursor else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactQuery:
        _require_keys(
            data,
            {
                "contract_version",
                "provider_id",
                "workspace_id",
                "scope",
                "limit",
                "cursor",
            },
            "Artifact query",
        )
        raw_cursor = data["cursor"]
        return cls(
            contract_version=_version_value(data["contract_version"], "Artifact query"),
            provider_id=ProviderId(str(data["provider_id"])),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "Artifact query scope")),
            limit=as_int(data["limit"], "Artifact query limit"),
            cursor=(
                ArtifactCursor.from_data(as_object(raw_cursor, "Artifact cursor"))
                if raw_cursor is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    items: tuple[ArtifactRecord, ...]
    next_cursor: ArtifactCursor | None
    contract_version: str = "1"

    def __post_init__(self) -> None:
        _require_version(self.contract_version, "Artifact page")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "items": [item.to_data() for item in self.items],
            "next_cursor": self.next_cursor.to_data() if self.next_cursor else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ArtifactPage:
        _require_keys(
            data, {"contract_version", "items", "next_cursor"}, "Artifact page"
        )
        raw_items = data["items"]
        if not isinstance(raw_items, list):
            raise ValueError("Artifact page items must be an array")
        items = cast(list[object], raw_items)
        raw_cursor = data["next_cursor"]
        return cls(
            contract_version=_version_value(data["contract_version"], "Artifact page"),
            items=tuple(
                ArtifactRecord.from_data(as_object(item, "Artifact page item"))
                for item in items
            ),
            next_cursor=(
                ArtifactCursor.from_data(as_object(raw_cursor, "Artifact next cursor"))
                if raw_cursor is not None
                else None
            ),
        )


class WorkspaceRepository(Protocol):
    async def create_workspace(
        self, workspace: WorkspaceState, context: RuntimeCallContext
    ) -> WorkspaceState: ...

    async def get_workspace(
        self,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> WorkspaceState: ...

    async def compare_and_set_workspace(
        self,
        workspace: WorkspaceState,
        expected_version: int,
        context: RuntimeCallContext,
    ) -> WorkspaceState: ...


class ArtifactRepository(Protocol):
    async def put_artifact(
        self, value: ArtifactValue, context: RuntimeCallContext
    ) -> ArtifactRecord: ...

    async def get_artifact(
        self,
        artifact_id: ArtifactId,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> ArtifactValue: ...

    async def list_artifacts(
        self, query: ArtifactQuery, context: RuntimeCallContext
    ) -> ArtifactPage: ...


class StorageProvider(WorkspaceRepository, ArtifactRepository, Protocol):
    async def capabilities(
        self, context: RuntimeCallContext
    ) -> StorageCapabilities: ...


class StorageProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderId, StorageProvider] = {}

    def register(self, provider_id: ProviderId, provider: StorageProvider) -> None:
        if provider_id in self._providers:
            raise core_error(
                ErrorCategory.CONFLICT,
                "storage_provider_already_registered",
                "storage provider is already registered",
            )
        self._providers[provider_id] = provider

    def get(self, provider_id: ProviderId) -> StorageProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "storage_provider_not_registered",
                "storage provider is not registered",
                retryable=True,
            )
        return provider


class InMemoryStorageProvider:
    """Thread-safe reference provider with Workspace CAS and immutable Artifacts."""

    def __init__(self, provider_id: ProviderId, *, max_artifact_bytes: int = 16 << 20):
        self.provider_id = provider_id
        self._capabilities = StorageCapabilities(provider_id, max_artifact_bytes)
        self._workspaces: dict[WorkspaceId, WorkspaceState] = {}
        self._artifacts: dict[ArtifactId, ArtifactValue] = {}
        self._lock = RLock()

    async def capabilities(self, context: RuntimeCallContext) -> StorageCapabilities:
        del context
        return self._capabilities

    async def create_workspace(
        self, workspace: WorkspaceState, context: RuntimeCallContext
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        if workspace.state_version != 0:
            raise core_error(
                ErrorCategory.CONFLICT,
                "invalid_initial_workspace_version",
                "new Workspace state version must be zero",
            )
        with self._lock:
            if workspace.workspace_id in self._workspaces:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "workspace_already_exists",
                    "Workspace identity already exists",
                )
            self._workspaces[workspace.workspace_id] = workspace
        return workspace

    async def get_workspace(
        self,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace_id, scope, context)
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workspace_not_found",
                "Workspace does not exist",
            )
        if workspace.scope.key != scope.key:
            raise core_error(
                ErrorCategory.DENIED,
                "workspace_scope_mismatch",
                "Workspace does not belong to the requested Scope",
            )
        return workspace

    async def compare_and_set_workspace(
        self,
        workspace: WorkspaceState,
        expected_version: int,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        with self._lock:
            current = self._workspaces.get(workspace.workspace_id)
            if current is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "workspace_not_found",
                    "Workspace does not exist",
                )
            if current.scope.key != workspace.scope.key:
                raise core_error(
                    ErrorCategory.DENIED,
                    "workspace_scope_mismatch",
                    "Workspace Scope cannot change during compare-and-set",
                )
            if current.state_version != expected_version:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_state_version",
                    "Workspace state version does not match",
                    retryable=True,
                )
            if workspace.state_version != expected_version + 1:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "invalid_next_state_version",
                    "committed Workspace must increment state version exactly once",
                )
            self._workspaces[workspace.workspace_id] = workspace
        return workspace

    async def put_artifact(
        self, value: ArtifactValue, context: RuntimeCallContext
    ) -> ArtifactRecord:
        record = value.record
        self._require_record(record, context)
        with self._lock:
            workspace = self._workspaces.get(record.workspace_id)
            if workspace is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "workspace_not_found",
                    "Artifact Workspace does not exist",
                )
            try:
                record.scope.require_narrower_than(workspace.scope)
            except CoreError as error:
                raise core_error(
                    ErrorCategory.DENIED,
                    "artifact_scope_mismatch",
                    "Artifact Scope is outside its Workspace",
                ) from error
            existing = self._artifacts.get(record.artifact_id)
            if existing is not None:
                if existing == value:
                    return existing.record
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "artifact_identity_conflict",
                    "Artifact identity already exists with different content",
                )
            self._artifacts[record.artifact_id] = value
        return record

    async def get_artifact(
        self,
        artifact_id: ArtifactId,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> ArtifactValue:
        _require_workspace_context(workspace_id, scope, context)
        with self._lock:
            value = self._artifacts.get(artifact_id)
        if value is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "artifact_not_found",
                "Artifact does not exist",
            )
        if (
            value.record.workspace_id != workspace_id
            or value.record.scope.key != scope.key
        ):
            raise core_error(
                ErrorCategory.DENIED,
                "artifact_identity_mismatch",
                "Artifact does not belong to the requested Workspace and Scope",
            )
        return value

    async def list_artifacts(
        self, query: ArtifactQuery, context: RuntimeCallContext
    ) -> ArtifactPage:
        if query.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "storage_provider_mismatch",
                "Artifact query provider does not match storage provider",
            )
        _require_workspace_context(query.workspace_id, query.scope, context)
        if query.cursor is not None:
            _validate_cursor(query.cursor, query)
        before = (
            (query.cursor.before_created_at, query.cursor.before_artifact_id.value)
            if query.cursor
            else None
        )
        with self._lock:
            records = [
                value.record
                for value in self._artifacts.values()
                if value.record.workspace_id == query.workspace_id
                and value.record.scope.key == query.scope.key
                and (
                    before is None
                    or (value.record.created_at, value.record.artifact_id.value)
                    < before
                )
            ]
        records.sort(
            key=lambda item: (item.created_at, item.artifact_id.value), reverse=True
        )
        selected = tuple(records[: query.limit])
        next_cursor = None
        if len(records) > query.limit:
            last = selected[-1]
            next_cursor = ArtifactCursor(
                provider_id=self.provider_id,
                workspace_id=query.workspace_id,
                scope=query.scope,
                limit=query.limit,
                query_fingerprint=query.query_fingerprint,
                before_created_at=last.created_at,
                before_artifact_id=last.artifact_id,
            )
        return ArtifactPage(selected, next_cursor)

    def _require_record(
        self, record: ArtifactRecord, context: RuntimeCallContext
    ) -> None:
        if record.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "storage_provider_mismatch",
                "Artifact provider does not match storage provider",
            )
        _require_workspace_context(record.workspace_id, record.scope, context)
        if record.byte_length > self._capabilities.max_artifact_bytes:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "artifact_too_large",
                "Artifact exceeds the provider byte limit",
            )


class StorageGateway:
    def __init__(
        self,
        *,
        providers: StorageProviderRegistry,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._providers = providers
        self._dispatcher = dispatcher
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def capabilities(
        self, provider_id: ProviderId, context: RuntimeCallContext
    ) -> StorageCapabilities:
        constraints: dict[str, JsonValue] = {"provider_id": provider_id.value}
        return cast(
            StorageCapabilities,
            await self._dispatch(
                StorageOperation.CAPABILITIES,
                provider_id,
                STORAGE_CAPABILITIES_ACTION,
                context.scope,
                context,
                constraints,
                lambda provider, call: provider.capabilities(call.context),
                expected_type=StorageCapabilities,
            ),
        )

    async def create_workspace(
        self,
        provider_id: ProviderId,
        workspace: WorkspaceState,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        constraints: dict[str, JsonValue] = {
            "workspace_id": workspace.workspace_id.value,
            "state_version": workspace.state_version,
        }
        result = await self._dispatch(
            StorageOperation.WORKSPACE_CREATE,
            provider_id,
            WORKSPACE_CREATE_ACTION,
            workspace.scope,
            context,
            constraints,
            lambda provider, call: provider.create_workspace(workspace, call.context),
            expected_type=WorkspaceState,
        )
        assert isinstance(result, WorkspaceState)
        if result != workspace:
            _protocol_failure("storage provider changed the created Workspace")
        return result

    async def get_workspace(
        self,
        provider_id: ProviderId,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace_id, scope, context)
        constraints: dict[str, JsonValue] = {"workspace_id": workspace_id.value}
        result = await self._dispatch(
            StorageOperation.WORKSPACE_GET,
            provider_id,
            WORKSPACE_GET_ACTION,
            scope,
            context,
            constraints,
            lambda provider, call: provider.get_workspace(
                workspace_id, call.context.scope, call.context
            ),
            expected_type=WorkspaceState,
        )
        assert isinstance(result, WorkspaceState)
        if result.workspace_id != workspace_id or result.scope.key != scope.key:
            _protocol_failure("storage provider returned a mismatched Workspace")
        return result

    async def compare_and_set_workspace(
        self,
        provider_id: ProviderId,
        workspace: WorkspaceState,
        expected_version: int,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        constraints: dict[str, JsonValue] = {
            "workspace_id": workspace.workspace_id.value,
            "expected_version": expected_version,
            "new_version": workspace.state_version,
        }
        result = await self._dispatch(
            StorageOperation.WORKSPACE_COMPARE_AND_SET,
            provider_id,
            WORKSPACE_COMPARE_AND_SET_ACTION,
            workspace.scope,
            context,
            constraints,
            lambda provider, call: provider.compare_and_set_workspace(
                workspace, expected_version, call.context
            ),
            expected_type=WorkspaceState,
        )
        assert isinstance(result, WorkspaceState)
        if result != workspace:
            _protocol_failure(
                "storage provider returned a mismatched Workspace CAS result"
            )
        return result

    async def put_artifact(
        self, value: ArtifactValue, context: RuntimeCallContext
    ) -> ArtifactRecord:
        record = value.record
        _require_workspace_context(record.workspace_id, record.scope, context)
        constraints: dict[str, JsonValue] = {
            "workspace_id": record.workspace_id.value,
            "artifact_id": record.artifact_id.value,
            "sha256": record.sha256,
            "byte_length": record.byte_length,
        }
        result = await self._dispatch(
            StorageOperation.ARTIFACT_PUT,
            record.provider_id,
            ARTIFACT_PUT_ACTION,
            record.scope,
            context,
            constraints,
            lambda provider, call: provider.put_artifact(value, call.context),
            expected_type=ArtifactRecord,
            byte_count=record.byte_length,
        )
        assert isinstance(result, ArtifactRecord)
        if result != record:
            _protocol_failure("storage provider returned a mismatched Artifact record")
        return result

    async def get_artifact(
        self,
        provider_id: ProviderId,
        artifact_id: ArtifactId,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> ArtifactValue:
        _require_workspace_context(workspace_id, scope, context)
        constraints: dict[str, JsonValue] = {
            "workspace_id": workspace_id.value,
            "artifact_id": artifact_id.value,
        }
        result = await self._dispatch(
            StorageOperation.ARTIFACT_GET,
            provider_id,
            ARTIFACT_GET_ACTION,
            scope,
            context,
            constraints,
            lambda provider, call: provider.get_artifact(
                artifact_id, workspace_id, call.context.scope, call.context
            ),
            expected_type=ArtifactValue,
        )
        assert isinstance(result, ArtifactValue)
        if (
            result.record.provider_id != provider_id
            or result.record.artifact_id != artifact_id
            or result.record.workspace_id != workspace_id
            or result.record.scope.key != scope.key
        ):
            _protocol_failure("storage provider returned a mismatched Artifact")
        return result

    async def list_artifacts(
        self, query: ArtifactQuery, context: RuntimeCallContext
    ) -> ArtifactPage:
        _require_workspace_context(query.workspace_id, query.scope, context)
        requested: dict[str, JsonValue] = {
            "workspace_id": query.workspace_id.value,
            "limit": query.limit,
        }
        provider = self._providers.get(query.provider_id)
        started_at = self._clock.now()
        effective_query: ArtifactQuery | None = None
        await self._emit_started(
            StorageOperation.ARTIFACT_LIST, query.provider_id, context
        )
        access = self._request(
            query.provider_id,
            ARTIFACT_LIST_ACTION,
            query.scope,
            context,
            requested,
        )
        try:

            async def operation(call: AuthorizedCall) -> object:
                nonlocal effective_query
                effective = _constrain_query(query, call.grant.constraints)
                effective_query = effective
                return await self._invoke(
                    provider.list_artifacts(effective, call.context), call.context
                )

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, ArtifactPage):
                _protocol_failure("storage provider returned an invalid Artifact page")
            page = cast(ArtifactPage, raw)
            if effective_query is None:
                raise AssertionError("Artifact query was not dispatched")
            _validate_page(page, effective_query)
            await self._emit_completed(
                StorageOperation.ARTIFACT_LIST,
                query.provider_id,
                context,
                started_at,
                record_count=len(page.items),
            )
            return page
        except CoreError as error:
            await self._emit_failure(
                StorageOperation.ARTIFACT_LIST,
                query.provider_id,
                context,
                started_at,
                error.detail,
            )
            raise

    async def _dispatch(
        self,
        operation_name: StorageOperation,
        provider_id: ProviderId,
        action: ActionRef,
        scope: ScopeRef,
        context: RuntimeCallContext,
        constraints: Mapping[str, JsonValue],
        invoke: Callable[[StorageProvider, AuthorizedCall], Awaitable[object]],
        *,
        expected_type: type[object],
        byte_count: int = 0,
    ) -> object:
        provider = self._providers.get(provider_id)
        started_at = self._clock.now()
        await self._emit_started(operation_name, provider_id, context)
        access = self._request(provider_id, action, scope, context, constraints)
        try:

            async def operation(call: AuthorizedCall) -> object:
                _require_exact_constraints(call.grant.constraints, constraints)
                return await self._invoke(invoke(provider, call), call.context)

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, expected_type):
                _protocol_failure("storage provider returned an invalid result")
            await self._emit_completed(
                operation_name,
                provider_id,
                context,
                started_at,
                record_count=1,
                byte_count=byte_count,
            )
            return raw
        except CoreError as error:
            await self._emit_failure(
                operation_name, provider_id, context, started_at, error.detail
            )
            raise

    def _request(
        self,
        provider_id: ProviderId,
        action: ActionRef,
        scope: ScopeRef,
        context: RuntimeCallContext,
        constraints: Mapping[str, JsonValue],
    ) -> AccessRequest:
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core", "storage_provider", ResourceId(provider_id.value)
            ),
            scope=scope,
            context=context,
            constraints=constraints,
        )

    async def _invoke[ResultT](
        self, operation: Awaitable[ResultT], context: RuntimeCallContext
    ) -> ResultT:
        try:
            return await await_provider(operation, context, self._clock)
        except CoreError:
            raise
        except Exception as error:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "storage_provider_failure",
                "storage provider operation failed",
                retryable=True,
            ) from error

    async def _emit_started(
        self,
        operation: StorageOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
    ) -> None:
        await self._emit(
            STORAGE_OPERATION_STARTED,
            context,
            {"operation": operation.value, "provider_id": provider_id.value},
        )

    async def _emit_completed(
        self,
        operation: StorageOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        started_at: datetime,
        *,
        record_count: int = 0,
        byte_count: int = 0,
    ) -> None:
        await self._emit(
            STORAGE_OPERATION_COMPLETED,
            context,
            {
                "operation": operation.value,
                "provider_id": provider_id.value,
                "record_count": record_count,
                "byte_count": byte_count,
                "outcome": "completed",
                "latency_ms": max(
                    0, int((self._clock.now() - started_at).total_seconds() * 1000)
                ),
            },
        )

    async def _emit_failure(
        self,
        operation: StorageOperation,
        provider_id: ProviderId,
        context: RuntimeCallContext,
        started_at: datetime,
        error: ErrorDetail,
    ) -> None:
        await self._emit(
            STORAGE_OPERATION_FAILED,
            context,
            {
                "operation": operation.value,
                "provider_id": provider_id.value,
                "error_code": error.code,
                "category": error.category.value,
                "outcome": "failed",
                "latency_ms": max(
                    0, int((self._clock.now() - started_at).total_seconds() * 1000)
                ),
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


def _require_workspace_context(
    workspace_id: WorkspaceId, scope: ScopeRef, context: RuntimeCallContext
) -> None:
    scope.require_narrower_than(context.scope)
    if workspace_id != context.workspace_id:
        raise core_error(
            ErrorCategory.DENIED,
            "workspace_context_mismatch",
            "storage operation Workspace does not match RuntimeCallContext",
        )


def _require_exact_constraints(
    granted: Mapping[str, JsonValue], requested: Mapping[str, JsonValue]
) -> None:
    if dict(granted) != dict(requested):
        raise core_error(
            ErrorCategory.DENIED,
            "invalid_grant",
            "storage grant changed immutable constraints",
        )


def _constrain_query(
    query: ArtifactQuery, constraints: Mapping[str, JsonValue]
) -> ArtifactQuery:
    if set(constraints) != {"workspace_id", "limit"}:
        _invalid_grant("Artifact list grant has unknown or missing constraints")
    if constraints["workspace_id"] != query.workspace_id.value:
        _invalid_grant("Artifact list grant changed Workspace identity")
    limit = constraints["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        _invalid_grant("Artifact list grant limit is malformed")
    assert isinstance(limit, int)
    if limit < 1 or limit > query.limit:
        _invalid_grant("Artifact list grant expanded limit")
    if query.cursor is not None and limit != query.limit:
        _invalid_grant("Artifact list cursor forbids limit drift")
    return replace(query, limit=limit)


def _validate_page(page: ArtifactPage, query: ArtifactQuery) -> None:
    if len(page.items) > query.limit:
        _protocol_failure("storage provider returned too many Artifacts")
    previous: tuple[datetime, str] | None = None
    for record in page.items:
        if (
            record.provider_id != query.provider_id
            or record.workspace_id != query.workspace_id
            or record.scope.key != query.scope.key
        ):
            _protocol_failure("storage provider page escaped the authorized query")
        key = record.created_at, record.artifact_id.value
        if previous is not None and key >= previous:
            _protocol_failure("storage provider Artifact page order is invalid")
        previous = key
    if page.next_cursor is not None:
        _validate_cursor(page.next_cursor, replace(query, limit=page.next_cursor.limit))


def _validate_cursor(cursor: ArtifactCursor, query: ArtifactQuery) -> None:
    if (
        cursor.provider_id != query.provider_id
        or cursor.workspace_id != query.workspace_id
        or cursor.scope.key != query.scope.key
        or cursor.limit != query.limit
        or cursor.query_fingerprint != query.query_fingerprint
    ):
        raise core_error(
            ErrorCategory.CONFLICT,
            "artifact_cursor_drift",
            "Artifact cursor does not match the effective query",
        )


def _protocol_failure(message: str) -> None:
    raise core_error(
        ErrorCategory.PROTOCOL_FAILURE, "storage_protocol_failure", message
    )


def _invalid_grant(message: str) -> None:
    raise core_error(ErrorCategory.DENIED, "invalid_grant", message)


def _require_version(version: str, name: str) -> None:
    if version != "1":
        raise ValueError(f"unsupported {name} contract version")


def _version_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} contract version must be a string")
    _require_version(value, name)
    return value


def _bool_value(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _datetime_value(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    return require_utc(parsed, name)


def _require_keys(data: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(data) != expected:
        raise ValueError(f"{name} fields do not match contract version 1")
