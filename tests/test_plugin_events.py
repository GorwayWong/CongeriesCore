from __future__ import annotations

import pytest

from congeries_core.event import (
    DeliveryClass,
    EventDispatcher,
    EventSinkCapabilities,
    InMemoryEventLedger,
    InMemoryEventSink,
    Sensitivity,
    SinkRegistration,
    core_schema_registry,
)
from congeries_core.plugin import (
    PluginLifecycleState,
    RuntimePluginEventPublisher,
)
from congeries_core.runtime.errors import CoreError, ErrorCategory, ErrorDetail

from .plugin_support import manifest
from .support import FixedClock, MatchingAllowPolicy, call_context


@pytest.mark.asyncio
async def test_plugin_events_are_reliable_deduplicated_and_reference_only() -> None:
    sink = InMemoryEventSink(
        "plugin-events",
        EventSinkCapabilities(
            frozenset({DeliveryClass.AUDIT, DeliveryClass.OBSERVABILITY}),
            frozenset({("*", "*")}),
            Sensitivity.RESTRICTED,
            acknowledgement=True,
        ),
    )
    ledger = InMemoryEventLedger()
    dispatcher = EventDispatcher(
        sequence_store=ledger,
        outbox=ledger,
        schema_registry=core_schema_registry(),
        sinks=(SinkRegistration(sink, required_for_audit=True),),
        clock=FixedClock(),
        authorization_policy=MatchingAllowPolicy(),
    )
    publisher = RuntimePluginEventPublisher(dispatcher)
    plugin = manifest().ref
    context = call_context()

    for _ in range(2):
        await publisher.transition_requested(
            plugin,
            "active",
            PluginLifecycleState.DRAINING,
            "operation-1",
            2,
            context,
        )
    await publisher.lifecycle_changed(
        plugin,
        "active",
        PluginLifecycleState.DRAINING,
        "operation-1",
        2,
        context,
    )
    await publisher.lifecycle_failed(
        plugin,
        "draining",
        PluginLifecycleState.UNLOADED,
        "operation-1",
        1,
        ErrorDetail(
            ErrorCategory.PROTOCOL_FAILURE,
            "unload_failed",
            "secret exception text must not appear",
            retryable=True,
        ),
        context,
    )
    await dispatcher.flush()
    await dispatcher.close()

    event_types = [event.event_type for event in sink.events]
    assert event_types.count("core.plugin.lifecycle_transition_requested") == 1
    assert event_types.count("core.plugin.lifecycle_changed") == 1
    assert event_types.count("core.plugin.lifecycle_failed") == 1
    serialized = str([event.to_data() for event in sink.events])
    assert "secret exception text" not in serialized
    assert "entrypoint" not in serialized
    failed = next(
        event
        for event in sink.events
        if event.event_type == "core.plugin.lifecycle_failed"
    )
    assert failed.payload.visible_data()["error_code"] == "unload_failed"


@pytest.mark.asyncio
async def test_plugin_audit_requires_a_required_sink() -> None:
    ledger = InMemoryEventLedger()
    dispatcher = EventDispatcher(
        sequence_store=ledger,
        outbox=ledger,
        schema_registry=core_schema_registry(),
        sinks=(),
        clock=FixedClock(),
        authorization_policy=MatchingAllowPolicy(),
    )
    publisher = RuntimePluginEventPublisher(dispatcher)

    with pytest.raises(CoreError) as raised:
        await publisher.transition_requested(
            manifest().ref,
            "registered",
            PluginLifecycleState.ACTIVE,
            "operation-2",
            0,
            call_context(),
        )
    assert raised.value.detail.code == "required_audit_sink_missing"
    await dispatcher.close()
