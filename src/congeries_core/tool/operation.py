"""Durable Tool operation intent and unknown-outcome contracts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from congeries_core.checkpoint import CheckpointReference
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, require_utc
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import (
    IdempotencyKey,
    NodeId,
    ResourceId,
    RunId,
    WorkspaceId,
)
from congeries_core.runtime.json_types import JsonValue, as_int, as_object
from congeries_core.runtime.scope import ScopeRef

from .model import ToolSideEffect

TOOL_OPERATION_CONTRACT_VERSION = "1"
TOOL_OPERATION_PREPARE_ACTION = ActionRef("core", "tool_operation.prepare", "1")
TOOL_OPERATION_READ_ACTION = ActionRef("core", "tool_operation.read", "1")
TOOL_OPERATION_TRANSITION_ACTION = ActionRef("core", "tool_operation.transition", "1")
TOOL_OPERATION_RESOLVE_ACTION = ActionRef("core", "tool_operation.resolve", "1")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def tool_operation_actions() -> tuple[ActionRef, ...]:
    return (
        TOOL_OPERATION_PREPARE_ACTION,
        TOOL_OPERATION_READ_ACTION,
        TOOL_OPERATION_TRANSITION_ACTION,
        TOOL_OPERATION_RESOLVE_ACTION,
    )


class ToolOperationStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED}


@dataclass(frozen=True, slots=True)
class ToolOperationRecord:
    operation_id: ResourceId
    run_id: RunId
    workspace_id: WorkspaceId
    node_id: NodeId
    scope: ScopeRef
    tool: CapabilityRef
    idempotency_key: IdempotencyKey
    request_fingerprint: str
    request_ref: CheckpointReference
    side_effect: ToolSideEffect
    status: ToolOperationStatus
    version: int
    created_at: datetime
    updated_at: datetime
    outcome_ref: CheckpointReference | None = None
    evidence_ref: CheckpointReference | None = None
    contract_version: str = TOOL_OPERATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TOOL_OPERATION_CONTRACT_VERSION:
            raise ValueError("unsupported Tool operation contract version")
        if self.version < 0 or not _SHA256.fullmatch(self.request_fingerprint):
            raise ValueError("Tool operation version or fingerprint is invalid")
        if self.tool.namespace != "core" or self.tool.kind != "tool":
            raise ValueError("Tool operation requires a core Tool reference")
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", require_utc(self.updated_at, "updated_at")
        )
        if self.updated_at < self.created_at:
            raise ValueError("Tool operation update cannot precede creation")
        self.request_ref.scope.require_narrower_than(self.scope)
        if self.outcome_ref is not None:
            self.outcome_ref.scope.require_narrower_than(self.scope)
        if self.status in {
            ToolOperationStatus.PREPARED,
            ToolOperationStatus.DISPATCHING,
        } and (self.outcome_ref is not None or self.evidence_ref is not None):
            raise ValueError("non-outcome Tool operation cannot contain references")
        if self.status.terminal and self.outcome_ref is None:
            raise ValueError("terminal Tool operation requires an outcome reference")
        if self.status is ToolOperationStatus.UNKNOWN and self.outcome_ref is None:
            raise ValueError("unknown Tool operation requires an outcome reference")
        if self.status is ToolOperationStatus.UNKNOWN and self.evidence_ref is not None:
            raise ValueError(
                "unknown Tool operation cannot contain resolution evidence"
            )
        if self.evidence_ref is not None:
            self.evidence_ref.scope.require_narrower_than(self.scope)

    @property
    def ref(self) -> ResourceRef:
        return ResourceRef("core", "tool_operation", self.operation_id)

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "operation_id": self.operation_id.value,
            "run_id": self.run_id.value,
            "workspace_id": self.workspace_id.value,
            "node_id": self.node_id.value,
            "scope": self.scope.to_data(),
            "tool": self.tool.to_data(),
            "idempotency_key": self.idempotency_key.value,
            "request_fingerprint": self.request_fingerprint,
            "request_ref": self.request_ref.to_data(),
            "side_effect": self.side_effect.value,
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "outcome_ref": self.outcome_ref.to_data() if self.outcome_ref else None,
            "evidence_ref": self.evidence_ref.to_data() if self.evidence_ref else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolOperationRecord:
        expected = {
            "contract_version",
            "operation_id",
            "run_id",
            "workspace_id",
            "node_id",
            "scope",
            "tool",
            "idempotency_key",
            "request_fingerprint",
            "request_ref",
            "side_effect",
            "status",
            "version",
            "created_at",
            "updated_at",
            "outcome_ref",
            "evidence_ref",
        }
        if set(data) != expected:
            raise ValueError("Tool operation record fields are invalid")
        outcome = data["outcome_ref"]
        evidence = data["evidence_ref"]
        return cls(
            operation_id=ResourceId(str(data["operation_id"])),
            run_id=RunId(str(data["run_id"])),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            node_id=NodeId(str(data["node_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "operation scope")),
            tool=CapabilityRef.from_data(as_object(data["tool"], "operation tool")),
            idempotency_key=IdempotencyKey(str(data["idempotency_key"])),
            request_fingerprint=str(data["request_fingerprint"]),
            request_ref=CheckpointReference.from_data(
                as_object(data["request_ref"], "request ref")
            ),
            side_effect=ToolSideEffect(str(data["side_effect"])),
            status=ToolOperationStatus(str(data["status"])),
            version=as_int(data["version"], "operation version"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            outcome_ref=CheckpointReference.from_data(as_object(outcome, "outcome ref"))
            if outcome is not None
            else None,
            evidence_ref=CheckpointReference.from_data(
                as_object(evidence, "evidence ref")
            )
            if evidence is not None
            else None,
            contract_version=str(data["contract_version"]),
        )


@dataclass(frozen=True, slots=True)
class PrepareToolOperation:
    record: ToolOperationRecord
    principal: RuntimePrincipal


@dataclass(frozen=True, slots=True)
class TransitionToolOperation:
    operation_id: ResourceId
    run_id: RunId
    request_fingerprint: str
    expected_version: int
    target: ToolOperationStatus
    outcome_ref: CheckpointReference | None
    evidence_ref: CheckpointReference | None
    principal: RuntimePrincipal


class ToolOperationStore(Protocol):
    async def prepare(
        self, record: ToolOperationRecord, context: RuntimeCallContext
    ) -> ToolOperationRecord: ...
    async def read(
        self, operation_id: ResourceId, context: RuntimeCallContext
    ) -> ToolOperationRecord: ...
    async def transition(
        self, request: TransitionToolOperation, context: RuntimeCallContext
    ) -> ToolOperationRecord: ...


class InMemoryToolOperationStore:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[ResourceId, ToolOperationRecord] = {}
        self._keys: dict[tuple[RunId, IdempotencyKey], ResourceId] = {}
        self._lock = asyncio.Lock()

    async def prepare(
        self, record: ToolOperationRecord, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        _validate_prepared(record)
        _validate_context(record, context)
        async with self._lock:
            existing = self._records.get(record.operation_id)
            keyed = self._keys.get((record.run_id, record.idempotency_key))
            if existing is not None or keyed is not None:
                candidate = existing or self._records[keyed]  # type: ignore[index]
                if _prepare_identity(candidate) == _prepare_identity(record):
                    return candidate
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "tool_operation_identity_conflict",
                    "Tool operation identity or payload changed",
                )
            self._records[record.operation_id] = record
            self._keys[(record.run_id, record.idempotency_key)] = record.operation_id
            return record

    async def read(
        self, operation_id: ResourceId, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        async with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise core_error(
                    ErrorCategory.UNAVAILABLE,
                    "tool_operation_not_found",
                    "Tool operation was not found",
                )
            _validate_context(record, context)
            return record

    async def transition(
        self, request: TransitionToolOperation, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        async with self._lock:
            current = self._records.get(request.operation_id)
            if current is None:
                raise core_error(
                    ErrorCategory.UNAVAILABLE,
                    "tool_operation_not_found",
                    "Tool operation was not found",
                )
            _validate_context(current, context)
            if current.run_id != request.run_id:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "tool_operation_run_mismatch",
                    "Tool operation Run does not match",
                )
            if current.request_fingerprint != request.request_fingerprint:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "tool_operation_request_fingerprint_conflict",
                    "Tool operation request fingerprint changed",
                )
            if (
                current.status is request.target
                and current.outcome_ref == request.outcome_ref
                and current.evidence_ref == request.evidence_ref
            ):
                return current
            if current.version != request.expected_version:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "tool_operation_version_conflict",
                    "Tool operation version is stale",
                )
            if not _allowed(current.status, request.target):
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "tool_operation_transition_invalid",
                    "Tool operation transition is invalid",
                )
            updated = replace(
                current,
                status=request.target,
                version=current.version + 1,
                updated_at=self._clock.now(),
                outcome_ref=request.outcome_ref,
                evidence_ref=request.evidence_ref,
            )
            self._records[current.operation_id] = updated
            return updated


class ToolOperationGateway:
    def __init__(
        self, store: ToolOperationStore, dispatcher: AuthorizedDispatcher[object]
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher

    async def prepare(
        self, request: PrepareToolOperation, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        async def save(call: AuthorizedCall) -> ToolOperationRecord:
            return await self._store.prepare(request.record, call.context)

        return await self._dispatch_raw(
            TOOL_OPERATION_PREPARE_ACTION,
            request.record.operation_id,
            request.principal,
            context,
            save,
            {
                "run_id": request.record.run_id.value,
                "workspace_id": request.record.workspace_id.value,
                "node_id": request.record.node_id.value,
                "tool": "/".join(request.record.tool.key),
                "idempotency_key": request.record.idempotency_key.value,
                "request_fingerprint": request.record.request_fingerprint,
            },
        )

    async def read(
        self,
        operation_id: ResourceId,
        principal: RuntimePrincipal,
        context: RuntimeCallContext,
    ) -> ToolOperationRecord:
        async def load(call: AuthorizedCall) -> ToolOperationRecord:
            return await self._store.read(operation_id, call.context)

        return await self._dispatch_raw(
            TOOL_OPERATION_READ_ACTION,
            operation_id,
            principal,
            context,
            load,
            {
                "run_id": context.run_id.value,
                "workspace_id": context.workspace_id.value,
            },
        )

    async def transition(
        self,
        request: TransitionToolOperation,
        context: RuntimeCallContext,
        *,
        resolve: bool = False,
    ) -> ToolOperationRecord:
        action = (
            TOOL_OPERATION_RESOLVE_ACTION
            if resolve
            else TOOL_OPERATION_TRANSITION_ACTION
        )

        async def update(call: AuthorizedCall) -> ToolOperationRecord:
            return await self._store.transition(request, call.context)

        return await self._dispatch_raw(
            action,
            request.operation_id,
            request.principal,
            context,
            update,
            {
                "run_id": request.run_id.value,
                "workspace_id": context.workspace_id.value,
                "request_fingerprint": request.request_fingerprint,
                "expected_version": request.expected_version,
                "target": request.target.value,
            },
        )

    async def _dispatch_raw(
        self,
        action: ActionRef,
        operation_id: ResourceId,
        principal: RuntimePrincipal,
        context: RuntimeCallContext,
        operation: Callable[[AuthorizedCall], Awaitable[ToolOperationRecord]],
        constraints: dict[str, JsonValue],
    ) -> ToolOperationRecord:
        async def authorized(call: AuthorizedCall) -> ToolOperationRecord:
            _validate_grant_constraints(call)
            return await operation(call)

        result = await self._dispatcher.dispatch(
            AccessRequest(
                principal,
                action,
                ResourceRef("core", "tool_operation", operation_id),
                context.scope,
                context,
                constraints,
            ),
            authorized,
        )
        if not isinstance(result, ToolOperationRecord):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "tool_operation_store_invalid",
                "Tool operation store returned an invalid record",
            )
        if (
            result.operation_id != operation_id
            or result.run_id != context.run_id
            or result.workspace_id != context.workspace_id
            or not result.scope.is_equal_or_descendant_of(context.scope)
        ):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "tool_operation_store_identity_mismatch",
                "Tool operation store returned the wrong operation identity",
            )
        return result


def _allowed(source: ToolOperationStatus, target: ToolOperationStatus) -> bool:
    return target in {
        ToolOperationStatus.PREPARED: {
            ToolOperationStatus.DISPATCHING,
            ToolOperationStatus.FAILED,
        },
        ToolOperationStatus.DISPATCHING: {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.UNKNOWN,
            ToolOperationStatus.FAILED,
        },
        ToolOperationStatus.UNKNOWN: {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.FAILED,
        },
    }.get(source, set())


def _prepare_identity(record: ToolOperationRecord) -> tuple[object, ...]:
    return (
        record.operation_id,
        record.run_id,
        record.workspace_id,
        record.node_id,
        record.scope,
        record.tool,
        record.idempotency_key,
        record.request_fingerprint,
        record.request_ref,
        record.side_effect,
    )


def _validate_context(record: ToolOperationRecord, context: RuntimeCallContext) -> None:
    if record.run_id != context.run_id or record.workspace_id != context.workspace_id:
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "tool_operation_context_mismatch",
            "Tool operation context does not match",
        )
    record.scope.require_narrower_than(context.scope)


def _validate_prepared(record: ToolOperationRecord) -> None:
    if (
        record.status is not ToolOperationStatus.PREPARED
        or record.version != 0
        or record.outcome_ref is not None
        or record.evidence_ref is not None
        or record.created_at != record.updated_at
    ):
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "tool_operation_prepare_invalid",
            "Tool operation prepare requires a clean version-zero intent",
        )


def _validate_grant_constraints(call: AuthorizedCall) -> None:
    requested = call.request.constraints
    for key, value in call.grant.constraints.items():
        if key not in requested or requested[key] != value:
            raise core_error(
                ErrorCategory.DENIED,
                "invalid_grant",
                "Tool operation grant constraints do not match the request",
            )
