from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from congeries_core.event.model import CoreEventType
from congeries_core.harness.agent import AgentSpec
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
from congeries_core.runtime.content import ContentBlock
from congeries_core.runtime.json_types import as_array, as_object

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
