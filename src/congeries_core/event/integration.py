"""Runtime and authorization Event publishers backed by EventDispatcher."""

from __future__ import annotations

from collections.abc import Mapping

from congeries_core.checkpoint.model import (
    ApprovalDecision,
    ApprovalRequest,
    Checkpoint,
)
from congeries_core.policy.authorization import (
    AccessRequest,
    CorePrincipalKind,
    Grant,
    PolicyDecision,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, TraceContext
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import CheckpointRef, PrincipalId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.run import RunTransition, WorkflowRun
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

    async def provider_event(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        event = await self._dispatcher.create_event(
            event_type=event_type,
            schema_version="1",
            run_id=context.run_id,
            root_run_id=context.root_run_id,
            parent_run_id=context.parent_run_id,
            scope=context.scope,
            context=context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=DeliveryClass.OBSERVABILITY,
            payload={key: PayloadField(value) for key, value in payload.items()},
        )
        principal = RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
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

    async def checkpoint_saved(
        self, checkpoint: Checkpoint, run: WorkflowRun, context: RuntimeCallContext
    ) -> None:
        del run
        await self._checkpoint_event(
            CoreEventType.CHECKPOINT_SAVED,
            DeliveryClass.OBSERVABILITY,
            context,
            {
                "checkpoint_ref": checkpoint.ref.value,
                "sequence": checkpoint.sequence,
                "graph_version": checkpoint.graph_version,
                "outcome": "saved",
            },
        )

    async def checkpoint_failed(
        self, checkpoint: Checkpoint, error: ErrorDetail, context: RuntimeCallContext
    ) -> None:
        await self._checkpoint_event(
            CoreEventType.CHECKPOINT_FAILED,
            DeliveryClass.OBSERVABILITY,
            context,
            {
                "checkpoint_ref": checkpoint.ref.value,
                "sequence": checkpoint.sequence,
                "error_code": error.code,
                "category": error.category.value,
                "outcome": "failed",
            },
        )

    async def checkpoint_migration_authorized(
        self,
        source: Checkpoint,
        migrated: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        await self._checkpoint_event(
            CoreEventType.CHECKPOINT_MIGRATION_AUTHORIZED,
            DeliveryClass.AUDIT,
            context,
            {
                "source_checkpoint_ref": source.ref.value,
                "migrated_checkpoint_ref": migrated.ref.value,
                "source_graph_version": source.graph_version,
                "target_graph_version": migrated.graph_version,
            },
        )

    async def checkpoint_fallback_authorized(
        self,
        source_ref: CheckpointRef,
        fallback: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        await self._checkpoint_event(
            CoreEventType.CHECKPOINT_FALLBACK_AUTHORIZED,
            DeliveryClass.AUDIT,
            context,
            {
                "source_checkpoint_ref": source_ref.value,
                "fallback_checkpoint_ref": fallback.ref.value,
                "fallback_sequence": fallback.sequence,
            },
        )

    async def approval_requested(
        self, request: ApprovalRequest, context: RuntimeCallContext
    ) -> None:
        await self._checkpoint_event(
            CoreEventType.APPROVAL_REQUESTED,
            DeliveryClass.AUDIT,
            context,
            {
                "approval_id": request.approval_id.value,
                "node_id": request.node_id.value,
                "correlation_id": request.correlation_id.value,
                "outcome": "requested",
            },
        )

    async def approval_decided(
        self, decision: ApprovalDecision, context: RuntimeCallContext
    ) -> None:
        await self._checkpoint_event(
            CoreEventType.APPROVAL_DECIDED,
            DeliveryClass.AUDIT,
            context,
            {
                "approval_id": decision.approval_id.value,
                "node_id": decision.node_id.value,
                "correlation_id": decision.correlation_id.value,
                "actor": decision.actor.id.value,
                "outcome": decision.outcome.value,
            },
            principal=decision.actor,
        )

    async def _checkpoint_event(
        self,
        event_type: CoreEventType,
        delivery_class: DeliveryClass,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
        *,
        principal: RuntimePrincipal | None = None,
    ) -> None:
        event = await self._dispatcher.create_event(
            event_type=event_type.value,
            schema_version="1",
            run_id=context.run_id,
            root_run_id=context.root_run_id,
            parent_run_id=context.parent_run_id,
            scope=context.scope,
            context=context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=delivery_class,
            payload={key: PayloadField(value) for key, value in payload.items()},
        )
        actor = principal or RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
        )
        await self._dispatcher.publish(event, context, actor)
