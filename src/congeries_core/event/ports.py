"""Async EventSink, sequence, and AuditOutbox ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from congeries_core.policy.authorization import RuntimePrincipal
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.ids import EventId, RunId

from .model import EventAcknowledgement, EventSinkCapabilities, RuntimeEvent


class EventSink(Protocol):
    @property
    def sink_id(self) -> str: ...

    @property
    def capabilities(self) -> EventSinkCapabilities: ...

    async def deliver(
        self, event: RuntimeEvent, context: RuntimeCallContext
    ) -> EventAcknowledgement | None: ...


class EventSequenceStore(Protocol):
    async def next_sequence(self, run_id: RunId) -> int: ...


@dataclass(frozen=True, slots=True)
class PendingAuditDelivery:
    event: RuntimeEvent
    sink_id: str
    context: RuntimeCallContext
    principal: RuntimePrincipal
    payload_digest: str
    attempts: int
    last_error: str | None


class AuditOutbox(Protocol):
    async def enqueue(
        self,
        event: RuntimeEvent,
        sink_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None: ...

    async def mark_attempt(
        self, event_id: EventId, sink_id: str, error: str | None
    ) -> None: ...

    async def acknowledge(self, acknowledgement: EventAcknowledgement) -> None: ...

    async def pending(self, limit: int = 100) -> tuple[PendingAuditDelivery, ...]: ...
