"""Runtime call context propagated to every external capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .control import CancellationToken, Clock, Deadline, TraceContext
from .errors import ErrorCategory, core_error
from .ids import IdempotencyKey, RunId, SessionId, WorkspaceId
from .json_types import as_object
from .scope import ScopeRef


@dataclass(frozen=True, slots=True)
class SessionRef:
    namespace: str
    session_id: SessionId

    def __post_init__(self) -> None:
        if not self.namespace or self.namespace != self.namespace.strip():
            raise ValueError("session namespace must be non-empty and trimmed")

    def to_data(self) -> dict[str, str]:
        return {"namespace": self.namespace, "session_id": self.session_id.value}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SessionRef:
        return cls(
            namespace=str(data["namespace"]),
            session_id=SessionId(str(data["session_id"])),
        )


@dataclass(frozen=True, slots=True)
class RuntimeCallContext:
    run_id: RunId
    root_run_id: RunId
    parent_run_id: RunId | None
    workspace_id: WorkspaceId
    session_ref: SessionRef | None
    scope: ScopeRef
    deadline: Deadline | None
    cancellation: CancellationToken
    trace: TraceContext
    idempotency_key: IdempotencyKey | None = None

    def check_active(self, clock: Clock) -> None:
        self.cancellation.raise_if_cancelled()
        if self.deadline:
            self.deadline.raise_if_expired(clock)

    def narrow(
        self,
        *,
        scope: ScopeRef | None = None,
        deadline: Deadline | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> RuntimeCallContext:
        effective_scope = scope or self.scope
        effective_scope.require_narrower_than(self.scope)
        effective_deadline = deadline or self.deadline
        if (
            self.deadline
            and effective_deadline
            and effective_deadline.at > self.deadline.at
        ):
            raise core_error(
                ErrorCategory.DENIED,
                "deadline_broadening_denied",
                "child calls may not extend their deadline",
            )
        return RuntimeCallContext(
            run_id=self.run_id,
            root_run_id=self.root_run_id,
            parent_run_id=self.parent_run_id,
            workspace_id=self.workspace_id,
            session_ref=self.session_ref,
            scope=effective_scope,
            deadline=effective_deadline,
            cancellation=self.cancellation,
            trace=self.trace.child(),
            idempotency_key=idempotency_key or self.idempotency_key,
        )

    def for_child_run(
        self,
        child_run_id: RunId,
        *,
        scope: ScopeRef | None = None,
        deadline: Deadline | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> RuntimeCallContext:
        narrowed = self.narrow(
            scope=scope,
            deadline=deadline,
            idempotency_key=idempotency_key,
        )
        return RuntimeCallContext(
            run_id=child_run_id,
            root_run_id=self.root_run_id,
            parent_run_id=self.run_id,
            workspace_id=self.workspace_id,
            session_ref=self.session_ref,
            scope=narrowed.scope,
            deadline=narrowed.deadline,
            cancellation=self.cancellation,
            trace=narrowed.trace,
            idempotency_key=narrowed.idempotency_key,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "run_id": self.run_id.value,
            "root_run_id": self.root_run_id.value,
            "parent_run_id": self.parent_run_id.value if self.parent_run_id else None,
            "workspace_id": self.workspace_id.value,
            "session_ref": self.session_ref.to_data() if self.session_ref else None,
            "scope": self.scope.to_data(),
            "deadline": self.deadline.at.isoformat() if self.deadline else None,
            "cancellation": {
                "token_id": self.cancellation.token_id.value,
                "cancelled": self.cancellation.cancelled,
            },
            "trace": {
                "trace_id": self.trace.trace_id.value,
                "span_id": self.trace.span_id.value,
                "correlation_id": self.trace.correlation_id.value,
                "causation_id": (
                    self.trace.causation_id.value if self.trace.causation_id else None
                ),
            },
            "idempotency_key": (
                self.idempotency_key.value if self.idempotency_key else None
            ),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> RuntimeCallContext:
        from .ids import (
            CancellationId,
            CausationId,
            CorrelationId,
            SpanId,
            TraceId,
        )

        raw_scope = as_object(data["scope"], "scope")
        raw_trace = as_object(data["trace"], "trace")
        raw_cancellation = as_object(data["cancellation"], "cancellation")
        raw_session = data.get("session_ref")
        session_data = (
            as_object(raw_session, "session_ref") if raw_session is not None else None
        )
        raw_deadline = data.get("deadline")
        return cls(
            run_id=RunId(str(data["run_id"])),
            root_run_id=RunId(str(data["root_run_id"])),
            parent_run_id=(
                RunId(str(data["parent_run_id"])) if data.get("parent_run_id") else None
            ),
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            session_ref=(
                SessionRef.from_data(session_data) if session_data is not None else None
            ),
            scope=ScopeRef.from_data(raw_scope),
            deadline=(
                Deadline(datetime.fromisoformat(str(raw_deadline)))
                if raw_deadline
                else None
            ),
            cancellation=CancellationToken(
                CancellationId(str(raw_cancellation["token_id"])),
                cancelled=bool(raw_cancellation.get("cancelled", False)),
            ),
            trace=TraceContext(
                trace_id=TraceId(str(raw_trace["trace_id"])),
                span_id=SpanId(str(raw_trace["span_id"])),
                correlation_id=CorrelationId(str(raw_trace["correlation_id"])),
                causation_id=(
                    CausationId(str(raw_trace["causation_id"]))
                    if raw_trace.get("causation_id")
                    else None
                ),
            ),
            idempotency_key=(
                IdempotencyKey(str(data["idempotency_key"]))
                if data.get("idempotency_key")
                else None
            ),
        )
