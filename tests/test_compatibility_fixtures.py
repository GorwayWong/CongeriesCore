from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from congeries_core.checkpoint import (
    ApprovalCheckpointState,
    Checkpoint,
    CheckpointMigrationRequest,
    CheckpointPage,
    checkpoint_actions,
)
from congeries_core.evaluation import (
    EvaluationRequest,
    EvaluationResult,
    QualityEvaluatorCapabilities,
    evaluation_actions,
)
from congeries_core.event.model import CoreEventType
from congeries_core.harness.agent import AgentSpec
from congeries_core.mcp import (
    McpAdapterDescriptor,
    McpDiscoverySnapshot,
    McpResourceRequest,
    McpResourceResponse,
    McpToolRequest,
    McpToolResponse,
    mcp_actions,
)
from congeries_core.plugin import ManifestValidator, plugin_actions
from congeries_core.policy.authorization import ActionRef
from congeries_core.provider import provider_actions
from congeries_core.provider.context import ContextBinding
from congeries_core.provider.memory import (
    MemoryCapabilities,
    MemoryItem,
    MemoryPage,
    MemoryQuery,
)
from congeries_core.provider.model import ModelBinding
from congeries_core.provider.storage import (
    ArtifactPage,
    ArtifactQuery,
    ArtifactReference,
    ArtifactValue,
    StorageCapabilities,
    storage_actions,
)
from congeries_core.runtime.content import ContentBlock
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.json_types import as_array, as_object
from congeries_core.skill import (
    SkillDescriptor,
    SkillResource,
    SkillResourceRequest,
    skill_actions,
)
from congeries_core.state.workspace import WorkspaceState
from congeries_core.tool import (
    ToolCall,
    ToolDescriptor,
    ToolResult,
    tool_actions,
)
from congeries_core.workflow import (
    WorkflowContext,
    WorkflowDefinition,
    WorkflowResult,
    WorkflowSuspension,
    workflow_actions,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v0.2"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _object(name: str) -> dict[str, object]:
    return as_object(json.loads(_text(name)), name)


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def test_content_context_model_and_agent_spec_v02_fixtures() -> None:
    content_data = _object("content.json")
    content = ContentBlock.from_data(content_data)
    assert ContentBlock.from_data(content.to_data()) == content
    assert _serialized(content.to_data()) == _text("content.json")

    context_data = _object("context.json")
    context = ContextBinding.from_data(context_data)
    assert ContextBinding.from_data(context.to_data()) == context
    assert _serialized(context.to_data()) == _text("context.json")

    model_data = _object("model.json")
    model = ModelBinding.from_data(model_data)
    assert ModelBinding.from_data(model.to_data()) == model
    assert _serialized(model.to_data()) == _text("model.json")

    agent_data = _object("agent_spec.json")
    agent = AgentSpec.from_data(agent_data)
    assert AgentSpec.from_data(agent.to_data()) == agent
    assert _serialized(agent.to_data()) == _text("agent_spec.json")
    upgraded = agent.upgrade_v2()
    assert upgraded.contract_version == "2"
    assert upgraded.to_data()["contract_version"] == "2"

    agent_v2 = AgentSpec.from_data(_object("agent_spec_v2.json"))
    assert agent_v2.contract_version == "2"
    assert AgentSpec.from_data(agent_v2.to_data()) == agent_v2
    assert _serialized(agent_v2.to_data()) == _text("agent_spec_v2.json")
    malformed_v2 = _object("agent_spec_v2.json")
    malformed_v2["contract_version"] = 2
    with pytest.raises(ValueError, match="must be a string"):
        AgentSpec.from_data(malformed_v2)

    legacy_unowned = _object("agent_spec.json")
    legacy_unowned["skill_refs"] = [
        {
            "namespace": "core",
            "kind": "skill",
            "id": "legacy.skill",
            "owning_extension": None,
        }
    ]
    decoded_unowned = AgentSpec.from_data(legacy_unowned)
    assert decoded_unowned.to_data() == legacy_unowned
    with pytest.raises(ValueError, match="owning extension"):
        decoded_unowned.upgrade_v2()


def test_memory_v02_fixture_round_trips_exactly() -> None:
    data = _object("memory.json")
    query = MemoryQuery.from_data(as_object(data["query"], "memory query"))
    item = MemoryItem.from_data(as_object(data["item"], "memory item"))
    page = MemoryPage.from_data(as_object(data["page"], "memory page"))
    capabilities = MemoryCapabilities.from_data(
        as_object(data["capabilities"], "memory capabilities")
    )
    reconstructed: dict[str, object] = {
        "query": query.to_data(),
        "item": item.to_data(),
        "page": page.to_data(),
        "capabilities": capabilities.to_data(),
    }
    assert MemoryQuery.from_data(query.to_data()) == query
    assert MemoryItem.from_data(item.to_data()) == item
    assert MemoryPage.from_data(page.to_data()) == page
    assert MemoryCapabilities.from_data(capabilities.to_data()) == capabilities
    assert reconstructed == data
    assert _serialized(reconstructed) == _text("memory.json")


def test_storage_v1_fixtures_round_trip_exactly() -> None:
    data = _object("storage.json")
    contracts = (
        ("capabilities", StorageCapabilities),
        ("workspace", WorkspaceState),
        ("artifact_value", ArtifactValue),
        ("artifact_reference", ArtifactReference),
        ("artifact_query", ArtifactQuery),
        ("artifact_page", ArtifactPage),
    )
    reconstructed: dict[str, object] = {}
    for name, contract in contracts:
        value = contract.from_data(as_object(data[name], name))
        assert contract.from_data(value.to_data()) == value
        reconstructed[name] = value.to_data()
    assert reconstructed == data
    assert _serialized(reconstructed) == _text("storage.json")

    action_values = as_array(
        json.loads(_text("storage_actions.json")), "Storage actions"
    )
    actions = tuple(
        ActionRef.from_data(as_object(item, "Storage action")) for item in action_values
    )
    assert actions == storage_actions()
    assert _serialized([item.to_data() for item in actions]) == _text(
        "storage_actions.json"
    )


def test_provider_action_and_core_event_catalog_v02_fixtures() -> None:
    action_values = as_array(
        json.loads(_text("provider_actions.json")), "Provider action fixture"
    )
    actions = tuple(
        ActionRef.from_data(as_object(item, "Provider action"))
        for item in action_values
    )
    assert actions == provider_actions()
    action_data = [action.to_data() for action in actions]
    assert _serialized(action_data) == _text("provider_actions.json")

    event_values = cast(
        list[str], as_array(json.loads(_text("core_events.json")), "Core events")
    )
    events = [event.value for event in CoreEventType]
    assert event_values == events
    assert _serialized(events) == _text("core_events.json")


def test_mcp_v1_fixtures_round_trip_exactly() -> None:
    adapter = McpAdapterDescriptor.from_data(_object("mcp_adapter.json"))
    assert McpAdapterDescriptor.from_data(adapter.to_data()) == adapter
    assert _serialized(adapter.to_data()) == _text("mcp_adapter.json")

    discovery = McpDiscoverySnapshot.from_data(_object("mcp_discovery.json"))
    assert McpDiscoverySnapshot.from_data(discovery.to_data()) == discovery
    assert _serialized(discovery.to_data()) == _text("mcp_discovery.json")

    records = _object("mcp_records.json")
    contracts = (
        ("tool_request", McpToolRequest),
        ("tool_response", McpToolResponse),
        ("resource_request", McpResourceRequest),
        ("resource_response", McpResourceResponse),
    )
    reconstructed: dict[str, object] = {}
    for name, contract in contracts:
        value = contract.from_data(as_object(records[name], name))
        assert contract.from_data(value.to_data()) == value
        reconstructed[name] = value.to_data()
    assert reconstructed == records
    assert _serialized(reconstructed) == _text("mcp_records.json")

    action_values = as_array(json.loads(_text("mcp_actions.json")), "MCP actions")
    actions = tuple(
        ActionRef.from_data(as_object(item, "MCP action")) for item in action_values
    )
    assert actions == mcp_actions()
    assert _serialized([item.to_data() for item in actions]) == _text(
        "mcp_actions.json"
    )

    error_values = as_array(json.loads(_text("mcp_errors.json")), "MCP errors")
    errors = tuple(
        ErrorDetail.from_data(as_object(item, "MCP error")) for item in error_values
    )
    assert tuple(ErrorDetail.from_data(item.to_data()) for item in errors) == errors
    assert _serialized([item.to_data() for item in errors]) == _text("mcp_errors.json")


def test_checkpoint_approval_migration_and_action_v02_fixtures() -> None:
    checkpoint = Checkpoint.from_data(_object("checkpoint.json"))
    assert Checkpoint.from_data(checkpoint.to_data()) == checkpoint
    assert checkpoint.integrity.digest == (
        "afbd705286dccefe8d1aa243183c3ae0023f25525ae211e1a5d04847719f7967"
    )
    assert _serialized(checkpoint.to_data()) == _text("checkpoint.json")

    page = CheckpointPage.from_data(_object("checkpoint_page.json"))
    assert CheckpointPage.from_data(page.to_data()) == page
    assert _serialized(page.to_data()) == _text("checkpoint_page.json")

    migration = CheckpointMigrationRequest.from_data(
        _object("checkpoint_migration.json")
    )
    assert CheckpointMigrationRequest.from_data(migration.to_data()) == migration
    assert _serialized(migration.to_data()) == _text("checkpoint_migration.json")

    approval = ApprovalCheckpointState.from_data(_object("approval.json"))
    assert ApprovalCheckpointState.from_data(approval.to_data()) == approval
    assert _serialized(approval.to_data()) == _text("approval.json")

    values = as_array(
        json.loads(_text("checkpoint_actions.json")), "Checkpoint action fixture"
    )
    actions = tuple(
        ActionRef.from_data(as_object(item, "Checkpoint action")) for item in values
    )
    assert actions == checkpoint_actions()
    assert _serialized([action.to_data() for action in actions]) == _text(
        "checkpoint_actions.json"
    )


def test_workflow_v02_fixtures_round_trip_exactly() -> None:
    for name, contract in (
        ("workflow_definition.json", WorkflowDefinition),
        ("workflow_context.json", WorkflowContext),
        ("workflow_result.json", WorkflowResult),
        ("workflow_suspension.json", WorkflowSuspension),
    ):
        value = contract.from_data(_object(name))
        assert contract.from_data(value.to_data()) == value
        assert _serialized(value.to_data()) == _text(name)

    values = as_array(
        json.loads(_text("workflow_actions.json")), "Workflow action fixture"
    )
    actions = tuple(
        ActionRef.from_data(as_object(item, "Workflow action")) for item in values
    )
    assert actions == workflow_actions()
    assert _serialized([action.to_data() for action in actions]) == _text(
        "workflow_actions.json"
    )


def test_evaluation_v02_fixtures_round_trip_exactly() -> None:
    for name, contract in (
        ("evaluation_request.json", EvaluationRequest),
        ("evaluation_result_passed.json", EvaluationResult),
        ("evaluation_result_denied.json", EvaluationResult),
        ("evaluation_result_failed.json", EvaluationResult),
        ("evaluation_capabilities.json", QualityEvaluatorCapabilities),
        ("workflow_evaluation_definition.json", WorkflowDefinition),
    ):
        value = contract.from_data(_object(name))
        assert contract.from_data(value.to_data()) == value
        assert _serialized(value.to_data()) == _text(name)

    values = as_array(
        json.loads(_text("evaluation_actions.json")), "Evaluation action fixture"
    )
    actions = tuple(
        ActionRef.from_data(as_object(item, "Evaluation action")) for item in values
    )
    assert actions == evaluation_actions()
    assert _serialized([action.to_data() for action in actions]) == _text(
        "evaluation_actions.json"
    )


def test_plugin_v1_manifest_action_and_event_fixtures_round_trip_exactly() -> None:
    manifest = ManifestValidator().validate(_object("plugin_manifest.json"))
    assert ManifestValidator().validate(manifest.to_data()) == manifest
    assert _serialized(manifest.to_data()) == _text("plugin_manifest.json")

    values = as_array(json.loads(_text("plugin_actions.json")), "Plugin actions")
    actions = tuple(
        ActionRef.from_data(as_object(item, "Plugin action")) for item in values
    )
    assert actions == plugin_actions()
    assert _serialized([action.to_data() for action in actions]) == _text(
        "plugin_actions.json"
    )

    event_values = cast(
        list[str], as_array(json.loads(_text("plugin_events.json")), "Plugin events")
    )
    assert event_values == [
        CoreEventType.PLUGIN_LIFECYCLE_TRANSITION_REQUESTED.value,
        CoreEventType.PLUGIN_LIFECYCLE_CHANGED.value,
        CoreEventType.PLUGIN_LIFECYCLE_FAILED.value,
    ]
    assert _serialized(event_values) == _text("plugin_events.json")


def test_skill_and_tool_v1_fixtures_round_trip_exactly() -> None:
    for name, contract in (
        ("skill_descriptor.json", SkillDescriptor),
        ("skill_resource_request.json", SkillResourceRequest),
        ("skill_resource.json", SkillResource),
        ("tool_descriptor.json", ToolDescriptor),
        ("tool_call.json", ToolCall),
        ("tool_result.json", ToolResult),
    ):
        value = contract.from_data(_object(name))
        assert contract.from_data(value.to_data()) == value
        assert _serialized(value.to_data()) == _text(name)

    for name, expected in (
        ("skill_actions.json", skill_actions()),
        ("tool_actions.json", tool_actions()),
    ):
        values = as_array(json.loads(_text(name)), name)
        actions = tuple(
            ActionRef.from_data(as_object(item, "Skill/Tool action")) for item in values
        )
        assert actions == expected
        assert _serialized([action.to_data() for action in actions]) == _text(name)
