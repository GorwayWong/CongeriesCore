from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from congeries_core.runtime.errors import CoreError, ErrorCategory, ErrorDetail
from congeries_core.runtime.ids import (
    AgentId,
    ArtifactId,
    DefinitionId,
    ModelBindingRef,
    WorkflowId,
    WorkspaceId,
)
from congeries_core.runtime.run import (
    AgentRun,
    AttemptOutcome,
    AttemptRecord,
    AuditFailureMode,
    Run,
    RunControlPolicy,
    RunStateMachine,
    RunStatus,
    WorkflowRun,
    create_child_agent_run,
    create_root_workflow_run,
)
from congeries_core.state.repository import InMemoryRunRepository
from congeries_core.state.service import RunService
from congeries_core.state.session import (
    InMemorySessionRepository,
    SessionState,
    SessionStatus,
)
from congeries_core.state.workspace import WorkspaceState

from .support import NOW, FixedClock, agent_run, child_scope, root_scope, session_ref


def transition_to_running(run: AgentRun) -> AgentRun:
    machine = RunStateMachine()
    current = machine.start(run, 0, NOW).current
    current = machine.advance(
        current, RunStatus.CONTEXT_LOADING, current.state_version, NOW
    ).current
    current = machine.advance(
        current, RunStatus.RUNNING, current.state_version, NOW
    ).current
    assert isinstance(current, AgentRun)
    return current


def test_run_factories_hierarchy_and_json_round_trip() -> None:
    workflow = create_root_workflow_run(
        definition_id=DefinitionId("workflow-definition"),
        workflow_id=WorkflowId("workflow-1"),
        graph_version="v1",
        workspace_id=WorkspaceId("workspace-1"),
        scope=root_scope(),
        created_at=NOW,
        session_ref=session_ref(),
    )
    child = create_child_agent_run(
        workflow,
        definition_id=DefinitionId("agent-definition"),
        agent_id=AgentId("agent-1"),
        model_binding_ref=ModelBindingRef("model-1"),
        scope=child_scope(workflow.scope),
        created_at=NOW,
    )
    assert child.root_run_id == workflow.run_id
    assert child.parent_run_id == workflow.run_id
    assert isinstance(Run.from_data(workflow.to_data()), WorkflowRun)
    assert isinstance(Run.from_data(child.to_data()), AgentRun)

    terminal = replace(workflow, status=RunStatus.CANCELLED, ended_at=NOW)
    with pytest.raises(CoreError, match="terminal"):
        create_child_agent_run(
            terminal,
            definition_id=DefinitionId("agent-definition"),
            agent_id=AgentId("agent-1"),
            model_binding_ref=ModelBindingRef("model-1"),
            scope=child_scope(workflow.scope),
            created_at=NOW,
        )


def test_lifecycle_pause_resume_retry_recovery_and_terminal_rules() -> None:
    machine = RunStateMachine()
    running = transition_to_running(agent_run())
    waiting = machine.advance(
        running, RunStatus.WAITING_APPROVAL, running.state_version, NOW
    ).current
    resumed_approval = machine.advance(
        waiting, RunStatus.RUNNING, waiting.state_version, NOW
    ).current
    paused = machine.pause(
        resumed_approval, resumed_approval.state_version, NOW
    ).current
    assert paused.continuation_status is RunStatus.RUNNING
    resumed = machine.resume(paused, paused.state_version, NOW).current
    assert resumed.status is RunStatus.RUNNING
    assert resumed.continuation_status is None

    retryable = ErrorDetail(
        ErrorCategory.UNAVAILABLE, "transient", "transient failure", retryable=True
    )
    retrying = machine.retry(resumed, resumed.state_version, NOW, retryable).current
    assert retrying.attempt == 2
    assert retrying.continuation_status is RunStatus.RUNNING
    assert retrying.attempt_history[-1].outcome is AttemptOutcome.RETRYABLE_FAILURE
    retry_paused = machine.pause(retrying, retrying.state_version, NOW).current
    retry_resumed = machine.resume(
        retry_paused, retry_paused.state_version, NOW
    ).current
    assert retry_resumed.status is RunStatus.RUNNING
    assert retry_resumed.attempt_history[-1].open
    retrying = machine.retry(
        retry_resumed, retry_resumed.state_version, NOW, retryable
    ).current
    redispatched = machine.redispatch_retry(
        retrying, retrying.state_version, NOW
    ).current
    assert redispatched.status is RunStatus.RUNNING
    assert redispatched.attempt_history[-1].open

    recovering = machine.recover(redispatched, redispatched.state_version, NOW).current
    assert recovering.status is RunStatus.RECOVERING
    recovered = machine.advance(
        recovering, RunStatus.RUNNING, recovering.state_version, NOW
    ).current
    assert recovered.attempt_history[-1].open
    completed = machine.complete(recovered, recovered.state_version, NOW).current
    assert completed.status is RunStatus.SUCCEEDED
    assert completed.ended_at == NOW
    assert (
        machine.complete(completed, completed.state_version, NOW).current is completed
    )
    with pytest.raises(CoreError):
        machine.cancel(completed, completed.state_version, NOW)


def test_failure_cancel_and_illegal_transitions() -> None:
    machine = RunStateMachine()
    created = agent_run()
    with pytest.raises(CoreError, match="cannot transition"):
        machine.complete(created, created.state_version, NOW)
    with pytest.raises(CoreError) as stale:
        machine.start(created, 10, NOW)
    assert stale.value.detail.code == "stale_state_version"
    running = transition_to_running(agent_run())
    with pytest.raises(CoreError) as non_retryable:
        machine.retry(
            running,
            running.state_version,
            NOW,
            ErrorDetail(ErrorCategory.CONFLICT, "fatal", "fatal"),
        )
    assert non_retryable.value.detail.code == "non_retryable_error"

    started = machine.start(created, 0, NOW).current
    assert machine.start(started, started.state_version, NOW).current is started
    cancelled = machine.cancel(started, started.state_version, NOW).current
    assert cancelled.status is RunStatus.CANCELLED
    assert machine.cancel(cancelled, cancelled.state_version, NOW).current is cancelled

    running = transition_to_running(agent_run())
    error = ErrorDetail(ErrorCategory.PROTOCOL_FAILURE, "fatal", "fatal failure")
    failed = machine.fail(running, running.state_version, NOW, error).current
    assert failed.status is RunStatus.FAILED
    assert failed.error_summary is not None
    assert machine.fail(failed, failed.state_version, NOW, error).current is failed


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"attempt": 0}, "attempt"),
        ({"state_version": -1}, "state version"),
        ({"root_run_id": agent_run().run_id}, "root Run"),
        ({"status": RunStatus.SUCCEEDED}, "ended_at"),
        ({"ended_at": NOW}, "non-terminal"),
        ({"status": RunStatus.PAUSED}, "continuation_status"),
        ({"continuation_status": RunStatus.RUNNING}, "valid only"),
        (
            {
                "status": RunStatus.PAUSED,
                "continuation_status": RunStatus.SUCCEEDED,
            },
            "resumable phase",
        ),
    ],
)
def test_run_invariants(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(agent_run(), **changes)


def test_run_attempt_history_invariants() -> None:
    with pytest.raises(ValueError, match="without duplicates"):
        replace(
            agent_run(),
            attempt=2,
            attempt_history=(AttemptRecord(1, NOW), AttemptRecord(1, NOW)),
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        replace(agent_run(), attempt_history=(AttemptRecord(2, NOW),))
    with pytest.raises(ValueError, match="current final"):
        replace(
            agent_run(),
            attempt=2,
            attempt_history=(AttemptRecord(1, NOW), AttemptRecord(2, NOW)),
        )


@pytest.mark.asyncio
async def test_run_repository_service_and_concurrent_cas() -> None:
    repository = InMemoryRunRepository()
    service = RunService(repository, FixedClock())
    run = agent_run()
    await service.create(run)
    with pytest.raises(CoreError):
        await service.create(run)
    with pytest.raises(CoreError):
        await service.get(type(run.run_id)("missing"))

    current = await service.start(run.run_id, 0)
    current = await service.advance(
        run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
    )
    current = await service.advance(
        run.run_id, current.state_version, RunStatus.RUNNING
    )
    results = await asyncio.gather(
        service.complete(run.run_id, current.state_version),
        service.cancel(run.run_id, current.state_version),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CoreError) for item in results) == 1
    assert (await service.get(run.run_id)).status in {
        RunStatus.SUCCEEDED,
        RunStatus.CANCELLED,
    }


@pytest.mark.asyncio
async def test_run_service_retry_and_audit_failure_modes() -> None:
    repository = InMemoryRunRepository()
    clock = FixedClock()
    service = RunService(repository, clock)
    run = agent_run()
    await service.create(run)
    current = await service.start(run.run_id, 0)
    current = await service.advance(
        run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
    )
    current = await service.advance(
        run.run_id, current.state_version, RunStatus.RUNNING
    )
    error = ErrorDetail(
        ErrorCategory.UNAVAILABLE, "audit", "audit failed", retryable=True
    )
    paused = await service.handle_audit_failure(run.run_id, error)
    assert paused.status is RunStatus.PAUSED
    assert await service.handle_audit_failure(run.run_id, error) == paused
    resumed = await service.resume(run.run_id, paused.state_version)
    retried = await service.retry(run.run_id, resumed.state_version, error)
    redispatched = await service.redispatch_retry(run.run_id, retried.state_version)
    recovered = await service.recover(run.run_id, redispatched.state_version)
    assert recovered.status is RunStatus.RECOVERING

    failing = replace(
        agent_run(),
        control_policy=RunControlPolicy(AuditFailureMode.FAIL),
    )
    await service.create(failing)
    failed = await service.handle_audit_failure(failing.run_id, error)
    assert failed.status is RunStatus.FAILED
    assert await service.handle_audit_failure(failing.run_id, error) is failed


@pytest.mark.asyncio
async def test_session_and_workspace_state() -> None:
    sessions = InMemorySessionRepository()
    session = SessionState(session_ref(), SessionStatus.OPEN, NOW)
    await sessions.add(session)
    assert await sessions.require_open(session.ref) == session
    with pytest.raises(CoreError):
        await sessions.add(session)
    with pytest.raises(CoreError):
        await sessions.get(session_ref().__class__("other", session.ref.session_id))
    with pytest.raises(CoreError):
        await sessions.close(session.ref, 7, NOW)
    closed = await sessions.close(session.ref, 0, NOW)
    assert closed.status is SessionStatus.CLOSED
    assert await sessions.close(session.ref, 0, NOW) is closed
    with pytest.raises(CoreError, match="closed"):
        await sessions.require_open(session.ref)

    workspace = WorkspaceState(WorkspaceId("workspace-1"), root_scope())
    updated = workspace.update(
        0, {"phase": "started"}, artifact_refs=(ArtifactId("a"),)
    )
    assert updated.state_version == 1
    assert updated.values["phase"] == "started"
    cleared = updated.update(1, {}, artifact_refs=())
    assert cleared.artifact_refs == ()
    with pytest.raises(TypeError):
        updated.values["phase"] = "changed"  # type: ignore[index]
    with pytest.raises(CoreError):
        updated.update(0, {})
