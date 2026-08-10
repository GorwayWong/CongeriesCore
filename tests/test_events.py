from __future__ import annotations

from dataclasses import replace

import pytest

from congeries_core.event.dispatcher import (
    EventDeliveryPolicy,
    EventDispatcher,
    SinkRegistration,
)
from congeries_core.event.memory import InMemoryEventLedger, InMemoryEventSink
from congeries_core.event.model import (
    ClassifiedPayload,
    CoreEventType,
    DeliveryClass,
    EventAcknowledgement,
    EventSinkCapabilities,
    PayloadField,
    RuntimeEvent,
    Sensitivity,
)
from congeries_core.event.redaction import ExplicitSensitivityRedactionPolicy
from congeries_core.event.schema import core_schema_registry
from congeries_core.policy.authorization import CorePrincipalKind, RuntimePrincipal
from congeries_core.runtime.errors import CoreError, ErrorCategory
from congeries_core.runtime.ids import (
    AcknowledgementId,
    EventId,
    PrincipalId,
    RunId,
)

from .support import NOW, DenyingPolicy, FixedClock, MatchingAllowPolicy, call_context


def capabilities(
    *delivery: DeliveryClass,
    sensitivity: Sensitivity = Sensitivity.RESTRICTED,
    acknowledgement: bool = True,
    schemas: frozenset[tuple[str, str]] = frozenset({("*", "*")}),
) -> EventSinkCapabilities:
    return EventSinkCapabilities(
        frozenset(delivery), schemas, sensitivity, acknowledgement
    )


def principal() -> RuntimePrincipal:
    return RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1"))


def dispatcher(
    *registrations: SinkRegistration,
    ledger: InMemoryEventLedger | None = None,
    policy=None,
    delivery_policy: EventDeliveryPolicy | None = None,
    sleeper=None,
) -> tuple[EventDispatcher, InMemoryEventLedger]:
    actual_ledger = ledger or InMemoryEventLedger()
    kwargs = {}
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return (
        EventDispatcher(
            sequence_store=actual_ledger,
            outbox=actual_ledger,
            schema_registry=core_schema_registry(),
            sinks=tuple(registrations),
            clock=FixedClock(),
            authorization_policy=policy,
            delivery_policy=delivery_policy,
            **kwargs,
        ),
        actual_ledger,
    )


async def make_event(
    event_dispatcher: EventDispatcher,
    delivery_class: DeliveryClass,
    *,
    event_type: CoreEventType = CoreEventType.ARTIFACT_CREATED,
    payload: dict[str, PayloadField] | None = None,
) -> RuntimeEvent:
    context = call_context()
    return await event_dispatcher.create_event(
        event_type=event_type.value,
        schema_version="1",
        run_id=context.run_id,
        root_run_id=context.root_run_id,
        parent_run_id=None,
        scope=context.scope,
        context=context,
        sensitivity=Sensitivity.INTERNAL,
        delivery_class=delivery_class,
        payload=payload or {},
    )


def test_event_model_schema_and_redaction() -> None:
    event = RuntimeEvent(
        event_id=EventId("event-1"),
        event_type=CoreEventType.ARTIFACT_CREATED.value,
        schema_version="1",
        occurred_at=NOW,
        run_id=RunId("run-1"),
        root_run_id=RunId("run-1"),
        parent_run_id=None,
        sequence=1,
        scope=call_context().scope,
        correlation_id=call_context().trace.correlation_id,
        causation_id=None,
        sensitivity=Sensitivity.RESTRICTED,
        delivery_class=DeliveryClass.OBSERVABILITY,
        payload=ClassifiedPayload(
            {
                "public": PayloadField("visible", Sensitivity.PUBLIC),
                "secret": PayloadField("hidden", Sensitivity.RESTRICTED),
            }
        ),
    )
    assert RuntimeEvent.from_data(event.to_data()) == event
    assert (
        event.payload_digest == RuntimeEvent.from_data(event.to_data()).payload_digest
    )
    redacted = ExplicitSensitivityRedactionPolicy().redact(event, Sensitivity.PUBLIC)
    assert redacted.payload.visible_data() == {"public": "visible"}
    assert redacted.sensitivity is Sensitivity.PUBLIC
    assert Sensitivity.from_wire("internal") is Sensitivity.INTERNAL

    registry = core_schema_registry()
    registry.validate(event)
    missing = replace(event, event_type=CoreEventType.RUN_STATE_CHANGED.value)
    with pytest.raises(CoreError) as invalid:
        registry.validate(missing)
    assert invalid.value.detail.code == "invalid_event_payload"
    with pytest.raises(CoreError):
        registry.validate(replace(event, schema_version="999"))
    with pytest.raises(CoreError):
        registry.register(event.event_type, "1", lambda value: None)


def test_event_model_invariants_and_capabilities() -> None:
    with pytest.raises(ValueError):
        ClassifiedPayload({" ": PayloadField("x")})
    with pytest.raises(ValueError):
        RuntimeEvent.from_data({**_minimal_event().to_data(), "sequence": "bad"})
    with pytest.raises(ValueError):
        replace(_minimal_event(), event_type="invalid")
    with pytest.raises(ValueError):
        replace(_minimal_event(), sequence=0)
    with pytest.raises(ValueError):
        EventAcknowledgement(
            AcknowledgementId("ack"), EventId("event"), "", "digest", NOW
        )
    cap = capabilities(DeliveryClass.AUDIT)
    assert cap.supports(replace(_minimal_event(), delivery_class=DeliveryClass.AUDIT))
    assert not capabilities(DeliveryClass.OBSERVABILITY).supports(
        replace(_minimal_event(), delivery_class=DeliveryClass.AUDIT)
    )
    with pytest.raises(ValueError):
        EventDeliveryPolicy(audit_max_attempts=0)
    with pytest.raises(ValueError):
        EventDeliveryPolicy(base_retry_delay=-1)
    with pytest.raises(ValueError):
        EventDeliveryPolicy(observability_queue_capacity=0)


@pytest.mark.asyncio
async def test_observability_is_non_blocking_and_diagnostic() -> None:
    good = InMemoryEventSink(
        "good", capabilities(DeliveryClass.OBSERVABILITY, acknowledgement=False)
    )
    failing = InMemoryEventSink(
        "failing",
        capabilities(DeliveryClass.OBSERVABILITY, acknowledgement=False),
        failures_before_success=1,
    )
    event_dispatcher, _ = dispatcher(
        SinkRegistration(good),
        SinkRegistration(failing),
        policy=MatchingAllowPolicy(),
    )
    event = await make_event(event_dispatcher, DeliveryClass.OBSERVABILITY)
    await event_dispatcher.publish(event, call_context(), principal())
    await event_dispatcher.flush()
    assert [item.event_id for item in good.events] == [event.event_id]
    assert event_dispatcher.diagnostics[0].code == "sink_unavailable"
    await event_dispatcher.close()


@pytest.mark.asyncio
async def test_observability_queue_can_drop_without_failing_run() -> None:
    sink = InMemoryEventSink(
        "observation", capabilities(DeliveryClass.OBSERVABILITY, acknowledgement=False)
    )
    event_dispatcher, _ = dispatcher(
        SinkRegistration(sink),
        policy=MatchingAllowPolicy(),
        delivery_policy=EventDeliveryPolicy(observability_queue_capacity=1),
    )
    event = await make_event(event_dispatcher, DeliveryClass.OBSERVABILITY)
    await event_dispatcher.publish(event, call_context(), principal())
    await event_dispatcher.publish(event, call_context(), principal())
    assert any(
        item.code == "observability_queue_full" for item in event_dispatcher.diagnostics
    )
    await event_dispatcher.close()


@pytest.mark.asyncio
async def test_audit_retry_acknowledgement_and_pending_recovery() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    sink = InMemoryEventSink(
        "audit",
        capabilities(DeliveryClass.AUDIT),
        failures_before_success=2,
    )
    event_dispatcher, ledger = dispatcher(
        SinkRegistration(sink, required_for_audit=True),
        policy=MatchingAllowPolicy(),
        sleeper=sleep,
    )
    event = await make_event(event_dispatcher, DeliveryClass.AUDIT)
    await event_dispatcher.publish(event, call_context(), principal())
    assert len(sink.events) == 1
    assert delays == [0.05, 0.1]
    assert await ledger.pending() == ()

    failing = InMemoryEventSink(
        "recoverable", capabilities(DeliveryClass.AUDIT), failures_before_success=3
    )
    recovery_dispatcher, recovery_ledger = dispatcher(
        SinkRegistration(failing, required_for_audit=True),
        policy=MatchingAllowPolicy(),
        sleeper=sleep,
    )
    pending_event = await make_event(recovery_dispatcher, DeliveryClass.AUDIT)
    with pytest.raises(CoreError) as failed:
        await recovery_dispatcher.publish(pending_event, call_context(), principal())
    assert failed.value.detail.code == "audit_delivery_failed"
    assert len(await recovery_ledger.pending()) == 1

    replacement = InMemoryEventSink("recoverable", capabilities(DeliveryClass.AUDIT))
    restarted, _ = dispatcher(
        SinkRegistration(replacement, required_for_audit=True),
        ledger=recovery_ledger,
        policy=MatchingAllowPolicy(),
        delivery_policy=EventDeliveryPolicy(audit_max_attempts=4),
        sleeper=sleep,
    )
    assert await restarted.recover_pending() == 1
    assert replacement.events[0].event_id == pending_event.event_id


@pytest.mark.asyncio
async def test_audit_fail_closed_and_compatibility_checks() -> None:
    event_dispatcher, _ = dispatcher(policy=MatchingAllowPolicy())
    event = await make_event(event_dispatcher, DeliveryClass.AUDIT)
    with pytest.raises(CoreError) as missing:
        await event_dispatcher.publish(event, call_context(), principal())
    assert missing.value.detail.code == "required_audit_sink_missing"

    cases = [
        InMemoryEventSink("wrong-class", capabilities(DeliveryClass.OBSERVABILITY)),
        InMemoryEventSink(
            "wrong-schema",
            capabilities(DeliveryClass.AUDIT, schemas=frozenset({("other", "1")})),
        ),
        InMemoryEventSink(
            "no-ack", capabilities(DeliveryClass.AUDIT, acknowledgement=False)
        ),
    ]
    for sink in cases:
        candidate, _ = dispatcher(
            SinkRegistration(sink, required_for_audit=True),
            policy=MatchingAllowPolicy(),
        )
        candidate_event = await make_event(candidate, DeliveryClass.AUDIT)
        with pytest.raises(CoreError):
            await candidate.publish(candidate_event, call_context(), principal())

    for auth_policy in (None, DenyingPolicy()):
        sink = InMemoryEventSink("audit", capabilities(DeliveryClass.AUDIT))
        candidate, _ = dispatcher(
            SinkRegistration(sink, required_for_audit=True), policy=auth_policy
        )
        candidate_event = await make_event(candidate, DeliveryClass.AUDIT)
        with pytest.raises(CoreError) as denied:
            await candidate.publish(candidate_event, call_context(), principal())
        assert denied.value.detail.category is ErrorCategory.DENIED

    cancelled_context = call_context()
    cancelled_context.cancellation.cancel()
    sink = InMemoryEventSink("cancelled", capabilities(DeliveryClass.AUDIT))
    candidate, ledger = dispatcher(
        SinkRegistration(sink, required_for_audit=True),
        policy=MatchingAllowPolicy(),
    )
    candidate_event = await make_event(candidate, DeliveryClass.AUDIT)
    with pytest.raises(CoreError) as cancelled:
        await candidate.publish(candidate_event, cancelled_context, principal())
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert await ledger.pending() == ()


@pytest.mark.asyncio
async def test_redaction_failure_and_optional_audit_sink_failure() -> None:
    required = InMemoryEventSink("required", capabilities(DeliveryClass.AUDIT))
    optional = InMemoryEventSink(
        "optional", capabilities(DeliveryClass.AUDIT), failures_before_success=3
    )
    event_dispatcher, _ = dispatcher(
        SinkRegistration(required, required_for_audit=True),
        SinkRegistration(optional),
        policy=MatchingAllowPolicy(),
    )
    event = await make_event(event_dispatcher, DeliveryClass.AUDIT)
    await event_dispatcher.publish(event, call_context(), principal())
    assert required.events
    assert event_dispatcher.diagnostics[-1].code == "audit_delivery_failed"

    public_sink = InMemoryEventSink(
        "public",
        capabilities(DeliveryClass.AUDIT, sensitivity=Sensitivity.PUBLIC),
    )
    redacting, _ = dispatcher(
        SinkRegistration(public_sink, required_for_audit=True),
        policy=MatchingAllowPolicy(),
    )
    state_event = await make_event(
        redacting,
        DeliveryClass.AUDIT,
        event_type=CoreEventType.RUN_STATE_CHANGED,
        payload={
            "previous_status": PayloadField("created"),
            "new_status": PayloadField("starting"),
            "attempt": PayloadField(1),
            "reason": PayloadField("start"),
            "state_version": PayloadField(1),
        },
    )
    with pytest.raises(CoreError) as failed:
        await redacting.publish(state_event, call_context(), principal())
    assert failed.value.detail.code == "event_redaction_failed"


@pytest.mark.asyncio
async def test_in_memory_ledger_identity_conflicts_and_acknowledgement() -> None:
    ledger = InMemoryEventLedger()
    sink = InMemoryEventSink("audit", capabilities(DeliveryClass.AUDIT))
    event_dispatcher, _ = dispatcher(
        SinkRegistration(sink, required_for_audit=True),
        ledger=ledger,
        policy=MatchingAllowPolicy(),
    )
    event = await make_event(event_dispatcher, DeliveryClass.AUDIT)
    context = call_context()
    await ledger.enqueue(event, sink.sink_id, context, principal())
    changed = replace(event, event_type=CoreEventType.APPROVAL_REQUESTED.value)
    with pytest.raises(CoreError) as conflict:
        await ledger.enqueue(changed, sink.sink_id, context, principal())
    assert conflict.value.detail.code == "event_identity_conflict"
    with pytest.raises(CoreError):
        await ledger.mark_attempt(EventId("missing"), sink.sink_id, None)
    with pytest.raises(CoreError):
        await ledger.acknowledge(
            EventAcknowledgement(
                AcknowledgementId("ack"),
                event.event_id,
                sink.sink_id,
                "wrong",
                NOW,
            )
        )


@pytest.mark.asyncio
async def test_recover_pending_reports_missing_sink() -> None:
    ledger = InMemoryEventLedger()
    source_sink = InMemoryEventSink("gone", capabilities(DeliveryClass.AUDIT))
    source, _ = dispatcher(
        SinkRegistration(source_sink, required_for_audit=True),
        ledger=ledger,
        policy=MatchingAllowPolicy(),
    )
    event = await make_event(source, DeliveryClass.AUDIT)
    await ledger.enqueue(event, source_sink.sink_id, call_context(), principal())
    restarted, _ = dispatcher(ledger=ledger, policy=MatchingAllowPolicy())
    assert await restarted.recover_pending() == 0
    assert restarted.diagnostics[0].code == "sink_not_registered"


def _minimal_event() -> RuntimeEvent:
    context = call_context()
    return RuntimeEvent(
        EventId("event"),
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
        DeliveryClass.OBSERVABILITY,
        ClassifiedPayload(),
    )
