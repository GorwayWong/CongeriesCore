"""Minimal durable approval coordination around Checkpoint boundaries."""

from __future__ import annotations

from congeries_core.policy.authorization import (
    AccessRequest,
    AuditFailureHandler,
    AuthorizedCall,
    AuthorizedDispatcher,
    ResourceRef,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import ProviderId, ResourceId, RunId
from congeries_core.runtime.run import RunStatus, WorkflowRun
from congeries_core.state.service import RunService

from .coordinator import (
    CheckpointCoordinator,
    CheckpointEventPublisher,
    NullCheckpointEventPublisher,
)
from .gateway import APPROVAL_DECIDE_ACTION, CheckpointGateway
from .model import (
    ApprovalCheckpointState,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Checkpoint,
)


class ApprovalCoordinator:
    def __init__(
        self,
        *,
        checkpoint_coordinator: CheckpointCoordinator,
        checkpoint_gateway: CheckpointGateway,
        runs: RunService,
        dispatcher: AuthorizedDispatcher[object],
        audit_failure_handler: AuditFailureHandler,
        clock: Clock,
        publisher: CheckpointEventPublisher | None = None,
    ) -> None:
        self._checkpoints = checkpoint_coordinator
        self._gateway = checkpoint_gateway
        self._runs = runs
        self._dispatcher = dispatcher
        self._audit_failure = audit_failure_handler
        self._clock = clock
        self._publisher = publisher or NullCheckpointEventPublisher()

    async def request_approval(
        self,
        provider_id: ProviderId,
        request: ApprovalRequest,
        checkpoint: Checkpoint,
        expected_run_version: int,
        context: RuntimeCallContext,
    ) -> WorkflowRun:
        run = await self._require_workflow(request.run_id)
        if run.status is not RunStatus.RUNNING:
            raise core_error(
                ErrorCategory.CONFLICT,
                "approval_request_state_invalid",
                "approval may be requested only while WorkflowRun is RUNNING",
            )
        self._validate_request(request, checkpoint, run, context)
        committed = await self._checkpoints.save(
            provider_id, checkpoint, expected_run_version, context
        )
        try:
            await self._publisher.approval_requested(request, context)
        except CoreError as error:
            await self._audit_failure.handle(run.run_id, error.detail)
            raise
        waiting = await self._runs.advance(
            run.run_id, committed.state_version, RunStatus.WAITING_APPROVAL
        )
        if not isinstance(waiting, WorkflowRun):
            raise AssertionError("approval wait transition did not return WorkflowRun")
        return waiting

    async def decide(
        self,
        provider_id: ProviderId,
        decision: ApprovalDecision,
        checkpoint: Checkpoint,
        expected_run_version: int,
        context: RuntimeCallContext,
    ) -> WorkflowRun:
        run = await self._require_workflow(decision.run_id)
        if run.latest_checkpoint_ref is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "approval_checkpoint_missing",
                "WorkflowRun has no approval checkpoint",
            )
        current = await self._gateway.load(
            provider_id,
            run.latest_checkpoint_ref,
            run.run_id,
            run.scope,
            context,
        )
        state = _find_approval(current, decision.approval_id.value)
        if state.decision is not None:
            if state.decision == decision:
                return run
            raise core_error(
                ErrorCategory.CONFLICT,
                "approval_decision_conflict",
                "approval already has a different durable decision",
            )
        if run.status is not RunStatus.WAITING_APPROVAL:
            raise core_error(
                ErrorCategory.CONFLICT,
                "approval_decision_state_invalid",
                "WorkflowRun is not waiting for approval",
            )
        if run.state_version != expected_run_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_state_version",
                "Run state version does not match the expected version",
                retryable=True,
            )
        self._validate_decision(decision, state.request, checkpoint, run, context)
        constraints = {
            "approval_id": decision.approval_id.value,
            "run_id": decision.run_id.value,
            "node_id": decision.node_id.value,
            "correlation_id": decision.correlation_id.value,
            "outcome": decision.outcome.value,
        }
        access = AccessRequest(
            principal=decision.actor,
            action=APPROVAL_DECIDE_ACTION,
            resource=ResourceRef(
                "core", "approval", ResourceId(decision.approval_id.value)
            ),
            scope=decision.scope,
            context=context,
            constraints=constraints,
        )

        async def operation(call: AuthorizedCall) -> WorkflowRun:
            if dict(call.grant.constraints) != constraints:
                raise core_error(
                    ErrorCategory.DENIED,
                    "invalid_grant",
                    "approval grant changed decision identity",
                )
            committed = await self._checkpoints.save(
                provider_id, checkpoint, expected_run_version, call.context
            )
            try:
                await self._publisher.approval_decided(decision, call.context)
            except CoreError as error:
                await self._audit_failure.handle(run.run_id, error.detail)
                raise
            if decision.outcome is ApprovalOutcome.APPROVED:
                result = await self._runs.advance(
                    run.run_id, committed.state_version, RunStatus.RUNNING
                )
            elif decision.outcome is ApprovalOutcome.REJECTED:
                result = await self._runs.fail(
                    run.run_id,
                    committed.state_version,
                    ErrorDetail(
                        ErrorCategory.DENIED,
                        "approval_rejected",
                        "approval decision rejected execution",
                    ),
                )
            else:
                result = await self._runs.cancel(run.run_id, committed.state_version)
            if not isinstance(result, WorkflowRun):
                raise AssertionError("approval decision did not return WorkflowRun")
            return result

        result = await self._dispatcher.dispatch(access, operation)
        if not isinstance(result, WorkflowRun):
            raise AssertionError("approval dispatcher result is invalid")
        return result

    async def _require_workflow(self, run_id: RunId) -> WorkflowRun:
        run = await self._runs.get(run_id)
        if not isinstance(run, WorkflowRun):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "approval_requires_workflow_run",
                "approval coordination requires a WorkflowRun",
            )
        return run

    def _validate_request(
        self,
        request: ApprovalRequest,
        checkpoint: Checkpoint,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> None:
        if (
            request.run_id != run.run_id
            or request.scope.key != run.scope.key
            or context.run_id != run.run_id
            or context.scope.key != run.scope.key
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "approval_request_identity_mismatch",
                "approval request does not match WorkflowRun ownership",
            )
        if request.expires_at is not None and self._clock.now() >= request.expires_at:
            raise core_error(
                ErrorCategory.DENIED,
                "approval_request_expired",
                "approval request has expired",
            )
        state = _find_approval(checkpoint, request.approval_id.value)
        if state.request != request or state.decision is not None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "approval_checkpoint_invalid",
                "pre-approval checkpoint does not contain the pending request",
            )

    def _validate_decision(
        self,
        decision: ApprovalDecision,
        request: ApprovalRequest,
        checkpoint: Checkpoint,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> None:
        request_identity = (
            request.approval_id,
            request.run_id,
            request.node_id,
            request.correlation_id,
        )
        decision_identity = (
            decision.approval_id,
            decision.run_id,
            decision.node_id,
            decision.correlation_id,
        )
        if request_identity != decision_identity:
            raise core_error(
                ErrorCategory.DENIED,
                "approval_identity_mismatch",
                "approval decision identity or correlation is invalid",
            )
        if (
            decision.scope.key != request.scope.key
            or run.scope.key != request.scope.key
            or context.run_id != run.run_id
        ):
            raise core_error(
                ErrorCategory.DENIED,
                "approval_scope_mismatch",
                "approval decision Scope does not match request",
            )
        if decision.outcome not in request.allowed_outcomes:
            raise core_error(
                ErrorCategory.DENIED,
                "approval_outcome_not_allowed",
                "approval outcome is not allowed by the request",
            )
        if request.expires_at is not None and decision.decided_at >= request.expires_at:
            raise core_error(
                ErrorCategory.DENIED,
                "approval_decision_expired",
                "approval decision was made after expiration",
            )
        state = _find_approval(checkpoint, decision.approval_id.value)
        if state.request != request or state.decision != decision:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "approval_checkpoint_invalid",
                "post-approval checkpoint does not contain the decision",
            )


def _find_approval(checkpoint: Checkpoint, approval_id: str) -> ApprovalCheckpointState:
    matches = [
        item
        for item in checkpoint.approvals
        if item.request.approval_id.value == approval_id
    ]
    if len(matches) != 1:
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "approval_not_found",
            "checkpoint does not contain the approval identity",
        )
    return matches[0]
