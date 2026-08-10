"""Runtime and authorization Event publishers backed by EventDispatcher."""

from __future__ import annotations

from congeries_core.policy.authorization import (
    AccessRequest,
    CorePrincipalKind,
    Grant,
    PolicyDecision,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, TraceContext
from congeries_core.runtime.ids import PrincipalId
from congeries_core.runtime.run import RunTransition
from congeries_core.state.service import RunEventPublisher

from .dispatcher import EventDispatcher
from .model import (
    CoreEventType,
    DeliveryClass,
    PayloadField,
    Sensitivity,
)


class RuntimeEventPublisher(RunEventPublisher):
    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def run_state_changed(self, transition: RunTransition) -> None:
        run = transition.current
        context = RuntimeCallContext(
            run_id=run.run_id,
            root_run_id=run.root_run_id,
            parent_run_id=run.parent_run_id,
            workspace_id=run.workspace_id,
            session_ref=run.session_ref,
            scope=run.scope,
            deadline=None,
            cancellation=CancellationToken(),
            trace=TraceContext.new(),
        )
        event = await self._dispatcher.create_event(
            event_type=CoreEventType.RUN_STATE_CHANGED.value,
            schema_version="1",
            run_id=run.run_id,
            root_run_id=run.root_run_id,
            parent_run_id=run.parent_run_id,
            scope=run.scope,
            context=context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=DeliveryClass.OBSERVABILITY,
            payload={
                "previous_status": PayloadField(transition.previous.status.value),
                "new_status": PayloadField(run.status.value),
                "attempt": PayloadField(run.attempt),
                "reason": PayloadField(transition.reason),
                "state_version": PayloadField(run.state_version),
            },
        )
        principal = RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(run.run_id.value)
        )
        await self._dispatcher.publish(event, context, principal)

    async def authorization_denied(
        self, request: AccessRequest, decision: PolicyDecision
    ) -> None:
        event = await self._dispatcher.create_event(
            event_type=CoreEventType.AUTHORIZATION_DENIED.value,
            schema_version="1",
            run_id=request.context.run_id,
            root_run_id=request.context.root_run_id,
            parent_run_id=request.context.parent_run_id,
            scope=request.context.scope,
            context=request.context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=DeliveryClass.AUDIT,
            payload={
                "principal": PayloadField(request.principal.id.value),
                "action": PayloadField(".".join(request.action.key)),
                "resource": PayloadField(":".join(request.resource.key)),
                "reason_code": PayloadField(decision.reason_code or "denied"),
                "policy_effect": PayloadField(decision.effect.value),
            },
        )
        await self._dispatcher.publish(event, request.context, request.principal)

    async def cross_scope_granted(self, request: AccessRequest, grant: Grant) -> None:
        event = await self._dispatcher.create_event(
            event_type=CoreEventType.AUTHORIZATION_CROSS_SCOPE_GRANTED.value,
            schema_version="1",
            run_id=request.context.run_id,
            root_run_id=request.context.root_run_id,
            parent_run_id=request.context.parent_run_id,
            scope=grant.effective_scope,
            context=request.context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=DeliveryClass.AUDIT,
            payload={
                "principal": PayloadField(request.principal.id.value),
                "action": PayloadField(".".join(request.action.key)),
                "resource": PayloadField(":".join(request.resource.key)),
                "source_scope": PayloadField(":".join(grant.source_scope.key)),
                "destination_scope": PayloadField(":".join(grant.effective_scope.key)),
                "policy_version": PayloadField(grant.policy_version),
            },
        )
        await self._dispatcher.publish(event, request.context, request.principal)
