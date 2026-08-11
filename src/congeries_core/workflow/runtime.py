"""Minimal direct Workflow runtime with AgentNode and ApprovalNode support."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from congeries_core.checkpoint import (
    ApprovalCheckpointState,
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Checkpoint,
    CheckpointCoordinator,
    CheckpointGateway,
    CheckpointReference,
    NodeCheckpointState,
    NodeOutcome,
    RecoveryCoordinator,
    RecoveryRequest,
    SideEffectOutcome,
    SideEffectRecord,
)
from congeries_core.evaluation import (
    EVALUATION_CONTRACT_VERSION,
    EVALUATION_RESULT_SCHEMA,
    EvaluationHarness,
    EvaluationRequest,
    EvaluationResult,
    EvaluationVerdict,
)
from congeries_core.harness.agent import AgentExecutionResult, AgentRuntime
from congeries_core.policy.authorization import (
    AccessRequest,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.content import ContentBlock
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, Deadline
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import (
    ApprovalId,
    CheckpointRef,
    CorrelationId,
    EvaluationId,
    IdempotencyKey,
    NodeId,
    PrincipalId,
    ProviderId,
    ResourceId,
    RunId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_json_value,
    as_object,
)
from congeries_core.runtime.run import (
    AgentRun,
    RunStatus,
    WorkflowRun,
    create_child_agent_run,
)
from congeries_core.runtime.schema import SchemaRegistry
from congeries_core.state.service import RunService

from .model import (
    AgentNodeConfig,
    ApprovalNodeConfig,
    EvaluationNodeConfig,
    NodeOutputReference,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowExecutionOutcome,
    WorkflowInputSource,
    WorkflowNode,
    WorkflowNodeType,
    WorkflowResult,
    WorkflowSuspension,
)
from .persistence import (
    WORKFLOW_NODE_EXECUTE_ACTION,
    LoadNodeOutputRequest,
    NodeOutputPersistence,
    PersistNodeOutputRequest,
)
from .scheduler import DeterministicScheduler
from .validation import ValidatedWorkflow, WorkflowValidator


@dataclass(frozen=True, slots=True)
class _AgentDispatch:
    result: AgentExecutionResult
    value: JsonValue | None
    input_value: JsonValue
    node_context: RuntimeCallContext
    idempotency_key: IdempotencyKey


@dataclass(frozen=True, slots=True)
class _EvaluationDispatch:
    result: EvaluationResult
    input_value: JsonValue
    node_context: RuntimeCallContext
    idempotency_key: IdempotencyKey


class WorkflowRuntime:
    def __init__(
        self,
        *,
        validator: WorkflowValidator,
        runs: RunService,
        agents: AgentRuntime,
        dispatcher: AuthorizedDispatcher[object],
        outputs: NodeOutputPersistence,
        schemas: SchemaRegistry,
        checkpoints: CheckpointCoordinator,
        checkpoint_gateway: CheckpointGateway,
        checkpoint_provider_id: ProviderId,
        recovery: RecoveryCoordinator,
        approvals: ApprovalCoordinator,
        clock: Clock,
        evaluations: EvaluationHarness | None = None,
    ) -> None:
        self._validator = validator
        self._runs = runs
        self._agents = agents
        self._dispatcher = dispatcher
        self._outputs = outputs
        self._schemas = schemas
        self._checkpoints = checkpoints
        self._checkpoint_gateway = checkpoint_gateway
        self._checkpoint_provider_id = checkpoint_provider_id
        self._recovery = recovery
        self._approvals = approvals
        self._clock = clock
        self._evaluations = evaluations

    async def execute(
        self, definition: WorkflowDefinition, context: WorkflowContext
    ) -> WorkflowExecutionOutcome:
        try:
            validated = self._validator.validate(definition)
            self._schemas.validate(definition.input_schema, context.input)
        except CoreError as error:
            return await self._validation_failure(context.run_id, error.detail)

        run = await self._workflow_run(context.run_id)
        self._validate_identity(definition, run, context.runtime)
        if run.status is RunStatus.CREATED:
            run = await self._start(run)
            checkpoint = self._checkpoint(
                run,
                previous=None,
                node_states=(),
                pending_nodes=tuple(
                    sorted(
                        (item.node_id for item in definition.nodes),
                        key=lambda item: item.value,
                    )
                ),
                output_refs={},
                side_effects=(),
                approvals=(),
            )
            run = await self._checkpoints.save(
                self._checkpoint_provider_id,
                checkpoint,
                run.state_version,
                context.runtime,
            )
            return await self._drive(validated, run, context, checkpoint)
        if run.latest_checkpoint_ref is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_checkpoint_missing",
                "started WorkflowRun has no committed checkpoint",
            )
        checkpoint = await self._load_checkpoint(run, context.runtime)
        if run.status is RunStatus.WAITING_APPROVAL:
            return self._suspension(run, checkpoint)
        if run.status is not RunStatus.RUNNING:
            raise core_error(
                ErrorCategory.CONFLICT,
                "workflow_execution_state_invalid",
                "WorkflowRun cannot be executed from its current state",
            )
        return await self._drive(validated, run, context, checkpoint)

    async def recover(
        self,
        definition: WorkflowDefinition,
        context: WorkflowContext,
        request: RecoveryRequest,
    ) -> WorkflowExecutionOutcome:
        validated = self._validator.validate(definition)
        self._schemas.validate(definition.input_schema, context.input)
        if request.run_id != context.run_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_recovery_run_mismatch",
                "recovery request does not match Workflow context",
            )
        recovered = await self._recovery.recover(request, context.runtime)
        self._validate_identity(definition, recovered.run, context.runtime)
        return await self._drive(
            validated, recovered.run, context, recovered.checkpoint
        )

    async def decide_approval(
        self,
        definition: WorkflowDefinition,
        context: WorkflowContext,
        decision: ApprovalDecision,
    ) -> WorkflowExecutionOutcome:
        validated = self._validator.validate(definition)
        run = await self._workflow_run(context.run_id)
        self._validate_identity(definition, run, context.runtime)
        if run.status is not RunStatus.WAITING_APPROVAL:
            raise core_error(
                ErrorCategory.CONFLICT,
                "workflow_not_waiting_approval",
                "WorkflowRun is not waiting for an approval decision",
            )
        current = await self._load_checkpoint(run, context.runtime)
        approval_state = self._approval_state(current, decision.approval_id)
        decided_state = ApprovalCheckpointState(approval_state.request, decision)
        states = {
            item.node_id: item
            for item in current.node_states
            if item.node_id != decision.node_id
        }
        outcome = {
            ApprovalOutcome.APPROVED: NodeOutcome.SUCCEEDED,
            ApprovalOutcome.REJECTED: NodeOutcome.DENIED,
            ApprovalOutcome.CANCELLED: NodeOutcome.CANCELLED,
        }[decision.outcome]
        states[decision.node_id] = NodeCheckpointState(
            decision.node_id,
            outcome,
            approval_state=decided_state,
        )
        approvals = tuple(
            decided_state if item.request.approval_id == decision.approval_id else item
            for item in current.approvals
        )
        completed = {
            node_id
            for node_id, state in states.items()
            if state.outcome is NodeOutcome.SUCCEEDED
        }
        post_decision = self._checkpoint(
            run,
            previous=current,
            node_states=tuple(states.values()),
            pending_nodes=tuple(
                node_id
                for node_id in sorted(
                    (item.node_id for item in definition.nodes),
                    key=lambda item: item.value,
                )
                if node_id not in completed and node_id != decision.node_id
            ),
            output_refs=self._output_refs(definition, states),
            side_effects=current.side_effects,
            approvals=approvals,
        )
        decided_run = await self._approvals.decide(
            self._checkpoint_provider_id,
            decision,
            post_decision,
            run.state_version,
            context.runtime,
        )
        if decision.outcome is ApprovalOutcome.APPROVED:
            return await self._drive(validated, decided_run, context, post_decision)
        error = (
            ErrorDetail(
                ErrorCategory.CANCELLED,
                "approval_cancelled",
                "approval decision cancelled Workflow execution",
            )
            if decision.outcome is ApprovalOutcome.CANCELLED
            else ErrorDetail(
                ErrorCategory.DENIED,
                "approval_rejected",
                "approval decision rejected Workflow execution",
            )
        )
        return WorkflowExecutionOutcome(
            result=WorkflowResult(
                decided_run,
                error=error,
                output_refs=self._public_output_refs(definition, states),
                final_checkpoint_ref=post_decision.ref,
            )
        )

    async def _start(self, run: WorkflowRun) -> WorkflowRun:
        current = await self._runs.start(run.run_id, run.state_version)
        current = await self._runs.advance(
            run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
        )
        current = await self._runs.advance(
            run.run_id, current.state_version, RunStatus.RUNNING
        )
        if not isinstance(current, WorkflowRun):
            raise AssertionError("WorkflowRun changed kind while starting")
        return current

    async def _drive(
        self,
        workflow: ValidatedWorkflow,
        run: WorkflowRun,
        context: WorkflowContext,
        checkpoint: Checkpoint,
    ) -> WorkflowExecutionOutcome:
        waiting = self._pending_approval(checkpoint)
        if waiting is not None:
            if run.status is RunStatus.RUNNING:
                transitioned = await self._runs.advance(
                    run.run_id, run.state_version, RunStatus.WAITING_APPROVAL
                )
                if not isinstance(transitioned, WorkflowRun):
                    raise AssertionError(
                        "WorkflowRun changed kind while restoring approval"
                    )
                run = transitioned
            return WorkflowExecutionOutcome(
                suspension=WorkflowSuspension(run, checkpoint.ref, waiting.request)
            )

        states = {item.node_id: item for item in checkpoint.node_states}
        # A committed Evaluation failure is terminal state, not interrupted work.
        # Resolve it before constructing the scheduler so neither this node nor a
        # dependent node can be selected during recovery.
        stable_failure = await self._stable_evaluation_failure(
            workflow.definition, run, context.runtime, states
        )
        if stable_failure is not None:
            return await self._terminal_failure(
                run, stable_failure, workflow.definition, states
            )
        output_refs = self._output_refs(workflow.definition, states)
        completed = {
            item.node_id
            for item in checkpoint.node_states
            if item.outcome is NodeOutcome.SUCCEEDED
        }
        scheduler = DeterministicScheduler(workflow, completed)
        current_run = run
        current_checkpoint = checkpoint
        while not scheduler.done:
            node = scheduler.next()
            if node is None:
                detail = ErrorDetail(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "workflow_scheduler_stalled",
                    "Workflow scheduler has pending work but no ready node",
                )
                return await self._terminal_failure(
                    current_run, detail, workflow.definition, states
                )
            if node.node_type == WorkflowNodeType.APPROVAL.value:
                return await self._request_approval(
                    workflow.definition,
                    current_run,
                    context,
                    current_checkpoint,
                    states,
                    output_refs,
                    scheduler,
                    node,
                )
            if node.node_type == WorkflowNodeType.EVALUATION.value:
                return await self._execute_evaluation(
                    workflow,
                    current_run,
                    context,
                    current_checkpoint,
                    states,
                    output_refs,
                    scheduler,
                    node,
                )
            if node.node_type != WorkflowNodeType.AGENT.value:
                raise AssertionError("validator admitted an unsupported node")
            try:
                dispatched = await self._dispatch_agent(
                    workflow.definition,
                    current_run,
                    context,
                    output_refs,
                    node,
                )
            except CoreError as error:
                return await self._terminal_failure(
                    current_run, error.detail, workflow.definition, states
                )
            if dispatched.result.error is not None:
                return await self._terminal_failure(
                    current_run,
                    dispatched.result.error,
                    workflow.definition,
                    states,
                )
            if dispatched.value is None or node.output_schema is None:
                raise AssertionError("successful AgentNode requires a typed output")
            reference = await self._outputs.persist(
                PersistNodeOutputRequest(
                    run_id=current_run.run_id,
                    node_id=node.node_id,
                    schema=node.output_schema,
                    value=dispatched.value,
                    scope=node.scope,
                    idempotency_key=dispatched.idempotency_key,
                ),
                dispatched.node_context,
            )
            candidate_states = dict(states)
            candidate_states[node.node_id] = NodeCheckpointState(
                node.node_id, NodeOutcome.SUCCEEDED, output_ref=reference
            )
            candidate_outputs = dict(output_refs)
            candidate_outputs[node.node_id] = reference
            candidate_completed = {*scheduler.completed, node.node_id}
            side_effects = current_checkpoint.side_effects
            if node.side_effecting:
                side_effects = (
                    *side_effects,
                    SideEffectRecord(
                        operation_ref=ResourceRef(
                            "core", "workflow_node", ResourceId(node.node_id.value)
                        ),
                        idempotency_key=dispatched.idempotency_key,
                        request_fingerprint=self._fingerprint(dispatched.input_value),
                        result_ref=reference,
                        outcome=SideEffectOutcome.SUCCEEDED,
                    ),
                )
            stable = self._checkpoint(
                current_run,
                previous=current_checkpoint,
                node_states=tuple(candidate_states.values()),
                pending_nodes=tuple(
                    node_id
                    for node_id in sorted(
                        (item.node_id for item in workflow.definition.nodes),
                        key=lambda item: item.value,
                    )
                    if node_id not in candidate_completed
                ),
                output_refs=candidate_outputs,
                side_effects=side_effects,
                approvals=current_checkpoint.approvals,
            )
            current_run = await self._checkpoints.save(
                self._checkpoint_provider_id,
                stable,
                current_run.state_version,
                context.runtime,
            )
            scheduler.mark_completed(node.node_id)
            states = candidate_states
            output_refs = candidate_outputs
            current_checkpoint = stable

        return await self._terminal_success(
            workflow.definition,
            current_run,
            context.runtime,
            current_checkpoint,
            states,
            output_refs,
        )

    async def _execute_evaluation(
        self,
        workflow: ValidatedWorkflow,
        run: WorkflowRun,
        workflow_context: WorkflowContext,
        checkpoint: Checkpoint,
        states: dict[NodeId, NodeCheckpointState],
        output_refs: dict[NodeId, CheckpointReference],
        scheduler: DeterministicScheduler,
        node: WorkflowNode,
    ) -> WorkflowExecutionOutcome:
        if self._evaluations is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "evaluation_harness_unavailable",
                "WorkflowRuntime has no Evaluation harness",
            )
        evaluations = self._evaluations
        access = self._node_access(
            workflow.definition, run, node, workflow_context.runtime
        )

        async def operation(call: AuthorizedCall) -> object:
            if not isinstance(node.config, EvaluationNodeConfig):
                raise AssertionError("validated EvaluationNode config changed type")
            if node.input_schema is None:
                raise AssertionError("validated EvaluationNode lost input schema")
            key = self._node_idempotency_key(
                run.run_id, node.node_id, node.node_type, workflow_context
            )
            node_context = call.context.narrow(
                scope=node.scope,
                deadline=self._node_deadline(call.context, node),
                idempotency_key=key,
            )
            input_value = await self._node_input(
                workflow.definition,
                run,
                workflow_context,
                output_refs,
                node,
                node_context,
            )
            request = EvaluationRequest(
                contract_version=EVALUATION_CONTRACT_VERSION,
                evaluation_id=self._evaluation_id(run.run_id, node.node_id),
                value=input_value,
                input_schema=node.input_schema,
                policy_ref=node.config.policy_ref,
                quality_evaluator_id=node.config.quality_evaluator_id,
                quality_profile_ref=node.config.quality_profile_ref,
                scope=node.scope,
                constraints={},
            )
            result = await evaluations.evaluate(request, node_context)
            if result.evaluation_id != request.evaluation_id:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "workflow_evaluation_identity_mismatch",
                    "Evaluation result identity does not match its request",
                )
            result_value = as_json_value(result.to_data(), "Evaluation result")
            self._schemas.validate(EVALUATION_RESULT_SCHEMA, result_value)
            return _EvaluationDispatch(result, input_value, node_context, key)

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, _EvaluationDispatch):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_evaluation_dispatch_invalid",
                "EvaluationNode dispatch returned an invalid result",
            )
        # EvaluationHarness returned only after the verdict AUDIT was
        # acknowledged.  Persistence is therefore the next allowed side effect.
        reference = await self._outputs.persist(
            PersistNodeOutputRequest(
                run_id=run.run_id,
                node_id=node.node_id,
                schema=EVALUATION_RESULT_SCHEMA,
                value=as_json_value(raw.result.to_data(), "Evaluation result"),
                scope=node.scope,
                idempotency_key=raw.idempotency_key,
            ),
            raw.node_context,
        )
        succeeded = raw.result.verdict is EvaluationVerdict.PASSED
        outcome = self._evaluation_outcome(raw.result.verdict)
        candidate_states = dict(states)
        candidate_states[node.node_id] = NodeCheckpointState(
            node.node_id,
            outcome,
            output_ref=reference if succeeded else None,
            error_ref=None if succeeded else reference,
        )
        candidate_outputs = dict(output_refs)
        if succeeded:
            candidate_outputs[node.node_id] = reference
        # The node is absent from pending_nodes once this boundary commits even
        # when it failed.  It joins scheduler.completed only on PASSED below.
        boundary_nodes = {*scheduler.completed, node.node_id}
        stable = self._checkpoint(
            run,
            previous=checkpoint,
            node_states=tuple(candidate_states.values()),
            pending_nodes=tuple(
                node_id
                for node_id in sorted(
                    (item.node_id for item in workflow.definition.nodes),
                    key=lambda item: item.value,
                )
                if node_id not in boundary_nodes
            ),
            output_refs=candidate_outputs,
            side_effects=checkpoint.side_effects,
            approvals=checkpoint.approvals,
        )
        committed_run = await self._checkpoints.save(
            self._checkpoint_provider_id,
            stable,
            run.state_version,
            workflow_context.runtime,
        )
        if succeeded:
            # Checkpoint CAS is the unlock gate.  Moving this call above save()
            # would allow downstream work to observe an unstable result.
            scheduler.mark_completed(node.node_id)
            return await self._drive(workflow, committed_run, workflow_context, stable)
        return await self._terminal_failure(
            committed_run,
            self._evaluation_error(raw.result),
            workflow.definition,
            candidate_states,
        )

    async def _stable_evaluation_failure(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        context: RuntimeCallContext,
        states: dict[NodeId, NodeCheckpointState],
    ) -> ErrorDetail | None:
        # Stable non-success results are stored through error_ref using the same
        # typed EvaluationResult schema.  Loading and validating that value lets
        # recovery terminalize without redispatching an external evaluator.
        nodes = {item.node_id: item for item in definition.nodes}
        for node_id, state in sorted(states.items(), key=lambda item: item[0].value):
            node = nodes.get(node_id)
            if (
                node is None
                or node.node_type != WorkflowNodeType.EVALUATION.value
                or state.outcome is NodeOutcome.SUCCEEDED
            ):
                continue
            if state.error_ref is None:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "workflow_evaluation_error_reference_missing",
                    "stable non-successful EvaluationNode has no result reference",
                )
            value = await self._outputs.load(
                LoadNodeOutputRequest(
                    run.run_id,
                    node_id,
                    EVALUATION_RESULT_SCHEMA,
                    state.error_ref,
                    node.scope,
                ),
                context,
            )
            result = EvaluationResult.from_data(
                as_object(value, "stable Evaluation result")
            )
            if self._evaluation_outcome(result.verdict) is not state.outcome:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "workflow_evaluation_outcome_mismatch",
                    "stable Evaluation result and node outcome do not match",
                )
            return self._evaluation_error(result)
        return None

    async def _dispatch_agent(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        workflow_context: WorkflowContext,
        output_refs: dict[NodeId, CheckpointReference],
        node: WorkflowNode,
    ) -> _AgentDispatch:
        access = self._node_access(definition, run, node, workflow_context.runtime)

        async def operation(call: AuthorizedCall) -> object:
            if not isinstance(node.config, AgentNodeConfig):
                raise AssertionError("validated AgentNode config changed type")
            node.scope.require_narrower_than(run.scope)
            key = self._node_idempotency_key(
                run.run_id, node.node_id, node.node_type, workflow_context
            )
            node_context = call.context.narrow(
                scope=node.scope,
                deadline=self._node_deadline(call.context, node),
                idempotency_key=key,
            )
            child = create_child_agent_run(
                run,
                definition_id=node.config.definition_id,
                agent_id=node.config.agent_id,
                model_binding_ref=node.config.model_binding_ref,
                scope=node_context.scope,
                created_at=self._clock.now(),
            )
            created = await self._runs.create(child)
            if not isinstance(created, AgentRun):
                raise AssertionError("child AgentRun changed kind during creation")
            child_context = node_context.for_child_run(
                child.run_id,
                scope=node_context.scope,
                deadline=node_context.deadline,
                idempotency_key=key,
            )
            input_value = await self._node_input(
                definition, run, workflow_context, output_refs, node, node_context
            )
            result = await self._agents.execute(
                child.run_id, self._content_input(input_value), child_context
            )
            value: JsonValue | None = None
            if result.response is not None:
                value = (
                    result.response.structured_output
                    if result.response.structured_output is not None
                    else as_json_value(
                        [item.to_data() for item in result.response.output],
                        "AgentNode content output",
                    )
                )
                if node.output_schema is None:
                    raise AssertionError("validated AgentNode lost output schema")
                self._schemas.validate(node.output_schema, value)
            return _AgentDispatch(result, value, input_value, node_context, key)

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, _AgentDispatch):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_agent_dispatch_invalid",
                "AgentNode dispatch returned an invalid result",
            )
        return raw

    async def _request_approval(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        workflow_context: WorkflowContext,
        checkpoint: Checkpoint,
        states: dict[NodeId, NodeCheckpointState],
        output_refs: dict[NodeId, CheckpointReference],
        scheduler: DeterministicScheduler,
        node: WorkflowNode,
    ) -> WorkflowExecutionOutcome:
        access = self._node_access(definition, run, node, workflow_context.runtime)

        async def operation(call: AuthorizedCall) -> object:
            if not isinstance(node.config, ApprovalNodeConfig):
                raise AssertionError("validated ApprovalNode config changed type")
            request = ApprovalRequest(
                approval_id=self._approval_id(run.run_id, node.node_id),
                run_id=run.run_id,
                node_id=node.node_id,
                correlation_id=CorrelationId(call.context.trace.correlation_id.value),
                scope=run.scope,
                allowed_outcomes=node.config.allowed_outcomes,
                expires_at=node.config.expires_at,
                prompt_ref=node.config.prompt_ref,
            )
            approval_state = ApprovalCheckpointState(request)
            candidate_states = dict(states)
            candidate_states[node.node_id] = NodeCheckpointState(
                node.node_id,
                NodeOutcome.WAITING_APPROVAL,
                approval_state=approval_state,
            )
            waiting_checkpoint = self._checkpoint(
                run,
                previous=checkpoint,
                node_states=tuple(candidate_states.values()),
                pending_nodes=tuple(
                    item for item in scheduler.pending_nodes() if item != node.node_id
                ),
                output_refs=output_refs,
                side_effects=checkpoint.side_effects,
                approvals=(*checkpoint.approvals, approval_state),
            )
            waiting_run = await self._approvals.request_approval(
                self._checkpoint_provider_id,
                request,
                waiting_checkpoint,
                run.state_version,
                workflow_context.runtime,
            )
            return WorkflowSuspension(waiting_run, waiting_checkpoint.ref, request)

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, WorkflowSuspension):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_approval_dispatch_invalid",
                "ApprovalNode dispatch returned an invalid suspension",
            )
        return WorkflowExecutionOutcome(suspension=raw)

    async def _terminal_success(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        context: RuntimeCallContext,
        checkpoint: Checkpoint,
        states: dict[NodeId, NodeCheckpointState],
        output_refs: dict[NodeId, CheckpointReference],
    ) -> WorkflowExecutionOutcome:
        source_id = definition.output_binding.source_node_id
        reference = output_refs.get(source_id)
        source = next(item for item in definition.nodes if item.node_id == source_id)
        if reference is None or source.output_schema is None:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_final_output_reference_missing",
                "final Workflow output is not durably referenced",
            )
        output = await self._outputs.load(
            LoadNodeOutputRequest(
                run.run_id, source_id, source.output_schema, reference, source.scope
            ),
            context,
        )
        self._schemas.validate(definition.output_schema, output)
        completed = await self._runs.complete(run.run_id, run.state_version)
        if not isinstance(completed, WorkflowRun):
            raise AssertionError("WorkflowRun changed kind while completing")
        return WorkflowExecutionOutcome(
            result=WorkflowResult(
                completed,
                output=output,
                output_refs=self._public_output_refs(definition, states),
                final_checkpoint_ref=checkpoint.ref,
            )
        )

    async def _terminal_failure(
        self,
        run: WorkflowRun,
        detail: ErrorDetail,
        definition: WorkflowDefinition,
        states: dict[NodeId, NodeCheckpointState],
    ) -> WorkflowExecutionOutcome:
        latest = await self._workflow_run(run.run_id)
        if not latest.status.terminal:
            if detail.category is ErrorCategory.CANCELLED:
                terminal = await self._runs.cancel(latest.run_id, latest.state_version)
            else:
                terminal = await self._runs.fail(
                    latest.run_id, latest.state_version, detail
                )
            if not isinstance(terminal, WorkflowRun):
                raise AssertionError("WorkflowRun changed kind while terminating")
            latest = terminal
        error = latest.error_summary.detail if latest.error_summary else detail
        return WorkflowExecutionOutcome(
            result=WorkflowResult(
                latest,
                error=error,
                output_refs=self._public_output_refs(definition, states),
                final_checkpoint_ref=latest.latest_checkpoint_ref,
            )
        )

    async def _validation_failure(
        self, run_id: RunId, detail: ErrorDetail
    ) -> WorkflowExecutionOutcome:
        run = await self._workflow_run(run_id)
        if not run.status.terminal:
            failed = await self._runs.fail(run.run_id, run.state_version, detail)
            if not isinstance(failed, WorkflowRun):
                raise AssertionError(
                    "WorkflowRun changed kind after validation failure"
                )
            run = failed
        return WorkflowExecutionOutcome(result=WorkflowResult(run, error=detail))

    async def _node_input(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        workflow_context: WorkflowContext,
        output_refs: dict[NodeId, CheckpointReference],
        node: WorkflowNode,
        context: RuntimeCallContext,
    ) -> JsonValue:
        binding = node.input_bindings[0]
        if binding.source is WorkflowInputSource.WORKFLOW_INPUT:
            return workflow_context.input
        source_id = binding.source_node_id
        if source_id is None:
            raise AssertionError("validated node output binding lost its source")
        reference = output_refs.get(source_id)
        source_node = next(
            item for item in definition.nodes if item.node_id == source_id
        )
        if reference is None or source_node.output_schema is None:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_bound_output_missing",
                "upstream Workflow output is not durably available",
            )
        return await self._outputs.load(
            LoadNodeOutputRequest(
                run.run_id,
                source_id,
                source_node.output_schema,
                reference,
                source_node.scope,
            ),
            context,
        )

    def _content_input(self, value: JsonValue) -> tuple[ContentBlock, ...]:
        if isinstance(value, list):
            try:
                blocks = tuple(
                    ContentBlock.from_data(as_object(item, "AgentNode input block"))
                    for item in as_array(value, "AgentNode input")
                )
            except (KeyError, TypeError, ValueError):
                blocks = ()
            if blocks:
                return blocks
        return (ContentBlock.json(value),)

    def _node_access(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        node: WorkflowNode,
        context: RuntimeCallContext,
    ) -> AccessRequest:
        permission = next(
            item
            for item in node.permissions
            if item.action == WORKFLOW_NODE_EXECUTE_ACTION
        )
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.WORKFLOW_NODE, PrincipalId(node.node_id.value)
            ),
            action=WORKFLOW_NODE_EXECUTE_ACTION,
            resource=permission.resource,
            scope=node.scope,
            context=context,
            constraints={},
        )

    def _checkpoint(
        self,
        run: WorkflowRun,
        *,
        previous: Checkpoint | None,
        node_states: tuple[NodeCheckpointState, ...],
        pending_nodes: tuple[NodeId, ...],
        output_refs: dict[NodeId, CheckpointReference],
        side_effects: tuple[SideEffectRecord, ...],
        approvals: tuple[ApprovalCheckpointState, ...],
    ) -> Checkpoint:
        return Checkpoint.create(
            checkpoint_id=CheckpointRef.new(),
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            definition_id=run.definition_id,
            graph_version=run.graph_version,
            scope=run.scope,
            sequence=previous.sequence + 1 if previous else 1,
            attempt=run.attempt,
            previous_checkpoint_ref=previous.ref if previous else None,
            node_states=tuple(sorted(node_states, key=lambda item: item.node_id.value)),
            pending_nodes=tuple(sorted(pending_nodes, key=lambda item: item.value)),
            external_refs=self._checkpoint_references(node_states, output_refs),
            side_effects=side_effects,
            approvals=approvals,
            created_at=self._clock.now(),
        )

    async def _load_checkpoint(
        self, run: WorkflowRun, context: RuntimeCallContext
    ) -> Checkpoint:
        marker = run.latest_checkpoint_ref
        if marker is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_checkpoint_missing",
                "WorkflowRun has no committed checkpoint",
            )
        return await self._checkpoint_gateway.load(
            self._checkpoint_provider_id, marker, run.run_id, run.scope, context
        )

    async def _workflow_run(self, run_id: RunId) -> WorkflowRun:
        run = await self._runs.get(run_id)
        if not isinstance(run, WorkflowRun):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_run_required",
                "WorkflowRuntime requires a WorkflowRun",
            )
        return run

    def _validate_identity(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> None:
        if (
            run.workflow_id != definition.workflow_id
            or run.definition_id != definition.definition_id
            or run.graph_version != definition.version
            or context.run_id != run.run_id
            or context.root_run_id != run.root_run_id
            or context.parent_run_id != run.parent_run_id
            or context.workspace_id != run.workspace_id
            or context.session_ref != run.session_ref
            or context.scope != run.scope
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_execution_identity_mismatch",
                "Workflow definition, Run, and RuntimeCallContext do not match",
            )
        for node in definition.nodes:
            node.scope.require_narrower_than(run.scope)

    def _output_refs(
        self,
        definition: WorkflowDefinition,
        states: dict[NodeId, NodeCheckpointState],
    ) -> dict[NodeId, CheckpointReference]:
        node_ids = {item.node_id for item in definition.nodes}
        return {
            node_id: state.output_ref
            for node_id, state in states.items()
            if node_id in node_ids and state.output_ref is not None
        }

    def _checkpoint_references(
        self,
        node_states: tuple[NodeCheckpointState, ...],
        output_refs: dict[NodeId, CheckpointReference],
    ) -> tuple[CheckpointReference, ...]:
        # error_ref is just as durable as output_ref.  Including both in
        # external_refs makes a committed failure self-contained for recovery.
        references = [
            output_refs[node_id]
            for node_id in sorted(output_refs, key=lambda item: item.value)
        ]
        references.extend(
            state.error_ref
            for state in sorted(node_states, key=lambda item: item.node_id.value)
            if state.error_ref is not None
        )
        return tuple(dict.fromkeys(references))

    def _public_output_refs(
        self,
        definition: WorkflowDefinition,
        states: dict[NodeId, NodeCheckpointState],
    ) -> tuple[NodeOutputReference, ...]:
        nodes = {item.node_id: item for item in definition.nodes}
        values: list[NodeOutputReference] = []
        for node_id, reference in self._output_refs(definition, states).items():
            schema = nodes[node_id].output_schema
            if schema is not None:
                values.append(NodeOutputReference(node_id, schema, reference))
        return tuple(sorted(values, key=lambda item: item.node_id.value))

    def _pending_approval(
        self, checkpoint: Checkpoint
    ) -> ApprovalCheckpointState | None:
        pending = [item for item in checkpoint.approvals if item.decision is None]
        if len(pending) > 1:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_multiple_pending_approvals",
                "the single-node runtime cannot restore multiple pending approvals",
            )
        return pending[0] if pending else None

    def _approval_state(
        self, checkpoint: Checkpoint, approval_id: ApprovalId
    ) -> ApprovalCheckpointState:
        matches = [
            item
            for item in checkpoint.approvals
            if item.request.approval_id == approval_id
        ]
        if len(matches) != 1:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_approval_not_found",
                "Workflow checkpoint does not contain the approval identity",
            )
        return matches[0]

    def _suspension(
        self, run: WorkflowRun, checkpoint: Checkpoint
    ) -> WorkflowExecutionOutcome:
        pending = self._pending_approval(checkpoint)
        if pending is None:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_approval_state_missing",
                "WAITING_APPROVAL Run has no pending approval in its checkpoint",
            )
        return WorkflowExecutionOutcome(
            suspension=WorkflowSuspension(run, checkpoint.ref, pending.request)
        )

    def _node_deadline(
        self, context: RuntimeCallContext, node: WorkflowNode
    ) -> Deadline | None:
        if node.timeout_seconds is None:
            return context.deadline
        node_deadline = Deadline(
            self._clock.now() + timedelta(seconds=node.timeout_seconds)
        )
        if context.deadline is not None and context.deadline.at <= node_deadline.at:
            return context.deadline
        return node_deadline

    def _node_idempotency_key(
        self,
        run_id: RunId,
        node_id: NodeId,
        node_type: str,
        context: WorkflowContext,
    ) -> IdempotencyKey:
        parent = context.runtime.idempotency_key
        identity = "|".join(
            (parent.value if parent else "", run_id.value, node_id.value, node_type)
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return IdempotencyKey(f"workflow-node-{digest}")

    def _approval_id(self, run_id: RunId, node_id: NodeId) -> ApprovalId:
        digest = hashlib.sha256(
            f"{run_id.value}|{node_id.value}|approval".encode()
        ).hexdigest()
        return ApprovalId(f"workflow-approval-{digest}")

    def _evaluation_id(self, run_id: RunId, node_id: NodeId) -> EvaluationId:
        digest = hashlib.sha256(
            f"{run_id.value}|{node_id.value}|evaluation|1".encode()
        ).hexdigest()
        return EvaluationId(f"workflow-evaluation-{digest}")

    def _evaluation_outcome(self, verdict: EvaluationVerdict) -> NodeOutcome:
        if verdict is EvaluationVerdict.PASSED:
            return NodeOutcome.SUCCEEDED
        if verdict in {
            EvaluationVerdict.POLICY_DENIED,
            EvaluationVerdict.POLICY_INDETERMINATE,
        }:
            return NodeOutcome.DENIED
        if verdict is EvaluationVerdict.TIMED_OUT:
            return NodeOutcome.TIMED_OUT
        if verdict is EvaluationVerdict.CANCELLED:
            return NodeOutcome.CANCELLED
        return NodeOutcome.FAILED

    def _evaluation_error(self, result: EvaluationResult) -> ErrorDetail:
        if result.error is not None:
            return result.error
        if result.verdict in {
            EvaluationVerdict.POLICY_DENIED,
            EvaluationVerdict.POLICY_INDETERMINATE,
        }:
            category = ErrorCategory.DENIED
        elif result.verdict is EvaluationVerdict.QUALITY_FAILED:
            category = ErrorCategory.PARTIAL_RESULT
        else:
            category = ErrorCategory.INVALID_REQUEST
        return ErrorDetail(
            category,
            result.verdict.value,
            "Evaluation did not pass",
            metadata={
                "evaluation_id": result.evaluation_id.value,
                "terminal_stage": result.terminal_stage.value,
            },
        )

    def _fingerprint(self, value: JsonValue) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
