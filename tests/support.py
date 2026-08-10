"""Shared deterministic test collaborators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from congeries_core.policy.authorization import (
    AccessRequest,
    Grant,
    PolicyDecision,
)
from congeries_core.runtime.context import RuntimeCallContext, SessionRef
from congeries_core.runtime.control import CancellationToken, TraceContext
from congeries_core.runtime.ids import (
    AgentId,
    DefinitionId,
    IdempotencyKey,
    ModelBindingRef,
    RunId,
    SessionId,
    WorkspaceId,
)
from congeries_core.runtime.run import AgentRun, create_root_agent_run
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FixedClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float = 1) -> None:
        self.value += timedelta(seconds=seconds)


class MatchingAllowPolicy:
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints={},
                issued_at=NOW,
                expires_at=None,
                policy_version="test-1",
                audit_correlation="audit-test",
            )
        )


class DenyingPolicy:
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        del request
        return PolicyDecision.deny("test_denied")


def root_scope() -> ScopeRef:
    return ScopeRef.core(CoreScopeKind.WORKSPACE, "workspace-1")


def child_scope(parent: ScopeRef | None = None) -> ScopeRef:
    return ScopeRef.core(CoreScopeKind.RUN, "run-scope", parent or root_scope())


def session_ref() -> SessionRef:
    return SessionRef("test", SessionId("session-1"))


def agent_run(*, scope: ScopeRef | None = None) -> AgentRun:
    return create_root_agent_run(
        definition_id=DefinitionId("agent-definition"),
        agent_id=AgentId("agent-1"),
        model_binding_ref=ModelBindingRef("model-1"),
        workspace_id=WorkspaceId("workspace-1"),
        scope=scope or root_scope(),
        created_at=NOW,
        session_ref=session_ref(),
    )


def call_context(
    *,
    run_id: RunId | None = None,
    scope: ScopeRef | None = None,
) -> RuntimeCallContext:
    actual_run = run_id or RunId("run-1")
    return RuntimeCallContext(
        run_id=actual_run,
        root_run_id=actual_run,
        parent_run_id=None,
        workspace_id=WorkspaceId("workspace-1"),
        session_ref=session_ref(),
        scope=scope or root_scope(),
        deadline=None,
        cancellation=CancellationToken(),
        trace=TraceContext.new(),
        idempotency_key=IdempotencyKey("idempotency-1"),
    )
