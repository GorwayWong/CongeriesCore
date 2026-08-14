"""Tool Operation Log v1 model, store, CAS, and authorization contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from congeries_core.adapter import SqliteToolOperationStore
from congeries_core.checkpoint import CheckpointReference
from congeries_core.policy.authorization import (
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    DenyAllPolicy,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import (
    IdempotencyKey,
    NodeId,
    PrincipalId,
    ResourceId,
    RunId,
    WorkspaceId,
)
from congeries_core.tool import (
    InMemoryToolOperationStore,
    PrepareToolOperation,
    ToolOperationGateway,
    ToolOperationRecord,
    ToolOperationStatus,
    ToolSideEffect,
    TransitionToolOperation,
    tool_operation_actions,
)

from .provider_support import AuditRecorder, FailureRecorder, RecordingPolicy
from .support import NOW, FixedClock, call_context, root_scope

TOOL = CapabilityRef(
    "core", "tool", ResourceId("operation-tool"), "operation-plugin", "1"
)


def _reference(kind: str, identifier: str) -> CheckpointReference:
    return CheckpointReference(
        kind,
        ResourceRef("core", kind, ResourceId(identifier)),
        root_scope(),
        "1",
    )


def _record(*, fingerprint: str = "a" * 64) -> ToolOperationRecord:
    return ToolOperationRecord(
        ResourceId("operation-1"),
        RunId("run-1"),
        WorkspaceId("workspace-1"),
        NodeId("tool-node"),
        root_scope(),
        TOOL,
        IdempotencyKey("operation-key"),
        fingerprint,
        _reference("workflow_node_output", "request-1"),
        ToolSideEffect.EXTERNAL,
        ToolOperationStatus.PREPARED,
        0,
        NOW,
        NOW,
    )


def _principal() -> RuntimePrincipal:
    return RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1"))


def _gateway(
    store: object,
    *,
    allow: bool = True,
    policy: RecordingPolicy | None = None,
) -> ToolOperationGateway:
    dispatcher = AuthorizedDispatcher[object](
        action_registry=ActionRegistry(tool_operation_actions()),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=FixedClock(),
        policy=(policy or RecordingPolicy()) if allow else DenyAllPolicy(),
    )
    return ToolOperationGateway(store, dispatcher)  # type: ignore[arg-type]


@pytest.fixture(params=("memory", "sqlite"))
def operation_store(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    if request.param == "memory":
        return InMemoryToolOperationStore(FixedClock())
    return SqliteToolOperationStore(
        tmp_path / "tool-operations.sqlite3", FixedClock()
    )


def test_tool_operation_record_round_trips_strictly() -> None:
    record = _record()
    assert ToolOperationRecord.from_data(record.to_data()) == record
    with pytest.raises(ValueError, match="fields are invalid"):
        ToolOperationRecord.from_data({**record.to_data(), "unknown": True})
    with pytest.raises(ValueError, match="fingerprint"):
        replace(record, request_fingerprint="not-sha256")


@pytest.mark.asyncio
async def test_store_prepare_replay_and_payload_drift(
    operation_store: object,
) -> None:
    gateway = _gateway(operation_store)
    context = call_context(run_id=RunId("run-1"))
    record = _record()
    first = await gateway.prepare(PrepareToolOperation(record, _principal()), context)
    assert await gateway.prepare(
        PrepareToolOperation(record, _principal()), context
    ) == first
    with pytest.raises(CoreError) as error:
        await gateway.prepare(
            PrepareToolOperation(
                replace(record, request_fingerprint="b" * 64), _principal()
            ),
            context,
        )
    assert error.value.detail.code == "tool_operation_identity_conflict"


@pytest.mark.asyncio
async def test_store_cas_unknown_resolution_and_terminal_immutability(
    operation_store: object,
) -> None:
    gateway = _gateway(operation_store)
    context = call_context(run_id=RunId("run-1"))
    principal = _principal()
    record = await gateway.prepare(PrepareToolOperation(_record(), principal), context)
    dispatching = await gateway.transition(
        TransitionToolOperation(
            record.operation_id,
            record.run_id,
            record.request_fingerprint,
            0,
            ToolOperationStatus.DISPATCHING,
            None,
            None,
            principal,
        ),
        context,
    )
    unknown_ref = _reference("workflow_node_output", "unknown-1")
    unknown = await gateway.transition(
        TransitionToolOperation(
            record.operation_id,
            record.run_id,
            record.request_fingerprint,
            dispatching.version,
            ToolOperationStatus.UNKNOWN,
            unknown_ref,
            None,
            principal,
        ),
        context,
    )
    outcome_ref = _reference("workflow_node_output", "result-1")
    evidence_ref = _reference("tool_evidence", "evidence-1")
    succeeded = await gateway.transition(
        TransitionToolOperation(
            record.operation_id,
            record.run_id,
            record.request_fingerprint,
            unknown.version,
            ToolOperationStatus.SUCCEEDED,
            outcome_ref,
            evidence_ref,
            principal,
        ),
        context,
        resolve=True,
    )
    assert succeeded.version == 3
    assert succeeded.evidence_ref == evidence_ref
    with pytest.raises(CoreError) as error:
        await gateway.transition(
            TransitionToolOperation(
                record.operation_id,
                record.run_id,
            record.request_fingerprint,
                succeeded.version,
                ToolOperationStatus.FAILED,
                outcome_ref,
                evidence_ref,
                principal,
            ),
            context,
            resolve=True,
        )
    assert error.value.detail.code == "tool_operation_transition_invalid"


@pytest.mark.asyncio
async def test_store_cas_allows_only_one_concurrent_transition(
    operation_store: object,
) -> None:
    gateway = _gateway(operation_store)
    context = call_context(run_id=RunId("run-1"))
    principal = _principal()
    record = await gateway.prepare(PrepareToolOperation(_record(), principal), context)

    async def transition(target: ToolOperationStatus) -> object:
        outcome_ref = (
            None
            if target is ToolOperationStatus.DISPATCHING
            else _reference("workflow_node_output", f"result-{target.value}")
        )
        return await gateway.transition(
            TransitionToolOperation(
                record.operation_id,
                record.run_id,
            record.request_fingerprint,
                record.version,
                target,
                outcome_ref,
                None,
                principal,
            ),
            context,
        )

    outcomes = await asyncio.gather(
        transition(ToolOperationStatus.DISPATCHING),
        transition(ToolOperationStatus.FAILED),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ToolOperationRecord) for item in outcomes) == 1
    assert sum(isinstance(item, CoreError) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_sqlite_store_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    context = call_context(run_id=RunId("run-1"))
    await SqliteToolOperationStore(path).prepare(_record(), context)
    restored = await SqliteToolOperationStore(path).read(
        ResourceId("operation-1"), context
    )
    assert restored == _record()


@pytest.mark.asyncio
async def test_operation_gateway_is_default_deny() -> None:
    gateway = _gateway(InMemoryToolOperationStore(FixedClock()), allow=False)
    with pytest.raises(CoreError) as error:
        await gateway.prepare(
            PrepareToolOperation(_record(), _principal()),
            call_context(run_id=RunId("run-1")),
        )
    assert error.value.detail.category.value == "denied"


@pytest.mark.asyncio
async def test_operation_gateway_rejects_invalid_grant_constraints() -> None:
    gateway = _gateway(
        InMemoryToolOperationStore(FixedClock()),
        policy=RecordingPolicy(constraints={"request_fingerprint": "b" * 64}),
    )
    with pytest.raises(CoreError) as error:
        await gateway.prepare(
            PrepareToolOperation(_record(), _principal()),
            call_context(run_id=RunId("run-1")),
        )
    assert error.value.detail.code == "invalid_grant"


@pytest.mark.asyncio
async def test_operation_store_rejects_workspace_context_mismatch() -> None:
    gateway = _gateway(InMemoryToolOperationStore(FixedClock()))
    context = replace(
        call_context(run_id=RunId("run-1")),
        workspace_id=WorkspaceId("other-workspace"),
    )
    with pytest.raises(CoreError) as error:
        await gateway.prepare(
            PrepareToolOperation(_record(), _principal()), context
        )
    assert error.value.detail.code == "tool_operation_context_mismatch"
