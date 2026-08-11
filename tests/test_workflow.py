from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from congeries_core.policy.authorization import ActionRef, ActionRegistry, ResourceRef
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import (
    AgentId,
    DefinitionId,
    ModelBindingRef,
    NodeId,
    ResourceId,
    WorkflowId,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.workflow import (
    WORKFLOW_NODE_EXECUTE_ACTION,
    AgentNodeConfig,
    DeterministicScheduler,
    ExecutionPolicy,
    UnsupportedNodeConfig,
    WorkflowDefinition,
    WorkflowDependency,
    WorkflowInputBinding,
    WorkflowInputSource,
    WorkflowNode,
    WorkflowNodeType,
    WorkflowOutputBinding,
    WorkflowPermission,
    WorkflowValidator,
    workflow_actions,
)

from .provider_support import StringObjectValidator
from .support import child_scope

SCHEMA = SchemaRef("test", "workflow_value", "1")


def _permission(action: ActionRef = WORKFLOW_NODE_EXECUTE_ACTION) -> WorkflowPermission:
    return WorkflowPermission(
        action,
        ResourceRef("core", "workflow_node", ResourceId("node-resource")),
    )


def _agent_node(
    value: str,
    *,
    source: NodeId | None = None,
    schema: SchemaRef = SCHEMA,
) -> WorkflowNode:
    return WorkflowNode(
        node_id=NodeId(value),
        node_type=WorkflowNodeType.AGENT.value,
        contract_version="1",
        input_schema=schema,
        input_bindings=(
            WorkflowInputBinding(
                WorkflowInputSource.NODE_OUTPUT
                if source
                else WorkflowInputSource.WORKFLOW_INPUT,
                source,
            ),
        ),
        output_schema=schema,
        scope=child_scope(),
        permissions=(_permission(),),
        timeout_seconds=30,
        retry_limit=0,
        side_effecting=False,
        idempotency_required=False,
        checkpoint=True,
        config=AgentNodeConfig(
            AgentId(f"agent-{value}"),
            DefinitionId(f"agent-definition-{value}"),
            ModelBindingRef("model-1"),
        ),
    )


def _definition(
    nodes: tuple[WorkflowNode, ...] | None = None,
    dependencies: tuple[WorkflowDependency, ...] = (),
    *,
    output: NodeId | None = None,
) -> WorkflowDefinition:
    actual_nodes = nodes or (_agent_node("a"),)
    return WorkflowDefinition(
        workflow_id=WorkflowId("workflow-1"),
        definition_id=DefinitionId("workflow-definition-1"),
        version="1",
        input_schema=SCHEMA,
        nodes=actual_nodes,
        dependencies=dependencies,
        output_schema=SCHEMA,
        output_binding=WorkflowOutputBinding(output or actual_nodes[-1].node_id),
        execution_policy=ExecutionPolicy(),
    )


def _validator(
    *,
    schemas: tuple[SchemaRef, ...] = (SCHEMA,),
    actions: tuple[ActionRef, ...] = workflow_actions(),
) -> WorkflowValidator:
    registry = SchemaRegistry()
    for schema in schemas:
        registry.register(schema, StringObjectValidator())
    return WorkflowValidator(
        schemas=registry,
        actions=ActionRegistry(actions),
    )


def _assert_code(definition: WorkflowDefinition, code: str) -> None:
    with pytest.raises(CoreError) as error:
        _validator().validate(definition)
    assert error.value.detail.code == code


def test_workflow_definition_is_frozen_strict_and_round_trips() -> None:
    definition = _definition()
    assert WorkflowDefinition.from_data(definition.to_data()) == definition
    with pytest.raises(FrozenInstanceError):
        definition.version = "2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown or missing"):
        WorkflowDefinition.from_data({**definition.to_data(), "extra": True})
    with pytest.raises(ValueError, match="unsupported Workflow contract"):
        WorkflowDefinition.from_data({**definition.to_data(), "contract_version": "2"})


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        (
            _definition(nodes=(_agent_node("a"), _agent_node("a"))),
            "workflow_duplicate_node",
        ),
        (
            _definition(
                dependencies=(WorkflowDependency(NodeId("a"), NodeId("missing")),)
            ),
            "workflow_dependency_node_missing",
        ),
        (
            _definition(
                nodes=(_agent_node("a"), _agent_node("b")),
                dependencies=(
                    WorkflowDependency(NodeId("a"), NodeId("b")),
                    WorkflowDependency(NodeId("b"), NodeId("a")),
                ),
            ),
            "workflow_cycle",
        ),
        (
            replace(
                _definition(),
                nodes=(
                    replace(
                        _agent_node("a"),
                        node_type=WorkflowNodeType.TOOL.value,
                        config=UnsupportedNodeConfig({}),
                    ),
                ),
            ),
            "workflow_node_contract_unsupported",
        ),
        (
            replace(
                _definition(),
                nodes=(
                    replace(
                        _agent_node("a"),
                        permissions=(_permission(ActionRef("test", "unknown", "1")),),
                    ),
                ),
            ),
            "workflow_node_execute_permission_missing",
        ),
        (
            replace(
                _definition(),
                nodes=(
                    replace(
                        _agent_node("a"),
                        side_effecting=True,
                        idempotency_required=False,
                    ),
                ),
            ),
            "workflow_node_idempotency_required",
        ),
        (
            _definition(
                nodes=(
                    _agent_node("a"),
                    _agent_node(
                        "b",
                        source=NodeId("a"),
                        schema=SchemaRef("test", "other", "1"),
                    ),
                ),
                dependencies=(WorkflowDependency(NodeId("a"), NodeId("b"), True),),
            ),
            "workflow_node_input_schema_missing",
        ),
        (
            replace(
                _definition(),
                output_binding=WorkflowOutputBinding(NodeId("missing")),
            ),
            "workflow_output_node_missing",
        ),
    ],
)
def test_validator_rejects_invalid_graphs(
    definition: WorkflowDefinition, code: str
) -> None:
    _assert_code(definition, code)


def test_validator_rejects_exact_schema_mismatch_after_both_are_registered() -> None:
    other = SchemaRef("test", "other", "1")
    definition = _definition(
        nodes=(
            _agent_node("a"),
            _agent_node("b", source=NodeId("a"), schema=other),
        ),
        dependencies=(WorkflowDependency(NodeId("a"), NodeId("b"), True),),
    )
    with pytest.raises(CoreError) as error:
        _validator(schemas=(SCHEMA, other)).validate(definition)
    assert error.value.detail.code == "workflow_node_schema_incompatible"


def test_validator_rejects_unsupported_execution_policy() -> None:
    definition = replace(
        _definition(), execution_policy=ExecutionPolicy(max_concurrency=2)
    )
    _assert_code(definition, "workflow_policy_unsupported")


def test_scheduler_is_sorted_and_never_releases_unmet_dependencies() -> None:
    nodes = tuple(_agent_node(value) for value in ("d", "c", "b", "a"))
    definition = _definition(
        nodes=nodes,
        dependencies=(
            WorkflowDependency(NodeId("a"), NodeId("c")),
            WorkflowDependency(NodeId("b"), NodeId("c")),
            WorkflowDependency(NodeId("c"), NodeId("d")),
        ),
        output=NodeId("d"),
    )
    scheduler = DeterministicScheduler(_validator().validate(definition))

    assert tuple(node.node_id.value for node in scheduler.ready()) == ("a", "b")
    with pytest.raises(CoreError) as error:
        scheduler.mark_completed(NodeId("c"))
    assert error.value.detail.code == "workflow_node_not_ready"

    assert scheduler.next().node_id == NodeId("a")  # type: ignore[union-attr]
    scheduler.mark_completed(NodeId("a"))
    assert scheduler.next().node_id == NodeId("b")  # type: ignore[union-attr]
    scheduler.mark_completed(NodeId("b"))
    assert scheduler.next().node_id == NodeId("c")  # type: ignore[union-attr]
    scheduler.mark_completed(NodeId("c"))
    scheduler.mark_completed(NodeId("d"))
    assert scheduler.done
    assert scheduler.pending_nodes() == ()
