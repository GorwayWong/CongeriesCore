from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from congeries_core.checkpoint import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalOutcome,
    Checkpoint,
    CheckpointCoordinator,
    CheckpointGateway,
    CheckpointMigratorRegistry,
    CheckpointReference,
    CheckpointStoreRegistry,
    InMemoryCheckpointStore,
    NodeOutcome,
    RecoveryCoordinator,
    RecoveryRequest,
    checkpoint_actions,
)
from congeries_core.evaluation import (
    EVALUATION_RESULT_SCHEMA,
    EvaluationHarness,
    EvaluationPolicyGateway,
    EvaluationPolicyRegistry,
    EvaluationRequest,
    EvaluationResult,
    EvaluationResultSchemaValidator,
    EvaluationStage,
    EvaluationStageResult,
    EvaluationVerdict,
    QualityEvaluatorCapabilities,
    QualityEvaluatorGateway,
    QualityEvaluatorRegistry,
    SchemaEvaluator,
    evaluation_actions,
)
from congeries_core.policy.authorization import (
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, Deadline, TraceContext
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import (
    CheckpointRef,
    DefinitionId,
    NodeId,
    PrincipalId,
    ProviderId,
    ResourceId,
    WorkflowId,
    WorkspaceId,
)
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.run import (
    AgentRun,
    RunStatus,
    WorkflowRun,
    create_root_workflow_run,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef
from congeries_core.workflow import (
    WORKFLOW_NODE_EXECUTE_ACTION,
    AgentNodeConfig,
    ApprovalNodeConfig,
    AuthorizedNodeOutputPersistence,
    DeterministicScheduler,
    EvaluationNodeConfig,
    ExecutionPolicy,
    LoadNodeOutputRequest,
    PersistNodeOutputRequest,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowDependency,
    WorkflowInputBinding,
    WorkflowInputSource,
    WorkflowNode,
    WorkflowNodeType,
    WorkflowOutputBinding,
    WorkflowPermission,
    WorkflowRuntime,
    WorkflowValidator,
    workflow_actions,
)

from ..provider_support import AuditRecorder, FailureRecorder
from ..support import NOW, FixedClock, root_scope, session_ref
from ..test_checkpoint import EchoPolicy, EventRecorder, Restorer
from .test_agent_runtime import RuntimeFixture, runtime_fixture

PROVIDER = ProviderId("workflow-checkpoints")
SCHEMA = SchemaRef("test", "workflow_value", "1")


class AcceptValidator:
    def validate(self, value: JsonValue) -> None:
        del value


@dataclass(slots=True)
class WorkflowEvaluationPolicy:
    verdict: EvaluationVerdict
    calls: int = 0

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult:
        del request, context
        self.calls += 1
        return EvaluationStageResult(
            EvaluationStage.POLICY, self.verdict, "workflow_policy_result"
        )


@dataclass(slots=True)
class WorkflowQualityEvaluator:
    verdict: EvaluationVerdict
    calls: int = 0

    async def capabilities(
        self, context: RuntimeCallContext
    ) -> QualityEvaluatorCapabilities:
        del context
        return QualityEvaluatorCapabilities(
            ProviderId("quality-1"),
            "1",
            ("1",),
            ("1",),
            (SCHEMA,),
            ("external",),
            ("evaluation_evidence",),
        )

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult:
        del request, context
        self.calls += 1
        return EvaluationStageResult(
            EvaluationStage.QUALITY, self.verdict, "workflow_quality_result"
        )


@dataclass(slots=True)
class WorkflowEvaluationEvents:
    verdicts: list[EvaluationResult] = field(default_factory=list)

    async def evaluation_started(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> None:
        del request, context

    async def evaluation_verdict_recorded(
        self,
        request: EvaluationRequest,
        result: EvaluationResult,
        context: RuntimeCallContext,
    ) -> None:
        del request, context
        self.verdicts.append(result)


@dataclass(slots=True)
class RecordingOutputStore:
    requests: list[PersistNodeOutputRequest] = field(default_factory=list)
    contexts: list[RuntimeCallContext] = field(default_factory=list)
    loads: list[LoadNodeOutputRequest] = field(default_factory=list)
    values: dict[str, JsonValue] = field(default_factory=dict)
    references: dict[str, CheckpointReference] = field(default_factory=dict)
    fail_after_first_write: bool = False
    fail_node_id: NodeId | None = None

    async def persist(
        self, request: PersistNodeOutputRequest, context: RuntimeCallContext
    ) -> CheckpointReference:
        self.requests.append(request)
        self.contexts.append(context)
        key = request.idempotency_key.value
        existing = self.references.get(key)
        if existing is not None:
            return existing
        reference = CheckpointReference(
            "workflow_node_output",
            ResourceRef(
                "core",
                "workflow_node_output",
                ResourceId(f"{request.run_id.value}-{request.node_id.value}"),
            ),
            request.scope,
            request.schema.version,
        )
        self.values[reference.resource.id.value] = request.value
        self.references[key] = reference
        if self.fail_after_first_write or self.fail_node_id == request.node_id:
            self.fail_after_first_write = False
            self.fail_node_id = None
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "output_write_interrupted",
                "simulated crash after durable output write",
                retryable=True,
            )
        return reference

    async def load(
        self, request: LoadNodeOutputRequest, context: RuntimeCallContext
    ) -> JsonValue:
        del context
        self.loads.append(request)
        return self.values[request.reference.resource.id.value]


@dataclass(slots=True)
class WorkflowHarness:
    runtime: WorkflowRuntime
    agent: RuntimeFixture
    definition: WorkflowDefinition
    context: WorkflowContext
    output_store: RecordingOutputStore
    checkpoint_store: InMemoryCheckpointStore
    events: EventRecorder
    restorer: Restorer
    evaluation_policy: WorkflowEvaluationPolicy | None = None
    quality_evaluator: WorkflowQualityEvaluator | None = None
    evaluation_events: WorkflowEvaluationEvents | None = None


def _node_scope(value: str) -> ScopeRef:
    return ScopeRef.core(CoreScopeKind.RUN, f"node-{value}", root_scope())


def _permission(value: str) -> WorkflowPermission:
    return WorkflowPermission(
        WORKFLOW_NODE_EXECUTE_ACTION,
        ResourceRef("core", "workflow_node", ResourceId(value)),
    )


def _agent_node(
    fixture: RuntimeFixture,
    value: str,
    *,
    source: NodeId | None = None,
    timeout_seconds: int | None = None,
) -> WorkflowNode:
    return WorkflowNode(
        node_id=NodeId(value),
        node_type=WorkflowNodeType.AGENT.value,
        contract_version="1",
        input_schema=SCHEMA,
        input_bindings=(
            WorkflowInputBinding(
                WorkflowInputSource.NODE_OUTPUT
                if source is not None
                else WorkflowInputSource.WORKFLOW_INPUT,
                source,
            ),
        ),
        output_schema=SCHEMA,
        scope=_node_scope(value),
        permissions=(_permission(value),),
        timeout_seconds=timeout_seconds,
        retry_limit=0,
        side_effecting=True,
        idempotency_required=True,
        checkpoint=True,
        config=AgentNodeConfig(
            fixture.run.agent_id,
            fixture.run.definition_id,
            fixture.run.model_binding_ref,
        ),
    )


def _approval_node(value: str = "approval") -> WorkflowNode:
    scope = _node_scope(value)
    prompt_scope = ScopeRef.core(CoreScopeKind.AGENT, "approval-prompt", scope)
    return WorkflowNode(
        node_id=NodeId(value),
        node_type=WorkflowNodeType.APPROVAL.value,
        contract_version="1",
        input_schema=None,
        input_bindings=(),
        output_schema=None,
        scope=scope,
        permissions=(_permission(value),),
        timeout_seconds=None,
        retry_limit=0,
        side_effecting=False,
        idempotency_required=False,
        checkpoint=True,
        config=ApprovalNodeConfig(
            CheckpointReference(
                "approval_prompt",
                ResourceRef("core", "approval_prompt", ResourceId("prompt-1")),
                prompt_scope,
                "1",
            ),
            expires_at=NOW + timedelta(hours=1),
        ),
    )


def _evaluation_node(value: str = "evaluation") -> WorkflowNode:
    permissions = (
        WorkflowPermission(
            WORKFLOW_NODE_EXECUTE_ACTION,
            ResourceRef("core", "workflow_node", ResourceId(value)),
        ),
        WorkflowPermission(
            evaluation_actions()[0],
            ResourceRef("core", "evaluation_policy", ResourceId("policy-1")),
        ),
        *(
            WorkflowPermission(
                action,
                ResourceRef("core", "quality_evaluator", ResourceId("quality-1")),
            )
            for action in evaluation_actions()[1:]
        ),
    )
    return WorkflowNode(
        node_id=NodeId(value),
        node_type=WorkflowNodeType.EVALUATION.value,
        contract_version="1",
        input_schema=SCHEMA,
        input_bindings=(WorkflowInputBinding(WorkflowInputSource.WORKFLOW_INPUT),),
        output_schema=EVALUATION_RESULT_SCHEMA,
        scope=_node_scope(value),
        permissions=permissions,
        timeout_seconds=10,
        retry_limit=0,
        side_effecting=True,
        idempotency_required=True,
        checkpoint=True,
        config=EvaluationNodeConfig(
            "policy-1", ProviderId("quality-1"), "external:profile-1"
        ),
    )


def _definition(
    fixture: RuntimeFixture, *, approval: bool = False, evaluation: bool = False
) -> WorkflowDefinition:
    first = _agent_node(fixture, "a", timeout_seconds=10)
    if evaluation:
        gate = _evaluation_node()
        final = _agent_node(fixture, "b")
        return WorkflowDefinition(
            WorkflowId("workflow-1"),
            DefinitionId("workflow-definition-1"),
            "1",
            SCHEMA,
            (gate, final),
            (WorkflowDependency(gate.node_id, final.node_id),),
            SCHEMA,
            WorkflowOutputBinding(final.node_id),
            ExecutionPolicy(),
        )
    if not approval:
        return WorkflowDefinition(
            WorkflowId("workflow-1"),
            DefinitionId("workflow-definition-1"),
            "1",
            SCHEMA,
            (first,),
            (),
            SCHEMA,
            WorkflowOutputBinding(first.node_id),
            ExecutionPolicy(),
        )
    gate = _approval_node()
    final = _agent_node(fixture, "b", source=first.node_id)
    return WorkflowDefinition(
        WorkflowId("workflow-1"),
        DefinitionId("workflow-definition-1"),
        "1",
        SCHEMA,
        (first, gate, final),
        (
            WorkflowDependency(first.node_id, gate.node_id),
            WorkflowDependency(first.node_id, final.node_id, True),
            WorkflowDependency(gate.node_id, final.node_id),
        ),
        SCHEMA,
        WorkflowOutputBinding(final.node_id),
        ExecutionPolicy(),
    )


async def _harness(
    *,
    approval: bool = False,
    fail_after_first_write: bool = False,
    fail_node_id: NodeId | None = None,
    evaluation_verdict: EvaluationVerdict | None = None,
) -> WorkflowHarness:
    agent = await runtime_fixture()
    definition = _definition(
        agent, approval=approval, evaluation=evaluation_verdict is not None
    )
    workflow_run = create_root_workflow_run(
        definition_id=definition.definition_id,
        workflow_id=definition.workflow_id,
        graph_version=definition.version,
        workspace_id=WorkspaceId("workspace-1"),
        scope=root_scope(),
        created_at=NOW,
        session_ref=session_ref(),
    )
    created = await agent.runs.create(workflow_run)
    assert isinstance(created, WorkflowRun)
    clock = FixedClock()
    actions = (*checkpoint_actions(), *workflow_actions(), *evaluation_actions())
    dispatcher = AuthorizedDispatcher(
        action_registry=ActionRegistry(actions),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        policy=EchoPolicy(),
    )
    checkpoint_store = InMemoryCheckpointStore(PROVIDER)
    checkpoint_stores = CheckpointStoreRegistry()
    checkpoint_stores.register(PROVIDER, checkpoint_store)
    gateway = CheckpointGateway(checkpoint_stores, dispatcher, clock)
    events = EventRecorder()
    checkpoints = CheckpointCoordinator(gateway, agent.runs, events)
    restorer = Restorer()
    recovery = RecoveryCoordinator(
        gateway=gateway,
        runs=agent.runs,
        migrators=CheckpointMigratorRegistry(),
        restorer=restorer,
        audit_failure_handler=FailureRecorder(),
        publisher=events,
    )
    approvals = ApprovalCoordinator(
        checkpoint_coordinator=checkpoints,
        checkpoint_gateway=gateway,
        runs=agent.runs,
        dispatcher=dispatcher,
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        publisher=events,
    )
    schemas = SchemaRegistry()
    schemas.register(SCHEMA, AcceptValidator())
    schemas.register(EVALUATION_RESULT_SCHEMA, EvaluationResultSchemaValidator())
    evaluation_policy: WorkflowEvaluationPolicy | None = None
    quality_evaluator: WorkflowQualityEvaluator | None = None
    evaluation_events: WorkflowEvaluationEvents | None = None
    evaluations: EvaluationHarness | None = None
    if evaluation_verdict is not None:
        policy_verdict = (
            evaluation_verdict
            if evaluation_verdict
            in {
                EvaluationVerdict.POLICY_DENIED,
                EvaluationVerdict.POLICY_INDETERMINATE,
            }
            else EvaluationVerdict.PASSED
        )
        quality_verdict = (
            evaluation_verdict
            if evaluation_verdict
            in {EvaluationVerdict.PASSED, EvaluationVerdict.QUALITY_FAILED}
            else EvaluationVerdict.PASSED
        )
        evaluation_policy = WorkflowEvaluationPolicy(policy_verdict)
        quality_evaluator = WorkflowQualityEvaluator(quality_verdict)
        policies = EvaluationPolicyRegistry()
        policies.register("policy-1", evaluation_policy)
        evaluators = QualityEvaluatorRegistry()
        evaluators.register(ProviderId("quality-1"), quality_evaluator)
        evaluation_events = WorkflowEvaluationEvents()
        evaluations = EvaluationHarness(
            schema=SchemaEvaluator(schemas),
            policy=EvaluationPolicyGateway(
                policies=policies, dispatcher=dispatcher, clock=clock
            ),
            quality=QualityEvaluatorGateway(
                evaluators=evaluators,
                capabilities_dispatcher=dispatcher,
                evaluate_dispatcher=dispatcher,
                clock=clock,
            ),
            events=evaluation_events,
            audit_failure_handler=FailureRecorder(),
            clock=clock,
        )
    output_store = RecordingOutputStore(
        fail_after_first_write=fail_after_first_write,
        fail_node_id=fail_node_id,
    )
    runtime = WorkflowRuntime(
        validator=WorkflowValidator(
            schemas=schemas,
            actions=ActionRegistry(actions),
        ),
        runs=agent.runs,
        agents=agent.runtime,
        dispatcher=dispatcher,
        outputs=AuthorizedNodeOutputPersistence(
            store=output_store, dispatcher=dispatcher
        ),
        schemas=schemas,
        checkpoints=checkpoints,
        checkpoint_gateway=gateway,
        checkpoint_provider_id=PROVIDER,
        recovery=recovery,
        approvals=approvals,
        clock=clock,
        evaluations=evaluations,
    )
    call = RuntimeCallContext(
        run_id=workflow_run.run_id,
        root_run_id=workflow_run.root_run_id,
        parent_run_id=None,
        workspace_id=workflow_run.workspace_id,
        session_ref=workflow_run.session_ref,
        scope=workflow_run.scope,
        deadline=Deadline(NOW + timedelta(minutes=1)),
        cancellation=CancellationToken(),
        trace=TraceContext.new(),
        idempotency_key=None,
    )
    return WorkflowHarness(
        runtime,
        agent,
        definition,
        WorkflowContext(workflow_run.run_id, {"value": "hello"}, call),
        output_store,
        checkpoint_store,
        events,
        restorer,
        evaluation_policy,
        quality_evaluator,
        evaluation_events,
    )


@pytest.mark.asyncio
async def test_agent_node_runs_as_child_and_commits_output_before_success() -> None:
    harness = await _harness()

    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert outcome.result.final_checkpoint_ref is not None
    assert outcome.result.output_refs[0].node_id == NodeId("a")
    assert [item.node_id.value for item in harness.output_store.requests] == ["a"]
    assert harness.events.order == ["checkpoint.saved", "checkpoint.saved"]
    persisted_context = harness.output_store.contexts[0]
    assert persisted_context.cancellation is harness.context.runtime.cancellation
    assert persisted_context.deadline == Deadline(NOW + timedelta(seconds=10))
    assert persisted_context.trace.trace_id == harness.context.runtime.trace.trace_id
    assert persisted_context.trace.span_id != harness.context.runtime.trace.span_id
    assert persisted_context.idempotency_key is not None
    latest = await harness.checkpoint_store.load(
        outcome.result.final_checkpoint_ref, harness.context.runtime
    )
    assert "hello" not in json.dumps(latest.to_data())

    child_runs = [
        transition.current
        for transition in harness.agent.transitions.transitions
        if isinstance(transition.current, AgentRun)
        and transition.current.parent_run_id == harness.context.run_id
    ]
    assert child_runs
    child = child_runs[-1]
    assert child.root_run_id == harness.context.run_id
    assert child.workspace_id == harness.context.runtime.workspace_id
    assert child.session_ref == harness.context.runtime.session_ref
    assert child.scope == _node_scope("a")


@pytest.mark.asyncio
async def test_validation_failure_dispatches_nothing_and_writes_no_checkpoint() -> None:
    harness = await _harness()
    invalid = replace(
        harness.definition,
        nodes=(replace(harness.definition.nodes[0], checkpoint=False),),
    )

    outcome = await harness.runtime.execute(invalid, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert outcome.result.run.latest_checkpoint_ref is None
    assert harness.output_store.requests == []
    assert harness.agent.model_provider.generate_calls == []
    assert harness.events.order == []


@pytest.mark.asyncio
async def test_recovery_replays_unstable_node_with_same_idempotency_key() -> None:
    harness = await _harness(fail_after_first_write=True)
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"

    interrupted_run = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(interrupted_run, WorkflowRun)
    assert interrupted_run.status is RunStatus.RUNNING
    assert interrupted_run.latest_checkpoint_ref is not None
    first_key = harness.output_store.requests[0].idempotency_key

    outcome = await harness.runtime.recover(
        harness.definition,
        harness.context,
        RecoveryRequest(
            interrupted_run.run_id,
            interrupted_run.state_version,
            PROVIDER,
            harness.definition.definition_id,
            harness.definition.version,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(harness.restorer.calls) == 1
    assert len(harness.output_store.requests) == 2
    assert harness.output_store.requests[1].idempotency_key == first_key
    assert len(harness.agent.model_provider.generate_calls) == 2
    assert harness.events.order == [
        "checkpoint.saved",
        "checkpoint.saved",
    ]


@pytest.mark.asyncio
async def test_recovery_skips_stable_node_and_replays_only_interrupted_node() -> None:
    harness = await _harness(fail_node_id=NodeId("b"))
    first = _agent_node(harness.agent, "a")
    second = _agent_node(harness.agent, "b", source=first.node_id)
    definition = WorkflowDefinition(
        harness.definition.workflow_id,
        harness.definition.definition_id,
        harness.definition.version,
        SCHEMA,
        (first, second),
        (WorkflowDependency(first.node_id, second.node_id, True),),
        SCHEMA,
        WorkflowOutputBinding(second.node_id),
        ExecutionPolicy(),
    )

    with pytest.raises(CoreError):
        await harness.runtime.execute(definition, harness.context)
    interrupted = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(interrupted, WorkflowRun)
    assert [item.node_id.value for item in harness.output_store.requests] == ["a", "b"]
    second_key = harness.output_store.requests[-1].idempotency_key

    outcome = await harness.runtime.recover(
        definition,
        harness.context,
        RecoveryRequest(
            interrupted.run_id,
            interrupted.state_version,
            PROVIDER,
            definition.definition_id,
            definition.version,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert [item.node_id.value for item in harness.output_store.requests] == [
        "a",
        "b",
        "b",
    ]
    assert harness.output_store.requests[-1].idempotency_key == second_key
    assert len(harness.agent.model_provider.generate_calls) == 3


@pytest.mark.asyncio
async def test_approval_waits_is_restart_safe_and_then_unlocks_downstream() -> None:
    harness = await _harness(approval=True)

    waiting = await harness.runtime.execute(harness.definition, harness.context)
    assert waiting.suspension is not None
    assert waiting.suspension.run.status is RunStatus.WAITING_APPROVAL
    assert [item.node_id.value for item in harness.output_store.requests] == ["a"]
    assert harness.events.order == [
        "checkpoint.saved",
        "checkpoint.saved",
        "checkpoint.saved",
        "approval.requested",
    ]

    restarted = await harness.runtime.execute(harness.definition, harness.context)
    assert restarted.suspension is not None
    assert restarted.suspension.approval == waiting.suspension.approval
    assert harness.events.order.count("approval.requested") == 1

    request = waiting.suspension.approval
    decision = ApprovalDecision(
        request.approval_id,
        request.run_id,
        request.node_id,
        request.correlation_id,
        request.scope,
        RuntimePrincipal.core(CorePrincipalKind.CORE_SERVICE, PrincipalId("approver")),
        ApprovalOutcome.APPROVED,
        NOW,
    )
    completed = await harness.runtime.decide_approval(
        harness.definition, harness.context, decision
    )

    assert completed.result is not None
    assert completed.result.run.status is RunStatus.SUCCEEDED
    assert [item.node_id.value for item in harness.output_store.requests] == ["a", "b"]
    assert harness.events.order[-3:] == [
        "checkpoint.saved",
        "approval.decided",
        "checkpoint.saved",
    ]


@pytest.mark.asyncio
async def test_evaluation_success_commits_before_unlocking_downstream() -> None:
    harness = await _harness(evaluation_verdict=EvaluationVerdict.PASSED)
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert [request.node_id.value for request in harness.output_store.requests] == [
        "evaluation",
        "b",
    ]
    assert harness.evaluation_policy is not None
    assert harness.evaluation_policy.calls == 1
    assert harness.quality_evaluator is not None
    assert harness.quality_evaluator.calls == 1
    assert len(harness.agent.model_provider.generate_calls) == 1
    assert harness.evaluation_events is not None
    assert harness.evaluation_events.verdicts[0].verdict is EvaluationVerdict.PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "node_outcome", "quality_calls"),
    [
        (EvaluationVerdict.POLICY_DENIED, NodeOutcome.DENIED, 0),
        (EvaluationVerdict.POLICY_INDETERMINATE, NodeOutcome.DENIED, 0),
        (EvaluationVerdict.QUALITY_FAILED, NodeOutcome.FAILED, 1),
    ],
)
async def test_evaluation_failure_is_stable_and_never_unlocks_downstream(
    verdict: EvaluationVerdict,
    node_outcome: NodeOutcome,
    quality_calls: int,
) -> None:
    harness = await _harness(evaluation_verdict=verdict)
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert harness.agent.model_provider.generate_calls == []
    assert [request.node_id.value for request in harness.output_store.requests] == [
        "evaluation"
    ]
    assert harness.quality_evaluator is not None
    assert harness.quality_evaluator.calls == quality_calls
    marker = outcome.result.run.latest_checkpoint_ref
    assert marker is not None
    checkpoint = await harness.checkpoint_store.load(marker, harness.context.runtime)
    state = next(
        item for item in checkpoint.node_states if item.node_id.value == "evaluation"
    )
    assert state.outcome is node_outcome
    assert state.output_ref is None
    assert state.error_ref is not None
    assert NodeId("b") in checkpoint.pending_nodes
    assert "hello" not in json.dumps(checkpoint.to_data())


@pytest.mark.asyncio
async def test_recovery_terminalizes_stable_evaluation_failure_without_redispatch() -> (
    None
):
    harness = await _harness(evaluation_verdict=EvaluationVerdict.QUALITY_FAILED)
    original_fail = harness.agent.runs.fail

    async def crash_before_terminal(*args: object) -> object:
        del args
        raise core_error(
            ErrorCategory.UNAVAILABLE,
            "terminal_transition_interrupted",
            "simulated crash after stable failure checkpoint",
        )

    harness.agent.runs.fail = crash_before_terminal  # type: ignore[method-assign]
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "terminal_transition_interrupted"
    harness.agent.runs.fail = original_fail  # type: ignore[method-assign]

    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    assert running.status is RunStatus.RUNNING
    assert running.latest_checkpoint_ref is not None
    assert harness.quality_evaluator is not None
    assert harness.quality_evaluator.calls == 1

    outcome = await harness.runtime.recover(
        harness.definition,
        harness.context,
        RecoveryRequest(
            running.run_id,
            running.state_version,
            PROVIDER,
            harness.definition.definition_id,
            harness.definition.version,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert harness.quality_evaluator.calls == 1
    assert harness.agent.model_provider.generate_calls == []


@pytest.mark.asyncio
async def test_evaluation_output_crash_replays_same_result_and_persistence_key() -> (
    None
):
    harness = await _harness(
        evaluation_verdict=EvaluationVerdict.PASSED,
        fail_after_first_write=True,
    )
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    assert running.latest_checkpoint_ref is not None
    first_key = harness.output_store.requests[0].idempotency_key
    assert harness.evaluation_events is not None
    first_digest = harness.evaluation_events.verdicts[0].digest

    outcome = await harness.runtime.recover(
        harness.definition,
        harness.context,
        RecoveryRequest(
            running.run_id,
            running.state_version,
            PROVIDER,
            harness.definition.definition_id,
            harness.definition.version,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert harness.output_store.requests[1].idempotency_key == first_key
    assert harness.evaluation_events.verdicts[1].digest == first_digest
    assert harness.evaluation_policy is not None
    assert harness.evaluation_policy.calls == 2


@pytest.mark.asyncio
async def test_evaluation_checkpoint_failure_never_unlocks_and_reuses_output_ref() -> (
    None
):
    harness = await _harness(evaluation_verdict=EvaluationVerdict.PASSED)
    original_save = harness.checkpoint_store.save
    save_calls = 0

    async def fail_second_save(
        checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "checkpoint_write_interrupted",
                "simulated checkpoint failure after result persistence",
            )
        return await original_save(checkpoint, context)

    harness.checkpoint_store.save = fail_second_save  # type: ignore[method-assign]
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "checkpoint_write_interrupted"
    assert harness.agent.model_provider.generate_calls == []
    first_key = harness.output_store.requests[0].idempotency_key
    first_reference = harness.output_store.references[first_key.value]
    harness.checkpoint_store.save = original_save  # type: ignore[method-assign]

    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert harness.output_store.requests[1].idempotency_key == first_key
    assert harness.output_store.references[first_key.value] == first_reference


@pytest.mark.asyncio
async def test_recovery_skips_evaluation_after_checkpoint_before_mark_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _harness(evaluation_verdict=EvaluationVerdict.PASSED)
    original_mark = DeterministicScheduler.mark_completed

    def crash_before_mark(scheduler: DeterministicScheduler, node_id: NodeId) -> None:
        if node_id == NodeId("evaluation"):
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "scheduler_mark_interrupted",
                "simulated crash after stable Evaluation checkpoint",
            )
        original_mark(scheduler, node_id)

    monkeypatch.setattr(DeterministicScheduler, "mark_completed", crash_before_mark)
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "scheduler_mark_interrupted"
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    assert running.latest_checkpoint_ref is not None
    assert harness.evaluation_policy is not None
    assert harness.evaluation_policy.calls == 1

    outcome = await harness.runtime.recover(
        harness.definition,
        harness.context,
        RecoveryRequest(
            running.run_id,
            running.state_version,
            PROVIDER,
            harness.definition.definition_id,
            harness.definition.version,
        ),
    )
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert harness.evaluation_policy.calls == 1
