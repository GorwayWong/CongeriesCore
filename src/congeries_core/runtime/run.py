"""Common Run envelope and pure lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from .context import SessionRef
from .control import require_utc
from .errors import ErrorCategory, ErrorDetail, core_error
from .ids import (
    AgentId,
    CheckpointRef,
    DefinitionId,
    ModelBindingRef,
    RunId,
    WorkflowId,
    WorkspaceId,
)
from .json_types import as_array, as_int, as_object
from .scope import ScopeRef


class RunKind(StrEnum):
    AGENT = "agent"
    WORKFLOW = "workflow"


class RunStatus(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    CONTEXT_LOADING = "context_loading"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RETRYING = "retrying"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    FINAL_FAILURE = "final_failure"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AuditFailureMode(StrEnum):
    PAUSE = "pause"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RunControlPolicy:
    audit_failure_mode: AuditFailureMode = AuditFailureMode.PAUSE

    def to_data(self) -> dict[str, str]:
        return {"audit_failure_mode": self.audit_failure_mode.value}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> RunControlPolicy:
        return cls(audit_failure_mode=AuditFailureMode(str(data["audit_failure_mode"])))


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    detail: ErrorDetail
    attempt: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("error attempt must be positive")
        object.__setattr__(
            self, "occurred_at", require_utc(self.occurred_at, "occurred_at")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "detail": self.detail.to_data(),
            "attempt": self.attempt,
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ErrorSummary:
        detail = as_object(data["detail"], "error detail")
        return cls(
            detail=ErrorDetail.from_data(detail),
            attempt=as_int(data["attempt"], "error attempt"),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
        )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: int
    started_at: datetime
    ended_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    checkpoint_ref: CheckpointRef | None = None
    error: ErrorSummary | None = None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        object.__setattr__(
            self, "started_at", require_utc(self.started_at, "attempt started_at")
        )
        if self.ended_at:
            object.__setattr__(
                self, "ended_at", require_utc(self.ended_at, "attempt ended_at")
            )
        if (self.ended_at is None) != (self.outcome is None):
            raise ValueError("attempt end time and outcome must be set together")
        if self.ended_at and self.ended_at < self.started_at:
            raise ValueError("attempt cannot end before it starts")

    @property
    def open(self) -> bool:
        return self.ended_at is None

    def close(
        self,
        outcome: AttemptOutcome,
        now: datetime,
        error: ErrorSummary | None = None,
    ) -> AttemptRecord:
        if not self.open:
            raise ValueError("attempt is already closed")
        return replace(self, ended_at=now, outcome=outcome, error=error)

    def to_data(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "outcome": self.outcome.value if self.outcome else None,
            "checkpoint_ref": (
                self.checkpoint_ref.value if self.checkpoint_ref else None
            ),
            "error": self.error.to_data() if self.error else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> AttemptRecord:
        raw_error = data.get("error")
        error_data = (
            as_object(raw_error, "attempt error") if raw_error is not None else None
        )
        return cls(
            attempt=as_int(data["attempt"], "attempt"),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            ended_at=(
                datetime.fromisoformat(str(data["ended_at"]))
                if data.get("ended_at")
                else None
            ),
            outcome=(
                AttemptOutcome(str(data["outcome"])) if data.get("outcome") else None
            ),
            checkpoint_ref=(
                CheckpointRef(str(data["checkpoint_ref"]))
                if data.get("checkpoint_ref")
                else None
            ),
            error=(
                ErrorSummary.from_data(error_data) if error_data is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class _DecodedRunFields:
    run_id: RunId
    kind: RunKind
    definition_id: DefinitionId
    root_run_id: RunId
    parent_run_id: RunId | None
    workspace_id: WorkspaceId
    session_ref: SessionRef | None
    scope: ScopeRef
    status: RunStatus
    attempt: int
    state_version: int
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime | None
    ended_at: datetime | None
    error_summary: ErrorSummary | None
    continuation_status: RunStatus | None
    attempt_history: tuple[AttemptRecord, ...]
    control_policy: RunControlPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    run_id: RunId
    kind: RunKind
    definition_id: DefinitionId
    root_run_id: RunId
    parent_run_id: RunId | None
    workspace_id: WorkspaceId
    session_ref: SessionRef | None
    scope: ScopeRef
    status: RunStatus
    attempt: int
    state_version: int
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime | None = None
    ended_at: datetime | None = None
    error_summary: ErrorSummary | None = None
    continuation_status: RunStatus | None = None
    attempt_history: tuple[AttemptRecord, ...] = field(default_factory=tuple)
    control_policy: RunControlPolicy = field(default_factory=RunControlPolicy)

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("run attempt must be positive")
        if self.state_version < 0:
            raise ValueError("state version must not be negative")
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, "created_at")
        )
        for name in ("started_at", "updated_at", "ended_at"):
            value = getattr(self, name)
            if value:
                object.__setattr__(self, name, require_utc(value, name))
        if self.parent_run_id is None and self.root_run_id != self.run_id:
            raise ValueError("root Run must reference itself as root_run_id")
        if self.parent_run_id is not None and self.parent_run_id == self.run_id:
            raise ValueError("Run cannot be its own parent")
        if self.status.terminal and self.ended_at is None:
            raise ValueError("terminal Run requires ended_at")
        if not self.status.terminal and self.ended_at is not None:
            raise ValueError("non-terminal Run cannot have ended_at")
        history_attempts = tuple(record.attempt for record in self.attempt_history)
        if history_attempts != tuple(sorted(set(history_attempts))):
            raise ValueError("attempt history must increase without duplicates")
        if history_attempts and history_attempts[-1] > self.attempt:
            raise ValueError("attempt history cannot exceed the current attempt")
        open_attempts = tuple(
            index for index, record in enumerate(self.attempt_history) if record.open
        )
        if open_attempts and (
            open_attempts != (len(self.attempt_history) - 1,)
            or self.attempt_history[-1].attempt != self.attempt
        ):
            raise ValueError("only the current final attempt record may be open")
        if self.status in {RunStatus.PAUSED, RunStatus.RETRYING}:
            if self.continuation_status is None:
                raise ValueError("paused or retrying Run requires continuation_status")
            allowed_continuations = {
                RunStatus.STARTING,
                RunStatus.CONTEXT_LOADING,
                RunStatus.RUNNING,
            }
            if self.status is RunStatus.PAUSED:
                allowed_continuations.add(RunStatus.WAITING_APPROVAL)
            if self.continuation_status not in allowed_continuations:
                raise ValueError("continuation_status is not a resumable phase")
        elif self.continuation_status is not None:
            raise ValueError(
                "continuation_status is valid only while paused or retrying"
            )

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "run_id": self.run_id.value,
            "kind": self.kind.value,
            "definition_id": self.definition_id.value,
            "root_run_id": self.root_run_id.value,
            "parent_run_id": self.parent_run_id.value if self.parent_run_id else None,
            "workspace_id": self.workspace_id.value,
            "session_ref": self.session_ref.to_data() if self.session_ref else None,
            "scope": self.scope.to_data(),
            "status": self.status.value,
            "attempt": self.attempt,
            "state_version": self.state_version,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "error_summary": (
                self.error_summary.to_data() if self.error_summary else None
            ),
            "continuation_status": (
                self.continuation_status.value if self.continuation_status else None
            ),
            "attempt_history": [item.to_data() for item in self.attempt_history],
            "control_policy": self.control_policy.to_data(),
        }
        if isinstance(self, AgentRun):
            data.update(
                agent_id=self.agent_id.value,
                model_binding_ref=self.model_binding_ref.value,
            )
        elif isinstance(self, WorkflowRun):
            data.update(
                workflow_id=self.workflow_id.value,
                graph_version=self.graph_version,
                latest_checkpoint_ref=(
                    self.latest_checkpoint_ref.value
                    if self.latest_checkpoint_ref
                    else None
                ),
            )
        return data

    @classmethod
    def from_data(cls, data: dict[str, object]) -> Run:
        raw_scope = as_object(data["scope"], "run scope")
        raw_session = data.get("session_ref")
        raw_error = data.get("error_summary")
        raw_policy = as_object(data.get("control_policy", {}), "run control_policy")
        raw_history = as_array(data.get("attempt_history", []), "run attempt_history")
        session_data = (
            as_object(raw_session, "run session_ref")
            if raw_session is not None
            else None
        )
        error_data = (
            as_object(raw_error, "run error_summary") if raw_error is not None else None
        )
        history_data = tuple(
            as_object(item, "run attempt history item") for item in raw_history
        )
        common = _DecodedRunFields(
            run_id=RunId(str(data["run_id"])),
            kind=RunKind(str(data["kind"])),
            definition_id=DefinitionId(str(data["definition_id"])),
            root_run_id=RunId(str(data["root_run_id"])),
            parent_run_id=(
                RunId(str(data["parent_run_id"])) if data.get("parent_run_id") else None
            ),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            session_ref=(
                SessionRef.from_data(session_data) if session_data is not None else None
            ),
            scope=ScopeRef.from_data(raw_scope),
            status=RunStatus(str(data["status"])),
            attempt=as_int(data["attempt"], "run attempt"),
            state_version=as_int(data["state_version"], "run state version"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            started_at=(
                datetime.fromisoformat(str(data["started_at"]))
                if data.get("started_at")
                else None
            ),
            updated_at=(
                datetime.fromisoformat(str(data["updated_at"]))
                if data.get("updated_at")
                else None
            ),
            ended_at=(
                datetime.fromisoformat(str(data["ended_at"]))
                if data.get("ended_at")
                else None
            ),
            error_summary=(
                ErrorSummary.from_data(error_data) if error_data is not None else None
            ),
            continuation_status=(
                RunStatus(str(data["continuation_status"]))
                if data.get("continuation_status")
                else None
            ),
            attempt_history=tuple(
                AttemptRecord.from_data(item) for item in history_data
            ),
            control_policy=(
                RunControlPolicy.from_data(raw_policy)
                if raw_policy
                else RunControlPolicy()
            ),
        )
        if common.kind is RunKind.AGENT:
            return AgentRun(
                run_id=common.run_id,
                kind=common.kind,
                definition_id=common.definition_id,
                root_run_id=common.root_run_id,
                parent_run_id=common.parent_run_id,
                workspace_id=common.workspace_id,
                session_ref=common.session_ref,
                scope=common.scope,
                status=common.status,
                attempt=common.attempt,
                state_version=common.state_version,
                created_at=common.created_at,
                started_at=common.started_at,
                updated_at=common.updated_at,
                ended_at=common.ended_at,
                error_summary=common.error_summary,
                continuation_status=common.continuation_status,
                attempt_history=common.attempt_history,
                control_policy=common.control_policy,
                agent_id=AgentId(str(data["agent_id"])),
                model_binding_ref=ModelBindingRef(str(data["model_binding_ref"])),
            )
        return WorkflowRun(
            run_id=common.run_id,
            kind=common.kind,
            definition_id=common.definition_id,
            root_run_id=common.root_run_id,
            parent_run_id=common.parent_run_id,
            workspace_id=common.workspace_id,
            session_ref=common.session_ref,
            scope=common.scope,
            status=common.status,
            attempt=common.attempt,
            state_version=common.state_version,
            created_at=common.created_at,
            started_at=common.started_at,
            updated_at=common.updated_at,
            ended_at=common.ended_at,
            error_summary=common.error_summary,
            continuation_status=common.continuation_status,
            attempt_history=common.attempt_history,
            control_policy=common.control_policy,
            workflow_id=WorkflowId(str(data["workflow_id"])),
            graph_version=str(data["graph_version"]),
            latest_checkpoint_ref=(
                CheckpointRef(str(data["latest_checkpoint_ref"]))
                if data.get("latest_checkpoint_ref")
                else None
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentRun(Run):
    agent_id: AgentId
    model_binding_ref: ModelBindingRef

    def __post_init__(self) -> None:
        super(AgentRun, self).__post_init__()
        if self.kind is not RunKind.AGENT:
            raise ValueError("AgentRun kind must be agent")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowRun(Run):
    workflow_id: WorkflowId
    graph_version: str
    latest_checkpoint_ref: CheckpointRef | None = None

    def __post_init__(self) -> None:
        super(WorkflowRun, self).__post_init__()
        if self.kind is not RunKind.WORKFLOW:
            raise ValueError("WorkflowRun kind must be workflow")
        if not self.graph_version:
            raise ValueError("WorkflowRun graph_version is required")


def create_root_agent_run(
    *,
    definition_id: DefinitionId,
    agent_id: AgentId,
    model_binding_ref: ModelBindingRef,
    workspace_id: WorkspaceId,
    scope: ScopeRef,
    created_at: datetime,
    session_ref: SessionRef | None = None,
    control_policy: RunControlPolicy | None = None,
) -> AgentRun:
    run_id = RunId.new()
    return AgentRun(
        run_id=run_id,
        kind=RunKind.AGENT,
        definition_id=definition_id,
        root_run_id=run_id,
        parent_run_id=None,
        workspace_id=workspace_id,
        session_ref=session_ref,
        scope=scope,
        status=RunStatus.CREATED,
        attempt=1,
        state_version=0,
        created_at=created_at,
        updated_at=created_at,
        control_policy=control_policy or RunControlPolicy(),
        agent_id=agent_id,
        model_binding_ref=model_binding_ref,
    )


def create_root_workflow_run(
    *,
    definition_id: DefinitionId,
    workflow_id: WorkflowId,
    graph_version: str,
    workspace_id: WorkspaceId,
    scope: ScopeRef,
    created_at: datetime,
    session_ref: SessionRef | None = None,
    control_policy: RunControlPolicy | None = None,
) -> WorkflowRun:
    run_id = RunId.new()
    return WorkflowRun(
        run_id=run_id,
        kind=RunKind.WORKFLOW,
        definition_id=definition_id,
        root_run_id=run_id,
        parent_run_id=None,
        workspace_id=workspace_id,
        session_ref=session_ref,
        scope=scope,
        status=RunStatus.CREATED,
        attempt=1,
        state_version=0,
        created_at=created_at,
        updated_at=created_at,
        control_policy=control_policy or RunControlPolicy(),
        workflow_id=workflow_id,
        graph_version=graph_version,
    )


def create_child_agent_run(
    parent: WorkflowRun,
    *,
    definition_id: DefinitionId,
    agent_id: AgentId,
    model_binding_ref: ModelBindingRef,
    scope: ScopeRef,
    created_at: datetime,
) -> AgentRun:
    if parent.status.terminal:
        raise core_error(
            ErrorCategory.CONFLICT,
            "terminal_parent",
            "a terminal WorkflowRun cannot create a child AgentRun",
        )
    scope.require_narrower_than(parent.scope)
    run_id = RunId.new()
    return AgentRun(
        run_id=run_id,
        kind=RunKind.AGENT,
        definition_id=definition_id,
        root_run_id=parent.root_run_id,
        parent_run_id=parent.run_id,
        workspace_id=parent.workspace_id,
        session_ref=parent.session_ref,
        scope=scope,
        status=RunStatus.CREATED,
        attempt=1,
        state_version=0,
        created_at=created_at,
        updated_at=created_at,
        control_policy=parent.control_policy,
        agent_id=agent_id,
        model_binding_ref=model_binding_ref,
    )


@dataclass(frozen=True, slots=True)
class RunTransition:
    previous: Run
    current: Run
    reason: str


class RunStateMachine:
    """Pure state transition rules; persistence and publication live elsewhere."""

    _ADVANCE: ClassVar[dict[RunStatus, frozenset[RunStatus]]] = {
        RunStatus.CREATED: frozenset({RunStatus.STARTING}),
        RunStatus.STARTING: frozenset({RunStatus.CONTEXT_LOADING}),
        RunStatus.CONTEXT_LOADING: frozenset({RunStatus.RUNNING}),
        RunStatus.RUNNING: frozenset(
            {RunStatus.WAITING_APPROVAL, RunStatus.RECOVERING}
        ),
        RunStatus.WAITING_APPROVAL: frozenset({RunStatus.RUNNING}),
        RunStatus.RECOVERING: frozenset({RunStatus.RUNNING}),
    }

    def _check_version(self, run: Run, expected_version: int) -> None:
        if run.state_version != expected_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_state_version",
                "Run state version does not match the expected version",
                retryable=True,
            )

    def _replace(self, run: Run, now: datetime, **changes: object) -> Run:
        return replace(
            run,
            state_version=run.state_version + 1,
            updated_at=now,
            **changes,
        )

    def _open_attempt(self, run: Run, now: datetime) -> tuple[AttemptRecord, ...]:
        if run.attempt_history and run.attempt_history[-1].open:
            return run.attempt_history
        return (*run.attempt_history, AttemptRecord(run.attempt, now))

    def _close_attempt(
        self,
        run: Run,
        now: datetime,
        outcome: AttemptOutcome,
        error: ErrorSummary | None = None,
    ) -> tuple[AttemptRecord, ...]:
        history = run.attempt_history
        if not history or not history[-1].open:
            history = (*history, AttemptRecord(run.attempt, run.started_at or now))
        return (*history[:-1], history[-1].close(outcome, now, error))

    def start(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is RunStatus.STARTING:
            return RunTransition(run, run, "start_idempotent")
        if run.status is not RunStatus.CREATED:
            self._illegal(run, RunStatus.STARTING)
        current = self._replace(
            run,
            now,
            status=RunStatus.STARTING,
            started_at=run.started_at or now,
            attempt_history=self._open_attempt(run, now),
        )
        return RunTransition(run, current, "start")

    def advance(
        self,
        run: Run,
        target: RunStatus,
        expected_version: int,
        now: datetime,
        *,
        reason: str = "advance",
    ) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is target:
            return RunTransition(run, run, f"{reason}_idempotent")
        if target not in self._ADVANCE.get(run.status, frozenset()):
            self._illegal(run, target)
        changes: dict[str, object] = {"status": target}
        if run.status is RunStatus.RECOVERING and target is RunStatus.RUNNING:
            changes["attempt_history"] = self._open_attempt(run, now)
        current = self._replace(run, now, **changes)
        return RunTransition(run, current, reason)

    def pause(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is RunStatus.PAUSED:
            return RunTransition(run, run, "pause_idempotent")
        allowed = {
            RunStatus.STARTING,
            RunStatus.CONTEXT_LOADING,
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.RETRYING,
            RunStatus.RECOVERING,
        }
        if run.status not in allowed:
            self._illegal(run, RunStatus.PAUSED)
        continuation = run.continuation_status
        if run.status is RunStatus.RECOVERING:
            continuation = RunStatus.RUNNING
        elif run.status is not RunStatus.RETRYING:
            continuation = run.status
        current = self._replace(
            run,
            now,
            status=RunStatus.PAUSED,
            continuation_status=continuation,
        )
        return RunTransition(run, current, "pause")

    def resume(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is not RunStatus.PAUSED or run.continuation_status is None:
            self._illegal(run, RunStatus.RUNNING)
        current = self._replace(
            run,
            now,
            status=run.continuation_status,
            continuation_status=None,
            attempt_history=self._open_attempt(run, now),
        )
        return RunTransition(run, current, "resume")

    def retry(
        self,
        run: Run,
        expected_version: int,
        now: datetime,
        error: ErrorDetail,
    ) -> RunTransition:
        self._check_version(run, expected_version)
        if not error.retryable:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "non_retryable_error",
                "retry requires a retryable error",
            )
        if run.status not in {
            RunStatus.STARTING,
            RunStatus.CONTEXT_LOADING,
            RunStatus.RUNNING,
        }:
            self._illegal(run, RunStatus.RETRYING)
        summary = ErrorSummary(error, run.attempt, now)
        current = self._replace(
            run,
            now,
            status=RunStatus.RETRYING,
            attempt=run.attempt + 1,
            continuation_status=run.status,
            error_summary=summary,
            attempt_history=self._close_attempt(
                run, now, AttemptOutcome.RETRYABLE_FAILURE, summary
            ),
        )
        return RunTransition(run, current, "retry")

    def redispatch_retry(
        self, run: Run, expected_version: int, now: datetime
    ) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is not RunStatus.RETRYING or run.continuation_status is None:
            self._illegal(run, RunStatus.RUNNING)
        current = self._replace(
            run,
            now,
            status=run.continuation_status,
            continuation_status=None,
            attempt_history=self._open_attempt(run, now),
        )
        return RunTransition(run, current, "retry_redispatch")

    def recover(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status.terminal or run.status is RunStatus.CREATED:
            self._illegal(run, RunStatus.RECOVERING)
        history = self._close_attempt(run, now, AttemptOutcome.INTERRUPTED)
        current = self._replace(
            run,
            now,
            status=RunStatus.RECOVERING,
            attempt=run.attempt + 1,
            continuation_status=None,
            attempt_history=history,
        )
        return RunTransition(run, current, "recover")

    def complete(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is RunStatus.SUCCEEDED:
            return RunTransition(run, run, "complete_idempotent")
        if run.status is not RunStatus.RUNNING:
            self._illegal(run, RunStatus.SUCCEEDED)
        current = self._replace(
            run,
            now,
            status=RunStatus.SUCCEEDED,
            ended_at=now,
            attempt_history=self._close_attempt(run, now, AttemptOutcome.SUCCEEDED),
        )
        return RunTransition(run, current, "complete")

    def fail(
        self,
        run: Run,
        expected_version: int,
        now: datetime,
        error: ErrorDetail,
    ) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is RunStatus.FAILED:
            return RunTransition(run, run, "fail_idempotent")
        if run.status.terminal:
            self._illegal(run, RunStatus.FAILED)
        summary = ErrorSummary(error, run.attempt, now)
        current = self._replace(
            run,
            now,
            status=RunStatus.FAILED,
            ended_at=now,
            continuation_status=None,
            error_summary=summary,
            attempt_history=self._close_attempt(
                run, now, AttemptOutcome.FINAL_FAILURE, summary
            ),
        )
        return RunTransition(run, current, "fail")

    def cancel(self, run: Run, expected_version: int, now: datetime) -> RunTransition:
        self._check_version(run, expected_version)
        if run.status is RunStatus.CANCELLED:
            return RunTransition(run, run, "cancel_idempotent")
        if run.status.terminal:
            self._illegal(run, RunStatus.CANCELLED)
        current = self._replace(
            run,
            now,
            status=RunStatus.CANCELLED,
            ended_at=now,
            continuation_status=None,
            attempt_history=self._close_attempt(run, now, AttemptOutcome.CANCELLED),
        )
        return RunTransition(run, current, "cancel")

    def _illegal(self, run: Run, target: RunStatus) -> None:
        raise core_error(
            ErrorCategory.CONFLICT,
            "illegal_run_transition",
            f"cannot transition Run from {run.status.value} to {target.value}",
        )
