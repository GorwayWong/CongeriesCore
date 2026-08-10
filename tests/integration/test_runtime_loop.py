from __future__ import annotations

import pytest

from congeries_core.event.dispatcher import EventDispatcher, SinkRegistration
from congeries_core.event.integration import RuntimeEventPublisher
from congeries_core.event.memory import InMemoryEventLedger, InMemoryEventSink
from congeries_core.event.model import DeliveryClass, EventSinkCapabilities, Sensitivity
from congeries_core.event.schema import core_schema_registry
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    DenyAllPolicy,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.policy.integration import RunAuditFailureHandler
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import PrincipalId, ResourceId
from congeries_core.runtime.run import RunStatus
from congeries_core.state.repository import InMemoryRunRepository
from congeries_core.state.service import RunService

from ..support import FixedClock, MatchingAllowPolicy, agent_run, call_context


def event_sink() -> InMemoryEventSink:
    return InMemoryEventSink(
        "runtime",
        EventSinkCapabilities(
            delivery_classes=frozenset(
                {DeliveryClass.OBSERVABILITY, DeliveryClass.AUDIT}
            ),
            schema_versions=frozenset({("*", "*")}),
            maximum_sensitivity=Sensitivity.RESTRICTED,
            acknowledgement=True,
        ),
    )


def event_dispatcher(
    clock: FixedClock, sinks: tuple[SinkRegistration, ...]
) -> EventDispatcher:
    ledger = InMemoryEventLedger()
    return EventDispatcher(
        sequence_store=ledger,
        outbox=ledger,
        schema_registry=core_schema_registry(),
        sinks=sinks,
        clock=clock,
        authorization_policy=MatchingAllowPolicy(),
    )


@pytest.mark.asyncio
async def test_root_agent_run_authorization_state_and_events_close_loop() -> None:
    clock = FixedClock()
    sink = event_sink()
    events = event_dispatcher(clock, (SinkRegistration(sink, True),))
    publisher = RuntimeEventPublisher(events)
    runs = RunService(InMemoryRunRepository(), clock, publisher)
    run = agent_run()
    await runs.create(run)
    current = await runs.start(run.run_id, 0)
    current = await runs.advance(
        run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
    )
    current = await runs.advance(run.run_id, current.state_version, RunStatus.RUNNING)

    action = ActionRef("core", "model.generate", "1")
    request = AccessRequest(
        principal=RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(run.run_id.value)
        ),
        action=action,
        resource=ResourceRef("core", "model", ResourceId("model-1")),
        scope=run.scope,
        context=call_context(run_id=run.run_id, scope=run.scope),
    )
    authorized = AuthorizedDispatcher[str](
        action_registry=ActionRegistry((action,)),
        audit_publisher=publisher,
        audit_failure_handler=RunAuditFailureHandler(runs),
        clock=clock,
        policy=MatchingAllowPolicy(),
    )

    async def capability(call) -> str:
        return f"called:{call.context.run_id.value}"

    assert (
        await authorized.dispatch(request, capability) == f"called:{run.run_id.value}"
    )
    completed = await runs.complete(run.run_id, current.state_version)
    assert completed.status is RunStatus.SUCCEEDED
    await events.flush()
    assert [event.sequence for event in sink.events] == [1, 2, 3, 4]
    await events.close()


@pytest.mark.asyncio
async def test_audit_delivery_failure_pauses_the_protected_run() -> None:
    clock = FixedClock()
    events = event_dispatcher(clock, ())
    publisher = RuntimeEventPublisher(events)
    runs = RunService(InMemoryRunRepository(), clock, publisher)
    run = agent_run()
    await runs.create(run)
    current = await runs.start(run.run_id, 0)
    current = await runs.advance(
        run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
    )
    current = await runs.advance(run.run_id, current.state_version, RunStatus.RUNNING)
    action = ActionRef("core", "tool.invoke", "1")
    request = AccessRequest(
        principal=RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(run.run_id.value)
        ),
        action=action,
        resource=ResourceRef("core", "tool", ResourceId("tool-1")),
        scope=run.scope,
        context=call_context(run_id=run.run_id, scope=run.scope),
    )
    protected = AuthorizedDispatcher[str](
        action_registry=ActionRegistry((action,)),
        audit_publisher=publisher,
        audit_failure_handler=RunAuditFailureHandler(runs),
        clock=clock,
        policy=DenyAllPolicy(),
    )
    with pytest.raises(CoreError) as audit_failure:
        await protected.dispatch(request, lambda call: _unused(call))
    assert audit_failure.value.detail.code == "required_audit_sink_missing"
    assert (await runs.get(run.run_id)).status is RunStatus.PAUSED
    await events.close()


async def _unused(call) -> str:
    del call
    return "unused"
