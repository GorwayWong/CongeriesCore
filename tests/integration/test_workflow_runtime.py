from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import cast

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
    AccessRequest,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    Grant,
    PolicyDecision,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.provider.context import (
    ContextBinding,
    ContextCapabilities,
    ContextCompleteness,
    ContextCompletenessPolicy,
    ContextEntry,
    ContextKey,
    ContextMergeRegistry,
    ContextProvider,
    ContextProviderRegistry,
    ContextRequest,
    ContextRequirement,
    ContextResolver,
    ContextResult,
    ContextUsage,
    ContextWarning,
    context_actions,
)
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, Deadline, TraceContext
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
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
from congeries_core.runtime.json_types import JsonValue, as_json_value, as_object
from congeries_core.runtime.run import (
    AgentRun,
    RunStatus,
    WorkflowRun,
    create_root_workflow_run,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef
from congeries_core.skill import (
    SKILL_RESOURCE_READ_ACTION,
    SkillDescriptor,
    SkillResource,
    SkillResourceDescriptor,
    SkillResourceKind,
    SkillResourceRequest,
    SkillToolResolver,
)
from congeries_core.tool import (
    TOOL_EXECUTE_ACTION,
    InMemoryToolOperationStore,
    ToolCall,
    ToolDescriptor,
    ToolExecutionGuard,
    ToolExecutionPolicy,
    ToolGateway,
    ToolIdempotencyMode,
    ToolOperationGateway,
    ToolOperationStatus,
    ToolResult,
    ToolSideEffect,
    tool_actions,
    tool_operation_actions,
)
from congeries_core.workflow import (
    CONTEXT_NODE_RESULT_SCHEMA,
    SKILL_NODE_RESULT_SCHEMA,
    TOOL_NODE_REQUEST_SCHEMA,
    TOOL_NODE_RESULT_SCHEMA,
    WORKFLOW_NODE_EXECUTE_ACTION,
    AgentNodeConfig,
    ApprovalNodeConfig,
    AuthorizedNodeOutputPersistence,
    ContextNodeConfig,
    ContextNodeResult,
    ContextNodeResultSchemaValidator,
    DeterministicScheduler,
    EvaluationNodeConfig,
    ExecutionPolicy,
    LoadNodeOutputRequest,
    PersistNodeOutputRequest,
    SkillNodeConfig,
    SkillNodeResultSchemaValidator,
    ToolNodeConfig,
    ToolNodeRequestSchemaValidator,
    ToolNodeResultSchemaValidator,
    ToolOperationResolution,
    ToolOperationSuspension,
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

from ..provider_support import (
    AuditRecorder,
    FailureRecorder,
    StringObjectValidator,
)
from ..support import NOW, FixedClock, root_scope, session_ref
from ..test_checkpoint import EventRecorder, Restorer
from .test_agent_runtime import RuntimeFixture, runtime_fixture

PROVIDER = ProviderId("workflow-checkpoints")
SCHEMA = SchemaRef("test", "workflow_value", "1")
PROFILE_SCHEMA = SchemaRef("test", "profile", "1")
CONTEXT_PROVIDER = ProviderId("context-1")
SKILL_REF = CapabilityRef(
    "core", "skill", ResourceId("workflow-skill"), "test-plugin", "1"
)
SKILL_RESOURCE = SkillResourceDescriptor(
    ResourceId("instructions"),
    SkillResourceKind.INSTRUCTION,
    "instructions.md",
    "text/markdown",
    1024,
)
SKILL_DESCRIPTOR = SkillDescriptor(
    SKILL_REF, "Workflow Skill", "A read-only workflow test Skill", (SKILL_RESOURCE,)
)
TOOL_REF = CapabilityRef(
    "core", "tool", ResourceId("workflow-tool"), "test-plugin", "1"
)
TOOL_DESCRIPTOR = ToolDescriptor(
    TOOL_REF,
    "Workflow Tool",
    "A side-effecting workflow test Tool",
    SCHEMA,
    SCHEMA,
    TOOL_EXECUTE_ACTION,
    ToolExecutionPolicy(timeout_ms=10_000, max_attempts=1),
    ToolSideEffect.EXTERNAL,
    ToolIdempotencyMode.CALLER_KEY,
)


class AcceptValidator:
    def validate(self, value: JsonValue) -> None:
        del value


@dataclass(slots=True)
class WorkflowPolicy:
    denied_actions: set[str] = field(default_factory=lambda: set[str]())
    requests: list[AccessRequest] = field(default_factory=lambda: list[AccessRequest]())

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        if request.action.name in self.denied_actions:
            return PolicyDecision.deny("test_denied")
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints=request.constraints,
                issued_at=NOW,
                expires_at=None,
                policy_version="workflow-test-1",
                audit_correlation="workflow-audit",
            )
        )


@dataclass(slots=True)
class WorkflowContextProvider(ContextProvider):
    result: ContextResult
    block: bool = False
    swallow_cancel: bool = False
    capability_calls: int = 0
    provide_calls: list[ContextRequest] = field(
        default_factory=lambda: list[ContextRequest]()
    )
    cancelled_calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def capabilities(self, context: RuntimeCallContext) -> ContextCapabilities:
        del context
        self.capability_calls += 1
        return ContextCapabilities(
            CONTEXT_PROVIDER,
            "1",
            (ContextRequirement(ContextKey("test", "profile"), PROFILE_SCHEMA),),
            supports_partial=True,
        )

    async def provide(self, request: ContextRequest) -> ContextResult:
        self.provide_calls.append(request)
        self.started.set()
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled_calls += 1
                if not self.swallow_cancel:
                    raise
        return self.result


@dataclass(frozen=True, slots=True)
class _ResolvedWorkflowSkill:
    descriptor: SkillDescriptor = SKILL_DESCRIPTOR


@dataclass(frozen=True, slots=True)
class _ResolvedWorkflowTool:
    descriptor: ToolDescriptor = TOOL_DESCRIPTOR


class WorkflowSkillResolver:
    def resolve_skill(self, ref: CapabilityRef) -> _ResolvedWorkflowSkill:
        if ref != SKILL_REF:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "skill_not_found",
                "Skill is not registered",
            )
        return _ResolvedWorkflowSkill()

    def resolve_tool(self, ref: CapabilityRef) -> _ResolvedWorkflowTool:
        if ref != TOOL_REF:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "tool_not_found",
                "Tool is not registered",
            )

        return _ResolvedWorkflowTool()


@dataclass(slots=True)
class WorkflowSkillGateway:
    block: bool = False
    swallow_cancel: bool = False
    requests: list[SkillResourceRequest] = field(default_factory=list)
    contexts: list[RuntimeCallContext] = field(default_factory=list)
    cancelled_calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def load(
        self, request: SkillResourceRequest, context: RuntimeCallContext
    ) -> SkillResource:
        self.requests.append(request)
        self.contexts.append(context)
        self.started.set()

        async def operation() -> SkillResource:
            if self.block:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled_calls += 1
                    if not self.swallow_cancel:
                        raise
            return SkillResource(
                SKILL_REF,
                SKILL_RESOURCE,
                ContentBlock(
                    ContentKind.TEXT,
                    "# Instructions",
                    media_type="text/markdown",
                ),
            )

        return await await_provider(operation(), context, FixedClock())


@dataclass(slots=True)
class WorkflowToolGateway:
    error_before_guard: CoreError | None = None
    error_after_guard: CoreError | None = None
    before_return: Callable[[], None] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    contexts: list[RuntimeCallContext] = field(default_factory=list)

    async def execute(
        self,
        call: ToolCall,
        context: RuntimeCallContext,
        *,
        guard: ToolExecutionGuard | None = None,
    ) -> ToolResult:
        if self.error_before_guard is not None:
            raise self.error_before_guard
        if guard is not None:
            await guard.before_execute(call, TOOL_DESCRIPTOR, context)
        self.calls.append(call)
        self.contexts.append(context)
        if self.error_after_guard is not None:
            raise self.error_after_guard
        if self.before_return is not None:
            self.before_return()
        assert context.idempotency_key is not None
        return ToolResult(call.tool, call.input, 1, context.idempotency_key.value)


def _context_result(
    *,
    completeness: ContextCompleteness = ContextCompleteness.COMPLETE,
    value: JsonValue = None,
) -> ContextResult:
    entry_value = as_json_value(
        {"value": "Ada"} if value is None else value,
        "Context provider result",
    )
    missing = (
        (ContextKey("test", "profile"),)
        if completeness is ContextCompleteness.PARTIAL
        else ()
    )
    entries = (
        ()
        if completeness is ContextCompleteness.PARTIAL
        else (
            ContextEntry(
                ContextKey("test", "profile"),
                PROFILE_SCHEMA,
                entry_value,
                ("context-1",),
            ),
        )
    )
    warnings = (
        (
            ContextWarning(
                "profile_unavailable",
                "Profile context is unavailable",
                ContextKey("test", "profile"),
            ),
        )
        if completeness is ContextCompleteness.PARTIAL
        else ()
    )
    return ContextResult(
        CONTEXT_PROVIDER,
        "1",
        entries,
        completeness,
        missing,
        warnings,
        ContextUsage(0),
    )


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
    order: list[str] = field(default_factory=lambda: list[str]())

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
                ResourceId(
                    f"{request.run_id.value}-{request.node_id.value}-"
                    f"{len(self.references) + 1}"
                ),
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
        self.order.append(f"persist:{request.node_id.value}")
        return reference

    async def load(
        self, request: LoadNodeOutputRequest, context: RuntimeCallContext
    ) -> JsonValue:
        del context
        self.loads.append(request)
        return self.values[request.reference.resource.id.value]


class RecordingCheckpointStore(InMemoryCheckpointStore):
    def __init__(self, provider_id: ProviderId, order: list[str]) -> None:
        super().__init__(provider_id)
        self._order = order

    async def save(
        self, checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef:
        reference = await super().save(checkpoint, context)
        self._order.append("checkpoint")
        return reference


@dataclass(slots=True)
class WorkflowHarness:
    runtime: WorkflowRuntime
    agent: RuntimeFixture
    definition: WorkflowDefinition
    context: WorkflowContext
    output_store: RecordingOutputStore
    checkpoint_store: RecordingCheckpointStore
    events: EventRecorder
    restorer: Restorer
    evaluation_policy: WorkflowEvaluationPolicy | None = None
    quality_evaluator: WorkflowQualityEvaluator | None = None
    evaluation_events: WorkflowEvaluationEvents | None = None
    context_provider: WorkflowContextProvider | None = None
    policy: WorkflowPolicy | None = None
    skill_gateway: WorkflowSkillGateway | None = None
    tool_gateway: WorkflowToolGateway | None = None
    tool_operations: ToolOperationGateway | None = None


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
    input_schema: SchemaRef = SCHEMA,
) -> WorkflowNode:
    return WorkflowNode(
        node_id=NodeId(value),
        node_type=WorkflowNodeType.AGENT.value,
        contract_version="1",
        input_schema=input_schema,
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


def _context_node(
    *,
    completeness_policy: ContextCompletenessPolicy,
    timeout_seconds: int = 10,
) -> WorkflowNode:
    permissions = (
        _permission("context"),
        *(
            WorkflowPermission(
                action,
                ResourceRef(
                    "core",
                    "context_provider",
                    ResourceId(CONTEXT_PROVIDER.value),
                ),
            )
            for action in context_actions()
        ),
    )
    return WorkflowNode(
        node_id=NodeId("context"),
        node_type=WorkflowNodeType.CONTEXT.value,
        contract_version="1",
        input_schema=None,
        input_bindings=(),
        output_schema=CONTEXT_NODE_RESULT_SCHEMA,
        scope=_node_scope("context"),
        permissions=permissions,
        timeout_seconds=timeout_seconds,
        retry_limit=0,
        side_effecting=False,
        idempotency_required=True,
        checkpoint=True,
        config=ContextNodeConfig(
            ContextBinding(
                provider_ids=(CONTEXT_PROVIDER,),
                requirements=(
                    ContextRequirement(ContextKey("test", "profile"), PROFILE_SCHEMA),
                ),
                completeness_policy=completeness_policy,
            )
        ),
    )


def _skill_node(*, timeout_seconds: int = 10) -> WorkflowNode:
    return WorkflowNode(
        node_id=NodeId("skill"),
        node_type=WorkflowNodeType.SKILL.value,
        contract_version="1",
        input_schema=None,
        input_bindings=(),
        output_schema=SKILL_NODE_RESULT_SCHEMA,
        scope=_node_scope("skill"),
        permissions=(
            _permission("skill"),
            WorkflowPermission(
                SKILL_RESOURCE_READ_ACTION,
                ResourceRef(
                    "core",
                    "skill_resource",
                    ResourceId(
                        f"{SKILL_REF.id.value}:{SKILL_RESOURCE.resource_id.value}"
                    ),
                    owning_extension=SKILL_REF.owning_extension,
                ),
            ),
        ),
        timeout_seconds=timeout_seconds,
        retry_limit=0,
        side_effecting=False,
        idempotency_required=True,
        checkpoint=True,
        config=SkillNodeConfig(SKILL_REF, SKILL_RESOURCE.resource_id, 1024),
    )


def _tool_node(*, timeout_seconds: int = 10) -> WorkflowNode:
    return WorkflowNode(
        node_id=NodeId("tool"),
        node_type=WorkflowNodeType.TOOL.value,
        contract_version="1",
        input_schema=SCHEMA,
        input_bindings=(WorkflowInputBinding(WorkflowInputSource.WORKFLOW_INPUT),),
        output_schema=TOOL_NODE_RESULT_SCHEMA,
        scope=_node_scope("tool"),
        permissions=(
            _permission("tool"),
            WorkflowPermission(
                TOOL_EXECUTE_ACTION,
                ResourceRef(
                    "core",
                    "tool",
                    TOOL_REF.id,
                    owning_extension=TOOL_REF.owning_extension,
                ),
            ),
        ),
        timeout_seconds=timeout_seconds,
        retry_limit=0,
        side_effecting=True,
        idempotency_required=True,
        checkpoint=True,
        config=ToolNodeConfig(TOOL_REF),
    )


def _definition(
    fixture: RuntimeFixture,
    *,
    approval: bool = False,
    evaluation: bool = False,
    context_policy: ContextCompletenessPolicy | None = None,
    context_timeout_seconds: int = 10,
    skill: bool = False,
    skill_timeout_seconds: int = 10,
    tool: bool = False,
    tool_timeout_seconds: int = 10,
) -> WorkflowDefinition:
    first = _agent_node(fixture, "a", timeout_seconds=10)
    if skill:
        source = _skill_node(timeout_seconds=skill_timeout_seconds)
        return WorkflowDefinition(
            WorkflowId("workflow-skill-1"),
            DefinitionId("workflow-skill-definition-1"),
            "1",
            SCHEMA,
            (source,),
            (),
            SKILL_NODE_RESULT_SCHEMA,
            WorkflowOutputBinding(source.node_id),
            ExecutionPolicy(),
        )
    if tool:
        source = _tool_node(timeout_seconds=tool_timeout_seconds)
        return WorkflowDefinition(
            WorkflowId("workflow-tool-1"),
            DefinitionId("workflow-tool-definition-1"),
            "1",
            SCHEMA,
            (source,),
            (),
            TOOL_NODE_RESULT_SCHEMA,
            WorkflowOutputBinding(source.node_id),
            ExecutionPolicy(),
        )
    if context_policy is not None:
        source = _context_node(
            completeness_policy=context_policy,
            timeout_seconds=context_timeout_seconds,
        )
        final = _agent_node(
            fixture,
            "after-context",
            source=source.node_id,
            input_schema=CONTEXT_NODE_RESULT_SCHEMA,
        )
        return WorkflowDefinition(
            WorkflowId("workflow-context-1"),
            DefinitionId("workflow-context-definition-1"),
            "1",
            SCHEMA,
            (source, final),
            (WorkflowDependency(source.node_id, final.node_id, True),),
            SCHEMA,
            WorkflowOutputBinding(final.node_id),
            ExecutionPolicy(),
        )
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
    context_completeness_policy: ContextCompletenessPolicy | None = None,
    context_result: ContextResult | None = None,
    context_provider: WorkflowContextProvider | None = None,
    context_timeout_seconds: int = 10,
    context_resolver_enabled: bool = True,
    denied_actions: frozenset[str] = frozenset[str](),
    skill: bool = False,
    skill_gateway: WorkflowSkillGateway | None = None,
    skill_timeout_seconds: int = 10,
    tool: bool = False,
    tool_gateway: WorkflowToolGateway | None = None,
    tool_timeout_seconds: int = 10,
) -> WorkflowHarness:
    agent = await runtime_fixture()
    definition = _definition(
        agent,
        approval=approval,
        evaluation=evaluation_verdict is not None,
        context_policy=context_completeness_policy,
        context_timeout_seconds=context_timeout_seconds,
        skill=skill,
        skill_timeout_seconds=skill_timeout_seconds,
        tool=tool,
        tool_timeout_seconds=tool_timeout_seconds,
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
    actions = (
        *checkpoint_actions(),
        *workflow_actions(),
        *evaluation_actions(),
        *context_actions(),
        SKILL_RESOURCE_READ_ACTION,
        *tool_actions(),
        *tool_operation_actions(),
    )
    policy = WorkflowPolicy(denied_actions=set(denied_actions))
    dispatcher = AuthorizedDispatcher(
        action_registry=ActionRegistry(actions),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        policy=policy,
    )
    boundary_order: list[str] = []
    checkpoint_store = RecordingCheckpointStore(PROVIDER, boundary_order)
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
    schemas.register(PROFILE_SCHEMA, StringObjectValidator())
    schemas.register(CONTEXT_NODE_RESULT_SCHEMA, ContextNodeResultSchemaValidator())
    schemas.register(EVALUATION_RESULT_SCHEMA, EvaluationResultSchemaValidator())
    schemas.register(SKILL_NODE_RESULT_SCHEMA, SkillNodeResultSchemaValidator())
    schemas.register(TOOL_NODE_REQUEST_SCHEMA, ToolNodeRequestSchemaValidator())
    schemas.register(TOOL_NODE_RESULT_SCHEMA, ToolNodeResultSchemaValidator())
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
        order=boundary_order,
    )
    selected_context_provider = context_provider
    contexts: ContextResolver | None = None
    if context_completeness_policy is not None:
        if selected_context_provider is None:
            selected_context_provider = WorkflowContextProvider(
                context_result or _context_result()
            )
        providers = ContextProviderRegistry()
        providers.register(CONTEXT_PROVIDER, selected_context_provider)
        if context_resolver_enabled:
            contexts = ContextResolver(
                providers=providers,
                schemas=schemas,
                merges=ContextMergeRegistry(),
                dispatcher=dispatcher,
                clock=clock,
            )
    selected_tool_gateway = tool_gateway
    tool_operations: ToolOperationGateway | None = None
    if tool:
        selected_tool_gateway = selected_tool_gateway or WorkflowToolGateway()
        tool_operations = ToolOperationGateway(
            InMemoryToolOperationStore(clock), dispatcher
        )
    runtime = WorkflowRuntime(
        validator=WorkflowValidator(
            schemas=schemas,
            actions=ActionRegistry(actions),
            skill_tools=WorkflowSkillResolver(),
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
        contexts=contexts,
        skills=skill_gateway if skill else None,
        tools=cast(ToolGateway, selected_tool_gateway) if tool else None,
        tool_operations=tool_operations,
        skill_tools=cast(SkillToolResolver, WorkflowSkillResolver()),
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
        selected_context_provider,
        policy,
        skill_gateway if skill else None,
        selected_tool_gateway if tool else None,
        tool_operations,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completeness_policy",
    (
        ContextCompletenessPolicy.REQUIRE_COMPLETE,
        ContextCompletenessPolicy.ALLOW_PARTIAL,
    ),
)
async def test_context_node_persists_complete_or_allowed_partial_before_unlock(
    completeness_policy: ContextCompletenessPolicy,
) -> None:
    completeness = (
        ContextCompleteness.PARTIAL
        if completeness_policy is ContextCompletenessPolicy.ALLOW_PARTIAL
        else ContextCompleteness.COMPLETE
    )
    harness = await _harness(
        context_completeness_policy=completeness_policy,
        context_result=_context_result(completeness=completeness),
    )
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert harness.context_provider is not None
    assert len(harness.context_provider.provide_calls) == 1

    assert [item.node_id.value for item in harness.output_store.requests] == [
        "context",
        "after-context",
    ]
    persisted = ContextNodeResult.from_data(
        as_object(harness.output_store.requests[0].value, "ContextNode result")
    )
    assert persisted.completeness is completeness
    # The first item is the Workflow-start Checkpoint. For ContextNode itself the
    # important pair is persist:context followed by the successful node Checkpoint.
    assert harness.output_store.order[:3] == [
        "checkpoint",
        "persist:context",
        "checkpoint",
    ]
    node_context = harness.output_store.contexts[0]
    assert node_context.cancellation is harness.context.runtime.cancellation
    assert node_context.trace.trace_id == harness.context.runtime.trace.trace_id
    assert node_context.trace.span_id != harness.context.runtime.trace.span_id
    assert node_context.idempotency_key is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "denied_actions", "expected_code"),
    (
        (
            _context_result(completeness=ContextCompleteness.PARTIAL),
            frozenset[str](),
            "partial_context_rejected",
        ),
        (
            _context_result(),
            frozenset({"context.provide"}),
            "test_denied",
        ),
        (
            ContextResult(
                ProviderId("wrong-provider"),
                "1",
                _context_result().entries,
                ContextCompleteness.COMPLETE,
                usage=ContextUsage(0),
            ),
            frozenset[str](),
            "context_provider_identity_mismatch",
        ),
        (
            _context_result(value={"wrong": "shape"}),
            frozenset[str](),
            "schema_validation_failed",
        ),
    ),
)
async def test_context_node_failure_does_not_persist_or_unlock(
    result: ContextResult,
    denied_actions: frozenset[str],
    expected_code: str,
) -> None:
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
        context_result=result,
        denied_actions=denied_actions,
    )
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert outcome.result.error is not None
    assert outcome.result.error.code == expected_code
    assert harness.output_store.requests == []
    assert harness.agent.model_provider.generate_calls == []


@pytest.mark.asyncio
async def test_context_node_missing_resolver_fails_before_initial_checkpoint() -> None:
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
        context_resolver_enabled=False,
    )
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.error is not None
    assert outcome.result.error.code == "workflow_context_resolver_unavailable"
    assert harness.events.order == []
    assert harness.output_store.requests == []


@pytest.mark.asyncio
async def test_context_node_timeout_discards_provider_result() -> None:
    provider = WorkflowContextProvider(_context_result(), block=True)
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
        context_provider=provider,
        context_timeout_seconds=1,
    )
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert outcome.result.error is not None
    assert outcome.result.error.code == "deadline_exceeded"
    assert provider.cancelled_calls == 1
    assert harness.output_store.requests == []
    assert harness.agent.model_provider.generate_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow_cancel", (False, True))
async def test_context_node_cancel_and_swallowed_late_result_never_persist(
    swallow_cancel: bool,
) -> None:
    provider = WorkflowContextProvider(
        _context_result(), block=True, swallow_cancel=swallow_cancel
    )
    # `False` covers ordinary cooperative cancellation. `True` is the hostile
    # adapter case: the Provider catches task cancellation and returns a value
    # anyway. await_provider must still reject that late value after observing the
    # shared RuntimeCallContext cancellation signal.
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
        context_provider=provider,
    )
    task = asyncio.create_task(
        harness.runtime.execute(harness.definition, harness.context)
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    harness.context.runtime.cancellation.cancel()
    outcome = await task

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.CANCELLED
    assert provider.cancelled_calls == 1
    assert harness.output_store.requests == []
    assert harness.agent.model_provider.generate_calls == []


@pytest.mark.asyncio
async def test_context_node_interrupted_output_replays_with_same_identity() -> None:
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
        fail_node_id=NodeId("context"),
    )
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    first_key = harness.output_store.requests[0].idempotency_key
    first_reference = harness.output_store.references[first_key.value]

    # The write reached durable storage but raised before returning to Workflow.
    # Recovery must rerun resolution with the same key, allowing persistence to
    # identify and return the already-written reference.
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
    assert harness.context_provider is not None
    assert len(harness.context_provider.provide_calls) == 2
    assert harness.output_store.requests[1].idempotency_key == first_key
    assert harness.output_store.references[first_key.value] == first_reference


@pytest.mark.asyncio
async def test_context_node_checkpoint_failure_reuses_durable_output() -> None:
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE
    )
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
                "simulated checkpoint failure after ContextNode output persistence",
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

    # No ContextNode success was committed, so execute resolves again. The stable
    # key prevents the durable output from being duplicated, while the absent
    # Checkpoint prevents the dependent AgentNode from running too early.
    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert harness.output_store.requests[1].idempotency_key == first_key
    assert harness.output_store.references[first_key.value] == first_reference


@pytest.mark.asyncio
async def test_recovery_skips_context_after_checkpoint_before_mark_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _harness(
        context_completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE
    )
    original_mark = DeterministicScheduler.mark_completed

    def crash_before_mark(scheduler: DeterministicScheduler, node_id: NodeId) -> None:
        if node_id == NodeId("context"):
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "scheduler_mark_interrupted",
                "simulated crash after stable ContextNode checkpoint",
            )
        original_mark(scheduler, node_id)

    monkeypatch.setattr(DeterministicScheduler, "mark_completed", crash_before_mark)
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "scheduler_mark_interrupted"
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    assert harness.context_provider is not None
    assert len(harness.context_provider.provide_calls) == 1

    monkeypatch.setattr(DeterministicScheduler, "mark_completed", original_mark)
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
    assert len(harness.context_provider.provide_calls) == 1


@pytest.mark.asyncio
async def test_skill_node_persists_before_checkpoint_and_completes() -> None:
    gateway = WorkflowSkillGateway()
    harness = await _harness(skill=True, skill_gateway=gateway)

    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.requests) == 1
    assert harness.output_store.order[:3] == [
        "checkpoint",
        "persist:skill",
        "checkpoint",
    ]
    assert gateway.contexts[0].idempotency_key is not None


@pytest.mark.asyncio
async def test_skill_node_missing_gateway_fails_before_initial_checkpoint() -> None:
    harness = await _harness(skill=True)
    outcome = await harness.runtime.execute(harness.definition, harness.context)

    assert outcome.result is not None
    assert outcome.result.error is not None
    assert outcome.result.error.code == "workflow_skill_gateway_unavailable"
    assert harness.events.order == []
    assert harness.output_store.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow_cancel", (False, True))
async def test_skill_node_cancel_and_late_result_never_persist(
    swallow_cancel: bool,
) -> None:
    gateway = WorkflowSkillGateway(block=True, swallow_cancel=swallow_cancel)
    harness = await _harness(skill=True, skill_gateway=gateway)
    task = asyncio.create_task(
        harness.runtime.execute(harness.definition, harness.context)
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=2)
    harness.context.runtime.cancellation.cancel()
    outcome = await task

    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.CANCELLED
    assert gateway.cancelled_calls == 1
    assert harness.output_store.requests == []


@pytest.mark.asyncio
async def test_skill_node_interrupted_output_replays_with_same_identity() -> None:
    gateway = WorkflowSkillGateway()
    harness = await _harness(
        skill=True,
        skill_gateway=gateway,
        fail_node_id=NodeId("skill"),
    )
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    first_key = gateway.contexts[0].idempotency_key

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
    assert len(gateway.requests) == 2
    assert gateway.contexts[1].idempotency_key == first_key


@pytest.mark.asyncio
async def test_recovery_skips_skill_after_stable_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = WorkflowSkillGateway()
    harness = await _harness(skill=True, skill_gateway=gateway)
    original_mark = DeterministicScheduler.mark_completed

    def crash_before_mark(scheduler: DeterministicScheduler, node_id: NodeId) -> None:
        if node_id == NodeId("skill"):
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "scheduler_mark_interrupted",
                "simulated crash after stable SkillNode checkpoint",
            )
        original_mark(scheduler, node_id)

    monkeypatch.setattr(DeterministicScheduler, "mark_completed", crash_before_mark)
    with pytest.raises(CoreError):
        await harness.runtime.execute(harness.definition, harness.context)
    running = await harness.agent.runs.get(harness.context.run_id)
    assert isinstance(running, WorkflowRun)
    monkeypatch.setattr(DeterministicScheduler, "mark_completed", original_mark)

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
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_tool_node_commits_intent_before_dispatch_and_stable_result() -> None:
    gateway = WorkflowToolGateway()
    harness = await _harness(tool=True, tool_gateway=gateway)
    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.calls) == 1
    assert [request.schema for request in harness.output_store.requests] == [
        TOOL_NODE_REQUEST_SCHEMA,
        TOOL_NODE_RESULT_SCHEMA,
    ]
    assert harness.checkpoint_store._order == [
        "checkpoint",
        "persist:tool",
        "checkpoint",
        "persist:tool",
        "checkpoint",
    ]


@pytest.mark.asyncio
async def test_tool_node_unknown_pauses_and_recovery_never_replays() -> None:
    gateway = WorkflowToolGateway(
        error_after_guard=core_error(
            ErrorCategory.UNAVAILABLE,
            "mcp_disconnected",
            "Tool transport disconnected after dispatch",
            retryable=True,
        )
    )
    harness = await _harness(tool=True, tool_gateway=gateway)
    first = await harness.runtime.execute(harness.definition, harness.context)
    assert isinstance(first.suspension, ToolOperationSuspension)
    assert first.suspension.run.status is RunStatus.PAUSED
    assert len(gateway.calls) == 1

    replay = await harness.runtime.execute(harness.definition, harness.context)
    assert isinstance(replay.suspension, ToolOperationSuspension)
    assert replay.suspension.operation_id == first.suspension.operation_id
    assert replay.suspension.record_version == first.suspension.record_version
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_tool_node_predispatch_failure_is_stably_terminal() -> None:
    gateway = WorkflowToolGateway(
        error_before_guard=core_error(
            ErrorCategory.DENIED,
            "tool_denied",
            "Tool authorization was denied before dispatch",
        )
    )
    harness = await _harness(tool=True, tool_gateway=gateway)
    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.FAILED
    assert outcome.result.error is not None
    assert outcome.result.error.code == "tool_denied"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_tool_node_explicit_success_resolution_resumes_workflow() -> None:
    gateway = WorkflowToolGateway(
        error_after_guard=core_error(
            ErrorCategory.TIMEOUT,
            "tool_timeout",
            "Tool timed out after dispatch",
        )
    )
    harness = await _harness(tool=True, tool_gateway=gateway)
    suspended = await harness.runtime.execute(harness.definition, harness.context)
    assert isinstance(suspended.suspension, ToolOperationSuspension)
    suspension = suspended.suspension
    evidence = CheckpointReference(
        "tool_evidence",
        ResourceRef("test", "tool_evidence", ResourceId("confirmation-1")),
        _node_scope("tool"),
        "1",
    )
    resolved = await harness.runtime.resolve_tool_operation(
        harness.definition,
        harness.context,
        ToolOperationResolution(
            suspension.operation_id,
            suspension.record_version,
            ToolOperationStatus.SUCCEEDED,
            evidence,
            output={"value": "confirmed"},
        ),
        RuntimePrincipal("test", "operator", PrincipalId("operator-1")),
    )
    assert resolved.result is not None
    assert resolved.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_tool_node_explicit_failure_resolution_is_terminal() -> None:
    gateway = WorkflowToolGateway(
        error_after_guard=core_error(
            ErrorCategory.UNAVAILABLE,
            "external_unknown",
            "External outcome is unknown",
        )
    )
    harness = await _harness(tool=True, tool_gateway=gateway)
    suspended = await harness.runtime.execute(harness.definition, harness.context)
    assert isinstance(suspended.suspension, ToolOperationSuspension)
    suspension = suspended.suspension
    evidence = CheckpointReference(
        "tool_evidence",
        ResourceRef("test", "tool_evidence", ResourceId("confirmation-2")),
        _node_scope("tool"),
        "1",
    )
    resolved = await harness.runtime.resolve_tool_operation(
        harness.definition,
        harness.context,
        ToolOperationResolution(
            suspension.operation_id,
            suspension.record_version,
            ToolOperationStatus.FAILED,
            evidence,
            error=ErrorDetail(
                ErrorCategory.UNAVAILABLE,
                "operator_confirmed_failed",
                "Operator confirmed the Tool failed",
            ),
        ),
        RuntimePrincipal("test", "operator", PrincipalId("operator-1")),
    )
    assert resolved.result is not None
    assert resolved.result.run.status is RunStatus.FAILED
    assert resolved.result.error is not None
    assert resolved.result.error.code == "operator_confirmed_failed"
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_tool_node_request_persistence_crash_reuses_durable_request() -> None:
    gateway = WorkflowToolGateway()
    harness = await _harness(
        tool=True,
        tool_gateway=gateway,
        fail_node_id=NodeId("tool"),
    )
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"
    assert gateway.calls == []

    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.calls) == 1
    request_keys = [
        request.idempotency_key
        for request in harness.output_store.requests
        if request.schema == TOOL_NODE_REQUEST_SCHEMA
    ]
    assert request_keys[0] == request_keys[1]


@pytest.mark.asyncio
async def test_tool_node_prepare_checkpoint_crash_never_dispatches_early() -> None:
    gateway = WorkflowToolGateway()
    harness = await _harness(tool=True, tool_gateway=gateway)
    original_save = harness.checkpoint_store.save
    save_calls = 0

    async def fail_predispatch(
        checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "checkpoint_write_interrupted",
                "simulated pre-dispatch checkpoint failure",
            )
        return await original_save(checkpoint, context)

    harness.checkpoint_store.save = fail_predispatch  # type: ignore[method-assign]
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "checkpoint_write_interrupted"
    assert gateway.calls == []
    harness.checkpoint_store.save = original_save  # type: ignore[method-assign]

    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_tool_node_result_persistence_crash_becomes_unknown_without_replay(
) -> None:
    gateway = WorkflowToolGateway()
    harness = await _harness(tool=True, tool_gateway=gateway)

    def fail_result_write() -> None:
        harness.output_store.fail_node_id = NodeId("tool")

    gateway.before_return = fail_result_write
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "output_write_interrupted"
    assert len(gateway.calls) == 1
    gateway.before_return = None

    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert isinstance(outcome.suspension, ToolOperationSuspension)
    assert outcome.suspension.run.status is RunStatus.PAUSED
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_tool_node_final_checkpoint_crash_reuses_completed_operation() -> None:
    gateway = WorkflowToolGateway()
    harness = await _harness(tool=True, tool_gateway=gateway)
    original_save = harness.checkpoint_store.save
    save_calls = 0

    async def fail_stable(
        checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 3:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "checkpoint_write_interrupted",
                "simulated stable ToolNode checkpoint failure",
            )
        return await original_save(checkpoint, context)

    harness.checkpoint_store.save = fail_stable  # type: ignore[method-assign]
    with pytest.raises(CoreError) as interrupted:
        await harness.runtime.execute(harness.definition, harness.context)
    assert interrupted.value.detail.code == "checkpoint_write_interrupted"
    assert len(gateway.calls) == 1
    harness.checkpoint_store.save = original_save  # type: ignore[method-assign]

    outcome = await harness.runtime.execute(harness.definition, harness.context)
    assert outcome.result is not None
    assert outcome.result.run.status is RunStatus.SUCCEEDED
    assert len(gateway.calls) == 1
