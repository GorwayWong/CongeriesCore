"""In-memory Event ports for tests and ephemeral deployments."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from congeries_core.policy.authorization import RuntimePrincipal
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import AcknowledgementId, EventId, RunId

from .model import EventAcknowledgement, EventSinkCapabilities, RuntimeEvent
from .ports import PendingAuditDelivery


class InMemoryEventLedger:
    def __init__(self) -> None:
        self._sequences: dict[RunId, int] = {}
        self._pending: dict[tuple[EventId, str], PendingAuditDelivery] = {}
        self._acknowledged: dict[tuple[EventId, str], EventAcknowledgement] = {}
        self._lock = asyncio.Lock()

    async def next_sequence(self, run_id: RunId) -> int:
        async with self._lock:
            sequence = self._sequences.get(run_id, 0) + 1
            self._sequences[run_id] = sequence
            return sequence

    async def enqueue(
        self,
        event: RuntimeEvent,
        sink_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        async with self._lock:
            key = event.event_id, sink_id
            digest = event.payload_digest
            existing = self._pending.get(key)
            acknowledgement = self._acknowledged.get(key)
            existing_digest = (
                existing.payload_digest
                if existing
                else acknowledgement.payload_digest
                if acknowledgement
                else None
            )
            if existing_digest is not None and existing_digest != digest:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "event_identity_conflict",
                    "event identity was reused with a different payload",
                )
            if existing is None and acknowledgement is None:
                self._pending[key] = PendingAuditDelivery(
                    event,
                    sink_id,
                    context,
                    principal,
                    digest,
                    0,
                    None,
                )

    async def mark_attempt(
        self, event_id: EventId, sink_id: str, error: str | None
    ) -> None:
        async with self._lock:
            key = event_id, sink_id
            pending = self._pending.get(key)
            if pending is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "outbox_entry_not_found",
                    "pending audit delivery does not exist",
                )
            self._pending[key] = PendingAuditDelivery(
                pending.event,
                pending.sink_id,
                pending.context,
                pending.principal,
                pending.payload_digest,
                pending.attempts + 1,
                error,
            )

    async def acknowledge(self, acknowledgement: EventAcknowledgement) -> None:
        async with self._lock:
            key = acknowledgement.event_id, acknowledgement.sink_id
            existing_ack = self._acknowledged.get(key)
            if existing_ack:
                if existing_ack.payload_digest != acknowledgement.payload_digest:
                    raise core_error(
                        ErrorCategory.CONFLICT,
                        "acknowledgement_conflict",
                        "acknowledgement payload digest conflicts with existing value",
                    )
                return
            pending = self._pending.get(key)
            if (
                pending is None
                or pending.payload_digest != acknowledgement.payload_digest
            ):
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "acknowledgement_conflict",
                    "acknowledgement does not match pending Event payload",
                )
            self._acknowledged[key] = acknowledgement
            del self._pending[key]

    async def pending(self, limit: int = 100) -> tuple[PendingAuditDelivery, ...]:
        async with self._lock:
            return tuple(list(self._pending.values())[:limit])


class InMemoryEventSink:
    def __init__(
        self,
        sink_id: str,
        capabilities: EventSinkCapabilities,
        *,
        failures_before_success: int = 0,
    ) -> None:
        self._sink_id = sink_id
        self._capabilities = capabilities
        self._failures_remaining = failures_before_success
        self._acknowledgements: dict[EventId, EventAcknowledgement] = {}
        self.events: list[RuntimeEvent] = []

    @property
    def sink_id(self) -> str:
        return self._sink_id

    @property
    def capabilities(self) -> EventSinkCapabilities:
        return self._capabilities

    async def deliver(
        self, event: RuntimeEvent, context: RuntimeCallContext
    ) -> EventAcknowledgement | None:
        del context
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "sink_unavailable",
                "EventSink is temporarily unavailable",
                retryable=True,
            )
        existing = self._acknowledgements.get(event.event_id)
        if existing:
            if existing.payload_digest != event.payload_digest:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "event_identity_conflict",
                    "Event identity was reused with a different payload",
                )
            return existing
        self.events.append(event)
        if not self._capabilities.acknowledgement:
            return None
        acknowledgement = EventAcknowledgement(
            acknowledgement_id=AcknowledgementId.new(),
            event_id=event.event_id,
            sink_id=self.sink_id,
            payload_digest=event.payload_digest,
            acknowledged_at=datetime.now(UTC),
        )
        self._acknowledgements[event.event_id] = acknowledgement
        return acknowledgement
