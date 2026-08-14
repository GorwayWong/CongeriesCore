"""Pure pre-execution validation for normalized Workflow definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from congeries_core.evaluation import EVALUATION_RESULT_SCHEMA, evaluation_actions
from congeries_core.policy.authorization import ActionRegistry
from congeries_core.provider.context import context_actions
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import NodeId
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.skill import SKILL_RESOURCE_READ_ACTION, SkillToolResolver
from congeries_core.tool import ToolIdempotencyMode, ToolSideEffect

from .model import (
    CONTEXT_NODE_RESULT_SCHEMA,
    SKILL_NODE_RESULT_SCHEMA,
    TOOL_NODE_RESULT_SCHEMA,
    AgentNodeConfig,
    ApprovalNodeConfig,
    ContextNodeConfig,
    EvaluationNodeConfig,
    SkillNodeConfig,
    ToolNodeConfig,
    WorkflowDefinition,
    WorkflowInputSource,
    WorkflowNode,
    WorkflowNodeType,
)
from .persistence import WORKFLOW_NODE_EXECUTE_ACTION


@dataclass(frozen=True, slots=True)
class ValidatedWorkflow:
    definition: WorkflowDefinition
    topological_order: tuple[NodeId, ...]


class WorkflowValidator:
    def __init__(
        self,
        *,
        schemas: SchemaRegistry,
        actions: ActionRegistry,
        supported_nodes: frozenset[tuple[str, str]] | None = None,
        skill_tools: SkillToolResolver | None = None,
    ) -> None:
        self._schemas = schemas
        self._actions = actions
        self._skill_tools = skill_tools
        self._supported = supported_nodes or frozenset(
            {
                (WorkflowNodeType.AGENT.value, "1"),
                (WorkflowNodeType.APPROVAL.value, "1"),
                (WorkflowNodeType.CONTEXT.value, "1"),
                (WorkflowNodeType.EVALUATION.value, "1"),
                (WorkflowNodeType.SKILL.value, "1"),
                (WorkflowNodeType.TOOL.value, "1"),
            }
        )

    def validate(self, definition: WorkflowDefinition) -> ValidatedWorkflow:
        self._validate_policy(definition)
        self._require_schema(definition.input_schema, "workflow_input_schema_missing")
        self._require_schema(definition.output_schema, "workflow_output_schema_missing")

        node_ids = tuple(node.node_id for node in definition.nodes)
        if len(set(node_ids)) != len(node_ids):
            self._invalid("workflow_duplicate_node", "Workflow node ids must be unique")
        nodes = {node.node_id: node for node in definition.nodes}
        for node in definition.nodes:
            self._validate_node(node)

        dependency_keys = tuple(
            (item.source_node_id, item.target_node_id)
            for item in definition.dependencies
        )
        if len(set(dependency_keys)) != len(dependency_keys):
            self._invalid(
                "workflow_duplicate_dependency",
                "Workflow dependencies must be unique",
            )
        for dependency in definition.dependencies:
            if (
                dependency.source_node_id not in nodes
                or dependency.target_node_id not in nodes
            ):
                self._invalid(
                    "workflow_dependency_node_missing",
                    "Workflow dependency references a missing node",
                )

        order = self._topological_order(definition, nodes)
        self._validate_bindings(definition, nodes)
        self._validate_required_output(definition, nodes)
        return ValidatedWorkflow(definition, order)

    def _validate_policy(self, definition: WorkflowDefinition) -> None:
        policy = definition.execution_policy
        if (
            policy.max_concurrency != 1
            or policy.compensation_enabled
            or policy.max_attempts != 1
        ):
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "workflow_policy_unsupported",
                "the v0.2 direct runtime supports one-at-a-time fail-fast execution",
            )

    def _validate_node(self, node: WorkflowNode) -> None:
        if (node.node_type, node.contract_version) not in self._supported:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "workflow_node_contract_unsupported",
                "Workflow node type or contract version is unsupported",
            )
        if node.retry_limit != 0:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "workflow_node_retry_unsupported",
                "the v0.2 direct runtime does not schedule node retries",
            )
        if node.side_effecting and not node.idempotency_required:
            self._invalid(
                "workflow_node_idempotency_required",
                "side-effecting Workflow nodes require idempotency",
            )
        if not node.checkpoint:
            self._invalid(
                "workflow_node_checkpoint_required",
                "the v0.2 direct runtime requires stable node checkpoints",
            )
        if not any(
            permission.action == WORKFLOW_NODE_EXECUTE_ACTION
            for permission in node.permissions
        ):
            self._invalid(
                "workflow_node_execute_permission_missing",
                "Workflow node does not declare the execute permission",
            )
        for permission in node.permissions:
            if not self._actions.contains(permission.action):
                self._invalid(
                    "workflow_permission_not_evaluable",
                    "Workflow node declares an unregistered permission action",
                )
        if node.input_schema is not None:
            self._require_schema(
                node.input_schema, "workflow_node_input_schema_missing"
            )
        if node.output_schema is not None:
            self._require_schema(
                node.output_schema, "workflow_node_output_schema_missing"
            )

        if node.node_type == WorkflowNodeType.AGENT.value:
            if not isinstance(node.config, AgentNodeConfig):
                self._invalid(
                    "workflow_agent_config_invalid",
                    "AgentNode requires AgentNodeConfig",
                )
            if (
                node.input_schema is None
                or node.output_schema is None
                or len(node.input_bindings) != 1
            ):
                self._invalid(
                    "workflow_agent_contract_incomplete",
                    "AgentNode requires one input binding and input/output schemas",
                )
        elif node.node_type == WorkflowNodeType.SKILL.value:
            if not isinstance(node.config, SkillNodeConfig):
                self._invalid(
                    "workflow_skill_config_invalid",
                    "SkillNode requires SkillNodeConfig",
                )
            if (
                node.input_schema is not None
                or node.input_bindings
                or node.output_schema != SKILL_NODE_RESULT_SCHEMA
            ):
                self._invalid(
                    "workflow_skill_contract_invalid",
                    "SkillNode accepts no input and requires the fixed result schema",
                )
            if node.side_effecting:
                self._invalid(
                    "workflow_skill_side_effect_invalid",
                    "SkillNode must be non-side-effecting",
                )
            if not node.idempotency_required:
                self._invalid(
                    "workflow_skill_idempotency_required",
                    "SkillNode requires a stable idempotency identity",
                )
            if self._skill_tools is None:
                raise core_error(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "workflow_skill_resolver_unavailable",
                    "Workflow validation has no SkillToolResolver",
                )
            resolved = self._skill_tools.resolve_skill(node.config.skill)
            try:
                descriptor = resolved.descriptor.resource(node.config.resource_id)
            except KeyError:
                self._invalid(
                    "workflow_skill_resource_undeclared",
                    "SkillNode resource is not declared",
                )
            if node.config.max_bytes > descriptor.max_bytes:
                self._invalid(
                    "workflow_skill_budget_exceeded",
                    "SkillNode budget exceeds the declared resource budget",
                )
            expected_resource = (
                "core",
                "skill_resource",
                f"{node.config.skill.id.value}:{node.config.resource_id.value}",
                node.config.skill.owning_extension,
            )
            if not any(
                permission.action == SKILL_RESOURCE_READ_ACTION
                and (
                    permission.resource.namespace,
                    permission.resource.kind,
                    permission.resource.id.value,
                    permission.resource.owning_extension,
                )
                == expected_resource
                for permission in node.permissions
            ):
                self._invalid(
                    "workflow_skill_permission_resource_invalid",
                    "Skill permission resource does not match the configured resource",
                )
        elif node.node_type == WorkflowNodeType.CONTEXT.value:
            # ContextNode is a root data source, not a transform over Workflow
            # input. Freezing this shape prevents accidental input coercion and
            # guarantees every consumer sees the same persisted result contract.
            if not isinstance(node.config, ContextNodeConfig):
                self._invalid(
                    "workflow_context_config_invalid",
                    "ContextNode requires ContextNodeConfig",
                )
            if (
                node.input_schema is not None
                or node.input_bindings
                or node.output_schema != CONTEXT_NODE_RESULT_SCHEMA
            ):
                self._invalid(
                    "workflow_context_contract_invalid",
                    "ContextNode accepts no input and requires the fixed result schema",
                )
            if node.side_effecting:
                self._invalid(
                    "workflow_context_side_effect_invalid",
                    "ContextNode must be declared non-side-effecting",
                )
            if not node.idempotency_required:
                self._invalid(
                    "workflow_context_idempotency_required",
                    "ContextNode requires a stable idempotency identity",
                )
            for requirement in node.config.binding.requirements:
                self._require_schema(
                    requirement.schema,
                    "workflow_context_requirement_schema_missing",
                )
            declared = {permission.action for permission in node.permissions}
            # Both resolver phases are authorized independently. Declaring only
            # context.provide would still fail during capabilities discovery, so
            # reject the incomplete definition before the Run starts.
            if any(action not in declared for action in context_actions()):
                self._invalid(
                    "workflow_context_permission_missing",
                    "ContextNode must declare every Context action",
                )
            for provider_id in node.config.binding.provider_ids:
                for action in context_actions():
                    # Action presence is not enough: the permission must name the
                    # exact Provider the embedded binding can dispatch. Otherwise
                    # config A could execute using permission granted for B.
                    if not any(
                        permission.action == action
                        and permission.resource.namespace == "core"
                        and permission.resource.kind == "context_provider"
                        and permission.resource.id.value == provider_id.value
                        and permission.resource.owning_extension is None
                        for permission in node.permissions
                    ):
                        self._invalid(
                            "workflow_context_permission_resource_invalid",
                            "Context permission resource does not match node binding",
                        )
        elif node.node_type == WorkflowNodeType.TOOL.value:
            if not isinstance(node.config, ToolNodeConfig):
                self._invalid(
                    "workflow_tool_config_invalid", "ToolNode requires ToolNodeConfig"
                )
            if self._skill_tools is None:
                raise core_error(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "workflow_tool_resolver_unavailable",
                    "Workflow validation has no SkillToolResolver",
                )
            descriptor = self._skill_tools.resolve_tool(node.config.tool).descriptor
            if (
                node.input_schema != descriptor.input_schema
                or len(node.input_bindings) != 1
                or node.output_schema != TOOL_NODE_RESULT_SCHEMA
            ):
                self._invalid(
                    "workflow_tool_contract_invalid",
                    "ToolNode requires one descriptor input and the fixed "
                    "result schema",
                )
            expected_side_effecting = descriptor.side_effect is not ToolSideEffect.NONE
            if node.side_effecting != expected_side_effecting:
                self._invalid(
                    "workflow_tool_side_effect_invalid",
                    "ToolNode side-effect declaration does not match its descriptor",
                )
            if expected_side_effecting and (
                not node.idempotency_required
                or descriptor.idempotency is not ToolIdempotencyMode.CALLER_KEY
            ):
                self._invalid(
                    "workflow_tool_idempotency_required",
                    "side-effecting ToolNode requires caller-key idempotency",
                )
            if not any(
                permission.action == descriptor.action
                and permission.resource == descriptor.ref.resource
                for permission in node.permissions
            ):
                self._invalid(
                    "workflow_tool_permission_resource_invalid",
                    "Tool permission resource does not match the configured Tool",
                )
        elif node.node_type == WorkflowNodeType.APPROVAL.value:
            if not isinstance(node.config, ApprovalNodeConfig):
                self._invalid(
                    "workflow_approval_config_invalid",
                    "ApprovalNode requires ApprovalNodeConfig",
                )
            if (
                node.input_bindings
                or node.input_schema is not None
                or node.output_schema is not None
            ):
                self._invalid(
                    "workflow_approval_contract_invalid",
                    "ApprovalNode does not accept or produce a value in v0.2",
                )
            node.config.prompt_ref.scope.require_narrower_than(node.scope)
        elif node.node_type == WorkflowNodeType.EVALUATION.value:
            if not isinstance(node.config, EvaluationNodeConfig):
                self._invalid(
                    "workflow_evaluation_config_invalid",
                    "EvaluationNode requires EvaluationNodeConfig",
                )
            if (
                node.input_schema is None
                or node.output_schema != EVALUATION_RESULT_SCHEMA
                or len(node.input_bindings) != 1
            ):
                self._invalid(
                    "workflow_evaluation_contract_incomplete",
                    "EvaluationNode requires one input and the fixed result schema",
                )
            if not node.idempotency_required:
                self._invalid(
                    "workflow_evaluation_idempotency_required",
                    "EvaluationNode requires idempotency",
                )
            declared = {permission.action for permission in node.permissions}
            if any(action not in declared for action in evaluation_actions()):
                self._invalid(
                    "workflow_evaluation_permission_missing",
                    "EvaluationNode must declare every Evaluation action",
                )
            # Action presence alone is insufficient: a node could otherwise
            # declare permission for policy A while its config dispatches policy
            # B.  Bind every action to the exact configured resource up front.
            expected_resources = {
                "evaluation.policy.evaluate": (
                    "evaluation_policy",
                    node.config.policy_ref,
                ),
                "evaluation.quality.capabilities": (
                    "quality_evaluator",
                    node.config.quality_evaluator_id.value,
                ),
                "evaluation.quality.evaluate": (
                    "quality_evaluator",
                    node.config.quality_evaluator_id.value,
                ),
            }
            for action in evaluation_actions():
                kind, resource_id = expected_resources[action.name]
                if not any(
                    permission.action == action
                    and permission.resource.namespace == "core"
                    and permission.resource.kind == kind
                    and permission.resource.id.value == resource_id
                    for permission in node.permissions
                ):
                    self._invalid(
                        "workflow_evaluation_permission_resource_invalid",
                        "Evaluation permission resource does not match node config",
                    )

    def _topological_order(
        self, definition: WorkflowDefinition, nodes: dict[NodeId, WorkflowNode]
    ) -> tuple[NodeId, ...]:
        incoming = {node_id: 0 for node_id in nodes}
        outgoing: dict[NodeId, list[NodeId]] = {node_id: [] for node_id in nodes}
        for dependency in definition.dependencies:
            incoming[dependency.target_node_id] += 1
            outgoing[dependency.source_node_id].append(dependency.target_node_id)
        ready = sorted(
            (node_id for node_id, count in incoming.items() if count == 0),
            key=lambda item: item.value,
        )
        order: list[NodeId] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for target in sorted(outgoing[current], key=lambda item: item.value):
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
                    ready.sort(key=lambda item: item.value)
        if len(order) != len(nodes):
            self._invalid("workflow_cycle", "Workflow dependencies contain a cycle")
        return tuple(order)

    def _validate_bindings(
        self, definition: WorkflowDefinition, nodes: dict[NodeId, WorkflowNode]
    ) -> None:
        dependencies = {
            (item.source_node_id, item.target_node_id): item
            for item in definition.dependencies
        }
        for node in definition.nodes:
            for binding in node.input_bindings:
                if binding.source is WorkflowInputSource.WORKFLOW_INPUT:
                    if node.input_schema != definition.input_schema:
                        self._invalid(
                            "workflow_input_schema_incompatible",
                            "Workflow input and node input schemas must match exactly",
                        )
                    continue
                source_id = binding.source_node_id
                if source_id is None or source_id not in nodes:
                    self._invalid(
                        "workflow_binding_source_missing",
                        "Workflow input binding references a missing node",
                    )
                source = nodes[source_id]
                dependency = dependencies.get((source_id, node.node_id))
                if dependency is None or not dependency.carries_output:
                    self._invalid(
                        "workflow_output_dependency_missing",
                        "node output binding requires an output-carrying dependency",
                    )
                if source.output_schema != node.input_schema:
                    self._invalid(
                        "workflow_node_schema_incompatible",
                        "producer and consumer schemas must match exactly",
                    )
        for dependency in definition.dependencies:
            if not dependency.carries_output:
                continue
            target = nodes[dependency.target_node_id]
            if not any(
                binding.source_node_id == dependency.source_node_id
                for binding in target.input_bindings
            ):
                self._invalid(
                    "workflow_output_dependency_unbound",
                    "output-carrying dependency has no matching input binding",
                )

    def _validate_required_output(
        self, definition: WorkflowDefinition, nodes: dict[NodeId, WorkflowNode]
    ) -> None:
        source_id = definition.output_binding.source_node_id
        source = nodes.get(source_id)
        if source is None:
            self._invalid(
                "workflow_output_node_missing",
                "Workflow output binding references a missing node",
            )
        if not definition.output_binding.required:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "workflow_optional_output_unsupported",
                "the v0.2 direct runtime requires its final output",
            )
        if source.output_schema != definition.output_schema:
            self._invalid(
                "workflow_output_schema_incompatible",
                "Workflow and source node output schemas must match exactly",
            )

        # ContextNode has no Workflow input by design, but it is still a valid
        # graph root because it produces data from an authorized Provider. Without
        # this seed the required-output check would incorrectly call every
        # ContextNode-only Workflow unreachable.
        reachable = {
            node.node_id
            for node in definition.nodes
            if node.node_type
            in {
                WorkflowNodeType.CONTEXT.value,
                WorkflowNodeType.SKILL.value,
            }
            or any(
                binding.source is WorkflowInputSource.WORKFLOW_INPUT
                for binding in node.input_bindings
            )
        }
        changed = True
        while changed:
            changed = False
            for dependency in definition.dependencies:
                if (
                    dependency.source_node_id in reachable
                    and dependency.target_node_id not in reachable
                ):
                    reachable.add(dependency.target_node_id)
                    changed = True
        if source_id not in reachable:
            self._invalid(
                "workflow_required_output_unreachable",
                "required Workflow output is unreachable from Workflow input",
            )

    def _require_schema(self, schema: SchemaRef, code: str) -> None:
        if not self._schemas.contains(schema):
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                code,
                "Workflow references an unregistered schema",
            )

    def _invalid(self, code: str, message: str) -> NoReturn:
        raise core_error(ErrorCategory.INVALID_REQUEST, code, message)
