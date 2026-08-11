"""Pure pre-execution validation for normalized Workflow definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from congeries_core.policy.authorization import ActionRegistry
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import NodeId
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from .model import (
    AgentNodeConfig,
    ApprovalNodeConfig,
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
    ) -> None:
        self._schemas = schemas
        self._actions = actions
        self._supported = supported_nodes or frozenset(
            {
                (WorkflowNodeType.AGENT.value, "1"),
                (WorkflowNodeType.APPROVAL.value, "1"),
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

        reachable = {
            node.node_id
            for node in definition.nodes
            if any(
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
