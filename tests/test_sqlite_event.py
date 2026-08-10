from __future__ import annotations

from dataclasses import replace

import pytest

from congeries_core.adapter.sqlite_event import SqliteEventLedger
from congeries_core.event.model import (
    ClassifiedPayload,
    CoreEventType,
    DeliveryClass,
    EventAcknowledgement,
    PayloadField,
    RuntimeEvent,
    Sensitivity,
)
from congeries_core.policy.authorization import CorePrincipalKind, RuntimePrincipal
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import AcknowledgementId, EventId, PrincipalId

from .support import NOW, call_context


def sqlite_event(event_id: str = "event-1") -> RuntimeEvent:
    context = call_context()
    return RuntimeEvent(
        EventId(event_id),
        CoreEventType.ARTIFACT_CREATED.value,
        "1",
        NOW,
        context.run_id,
        context.root_run_id,
        None,
        1,
        context.scope,
        context.trace.correlation_id,
        None,
        Sensitivity.INTERNAL,
        DeliveryClass.AUDIT,
        ClassifiedPayload({"artifact": PayloadField("artifact-1")}),
    )


@pytest.mark.asyncio
async def test_sqlite_sequence_and_pending_survive_restart(tmp_path) -> None:
    path = tmp_path / "events.sqlite3"
    first = SqliteEventLedger(path)
    context = call_context()
    principal = RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1"))
    event = sqlite_event()
    assert await first.next_sequence(context.run_id) == 1
    assert await first.next_sequence(context.run_id) == 2
    await first.enqueue(event, "audit", context, principal)
    await first.mark_attempt(event.event_id, "audit", "temporary")

    restarted = SqliteEventLedger(path)
    assert await restarted.next_sequence(context.run_id) == 3
    pending = await restarted.pending()
    assert len(pending) == 1
    assert pending[0].event == event
    assert pending[0].attempts == 1
    assert pending[0].last_error == "temporary"

    acknowledgement = EventAcknowledgement(
        AcknowledgementId("ack-1"), event.event_id, "audit", event.payload_digest, NOW
    )
    await restarted.acknowledge(acknowledgement)
    await restarted.acknowledge(acknowledgement)
    assert await restarted.pending() == ()


@pytest.mark.asyncio
async def test_sqlite_identity_and_acknowledgement_conflicts(tmp_path) -> None:
    ledger = SqliteEventLedger(tmp_path / "conflicts.sqlite3")
    context = call_context()
    principal = RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1"))
    event = sqlite_event()
    await ledger.enqueue(event, "audit", context, principal)
    await ledger.enqueue(event, "audit", context, principal)
    with pytest.raises(CoreError):
        await ledger.enqueue(
            replace(event, payload=ClassifiedPayload({"changed": PayloadField(True)})),
            "audit",
            context,
            principal,
        )
    with pytest.raises(CoreError):
        await ledger.mark_attempt(EventId("missing"), "audit", None)
    with pytest.raises(CoreError):
        await ledger.acknowledge(
            EventAcknowledgement(
                AcknowledgementId("ack"), event.event_id, "audit", "wrong", NOW
            )
        )
    with pytest.raises(ValueError):
        await ledger.pending(0)
