from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from congeries_core.evaluation import EVALUATION_RESULT_SCHEMA, evaluation_actions
from congeries_core.policy.authorization import ActionRef, ActionRegistry, ResourceRef
from congeries_core.provider.context import (
    ContextBinding,
    ContextCompleteness,
    ContextEntry,
    ContextKey,
    ContextRequirement,
    ContextUsage,
    ResolvedContext,
    context_actions,
)
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import (
    AgentId,
    DefinitionId,
    ModelBindingRef,
    NodeId,
    ProviderId,
    ResourceId,
    WorkflowId,
)
from congeries_core.runtime.json_types import as_json_value
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.workflow import (
    CONTEXT_NODE_RESULT_SCHEMA,
    WORKFLOW_NODE_EXECUTE_ACTION,
    AgentNodeConfig,
    ContextNodeConfig,
    ContextNodeResult,
    ContextNodeResultSchemaValidator,
    DeterministicScheduler,
    EvaluationNodeConfig,
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


def _evaluation_node() -> WorkflowNode:
    permissions = (
        _permission(),
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
        node_id=NodeId("evaluate"),
        node_type=WorkflowNodeType.EVALUATION.value,
        contract_version="1",
        input_schema=SCHEMA,
        input_bindings=(WorkflowInputBinding(WorkflowInputSource.WORKFLOW_INPUT),),
        output_schema=EVALUATION_RESULT_SCHEMA,
        scope=child_scope(),
        permissions=permissions,
        timeout_seconds=30,
        retry_limit=0,
        side_effecting=True,
        idempotency_required=True,
        checkpoint=True,
        config=EvaluationNodeConfig(
            "policy-1", ProviderId("quality-1"), "external:profile-1"
        ),
    )


def _context_node() -> WorkflowNode:
    provider_id = ProviderId("context-1")
    permissions = (
        _permission(),
        *(
            WorkflowPermission(
                action,
                ResourceRef("core", "context_provider", ResourceId(provider_id.value)),
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
        scope=child_scope(),
        permissions=permissions,
        timeout_seconds=30,
        retry_limit=0,
        side_effecting=False,
        idempotency_required=True,
        checkpoint=True,
        config=ContextNodeConfig(
            ContextBinding(
                provider_ids=(provider_id,),
                requirements=(
                    ContextRequirement(ContextKey("test", "profile"), SCHEMA),
                ),
            )
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
        registry.register(
            schema,
            ContextNodeResultSchemaValidator()
            if schema == CONTEXT_NODE_RESULT_SCHEMA
            else StringObjectValidator(),
        )
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
            "workflow_tool_config_invalid",
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


def test_evaluation_node_round_trips_and_requires_fixed_contract() -> None:
    node = _evaluation_node()
    definition = replace(
        _definition(nodes=(node,)),
        output_schema=EVALUATION_RESULT_SCHEMA,
        output_binding=WorkflowOutputBinding(node.node_id),
    )
    actions = (*workflow_actions(), *evaluation_actions())
    validated = _validator(
        schemas=(SCHEMA, EVALUATION_RESULT_SCHEMA), actions=actions
    ).validate(definition)
    assert WorkflowDefinition.from_data(definition.to_data()) == definition
    assert validated.definition.nodes[0].config == node.config

    for changed, code in (
        (
            replace(node, output_schema=SCHEMA),
            "workflow_evaluation_contract_incomplete",
        ),
        (
            replace(node, side_effecting=False, idempotency_required=False),
            "workflow_evaluation_idempotency_required",
        ),
        (
            replace(node, permissions=(_permission(),)),
            "workflow_evaluation_permission_missing",
        ),
        (
            replace(
                node,
                permissions=(
                    node.permissions[0],
                    replace(
                        node.permissions[1],
                        resource=ResourceRef(
                            "core", "evaluation_policy", ResourceId("wrong")
                        ),
                    ),
                    *node.permissions[2:],
                ),
            ),
            "workflow_evaluation_permission_resource_invalid",
        ),
    ):
        invalid = replace(
            definition,
            nodes=(changed,),
            output_schema=changed.output_schema or EVALUATION_RESULT_SCHEMA,
        )
        with pytest.raises(CoreError) as error:
            _validator(
                schemas=(SCHEMA, EVALUATION_RESULT_SCHEMA), actions=actions
            ).validate(invalid)
        assert error.value.detail.code == code


def test_context_node_contract_result_and_validation_are_frozen() -> None:
    node = _context_node()
    definition = replace(
        _definition(nodes=(node,)),
        output_schema=CONTEXT_NODE_RESULT_SCHEMA,
        output_binding=WorkflowOutputBinding(node.node_id),
    )
    actions = (*workflow_actions(), *context_actions())
    validator = _validator(
        schemas=(SCHEMA, CONTEXT_NODE_RESULT_SCHEMA), actions=actions
    )
    assert validator.validate(definition).topological_order == (node.node_id,)
    assert WorkflowDefinition.from_data(definition.to_data()) == definition

    resolved = ResolvedContext(
        entries=(
            ContextEntry(
                ContextKey("test", "profile"),
                SCHEMA,
                {"value": "Ada"},
                ("context-1",),
            ),
        ),
        completeness=ContextCompleteness.COMPLETE,
        missing_keys=(),
        warnings=(),
        selected_providers=(ProviderId("context-1"),),
        usage=ContextUsage(15),
    )
    result = ContextNodeResult.from_resolved(resolved)
    assert ContextNodeResult.from_data(result.to_data()) == result
    ContextNodeResultSchemaValidator().validate(
        as_json_value(result.to_data(), "ContextNode result")
    )
    with pytest.raises(FrozenInstanceError):
        result.contract_version = "2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown or missing"):
        ContextNodeResult.from_data({**result.to_data(), "extra": True})
    with pytest.raises(ValueError, match="unsupported"):
        ContextNodeResult.from_data({**result.to_data(), "contract_version": "2"})

    invalid_cases = (
        (
            replace(node, input_schema=SCHEMA),
            "workflow_context_contract_invalid",
        ),
        (
            replace(node, output_schema=SCHEMA),
            "workflow_context_contract_invalid",
        ),
        (
            replace(node, side_effecting=True),
            "workflow_context_side_effect_invalid",
        ),
        (
            replace(node, idempotency_required=False),
            "workflow_context_idempotency_required",
        ),
        (
            replace(node, permissions=(_permission(),)),
            "workflow_context_permission_missing",
        ),
        (
            replace(
                node,
                permissions=(
                    node.permissions[0],
                    replace(
                        node.permissions[1],
                        resource=ResourceRef(
                            "core", "context_provider", ResourceId("wrong")
                        ),
                    ),
                    node.permissions[2],
                ),
            ),
            "workflow_context_permission_resource_invalid",
        ),
    )
    for changed, code in invalid_cases:
        invalid = replace(
            definition,
            nodes=(changed,),
            output_schema=changed.output_schema or CONTEXT_NODE_RESULT_SCHEMA,
        )
        with pytest.raises(CoreError) as error:
            validator.validate(invalid)
        assert error.value.detail.code == code

    missing_requirement = SchemaRef("test", "missing_context", "1")
    assert isinstance(node.config, ContextNodeConfig)
    missing_schema_node = replace(
        node,
        config=ContextNodeConfig(
            replace(
                node.config.binding,
                requirements=(
                    ContextRequirement(
                        ContextKey("test", "profile"), missing_requirement
                    ),
                ),
            )
        ),
    )
    with pytest.raises(CoreError) as missing_schema:
        _validator(
            schemas=(SCHEMA, CONTEXT_NODE_RESULT_SCHEMA), actions=actions
        ).validate(replace(definition, nodes=(missing_schema_node,)))
    assert (
        missing_schema.value.detail.code
        == "workflow_context_requirement_schema_missing"
    )

    with pytest.raises(CoreError) as missing_action:
        _validator(
            schemas=(SCHEMA, CONTEXT_NODE_RESULT_SCHEMA),
            actions=(*workflow_actions(), context_actions()[0]),
        ).validate(definition)
    assert missing_action.value.detail.code == "workflow_permission_not_evaluable"
