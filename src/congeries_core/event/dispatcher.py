"""Runtime Event creation, routing, reliable audit, and best-effort observation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizationPolicy,
    PolicyEffect,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, Clock
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import EventId, ResourceId, RunId
from congeries_core.runtime.scope import ScopeRef

from .model import (
    ClassifiedPayload,
    DeliveryClass,
    PayloadField,
    RuntimeEvent,
    Sensitivity,
)
from .ports import AuditOutbox, EventSequenceStore, EventSink, PendingAuditDelivery
from .redaction import ExplicitSensitivityRedactionPolicy, RedactionPolicy
from .schema import EventSchemaRegistry

type Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SinkRegistration:
    sink: EventSink
    required_for_audit: bool = False


@dataclass(frozen=True, slots=True)
class EventDeliveryPolicy:
    audit_max_attempts: int = 3
    observability_max_attempts: int = 1
    base_retry_delay: float = 0.05
    maximum_retry_delay: float = 1.0
    observability_queue_capacity: int = 1024

    def __post_init__(self) -> None:
        if self.audit_max_attempts < 1 or self.observability_max_attempts < 1:
            raise ValueError("delivery attempts must be positive")
        if self.base_retry_delay < 0 or self.maximum_retry_delay < 0:
            raise ValueError("retry delays must not be negative")
        if self.observability_queue_capacity < 1:
            raise ValueError("observability queue capacity must be positive")


@dataclass(frozen=True, slots=True)
class EventDiagnostic:
    event_id: EventId
    sink_id: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _QueuedObservation:
    event: RuntimeEvent
    context: RuntimeCallContext
    principal: RuntimePrincipal


class EventDispatcher:
    """Routes events without making the event stream runtime state authority."""

    _PUBLISH_ACTION = ActionRef("core", "event.publish", "1")

    def __init__(
        self,
        *,
        sequence_store: EventSequenceStore,
        outbox: AuditOutbox,
        schema_registry: EventSchemaRegistry,
        sinks: tuple[SinkRegistration, ...],
        clock: Clock,
        authorization_policy: AuthorizationPolicy | None,
        redaction_policy: RedactionPolicy | None = None,
        delivery_policy: EventDeliveryPolicy | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._sequences = sequence_store
        self._outbox = outbox
        self._schemas = schema_registry
        self._sinks = sinks
        self._clock = clock
        self._authorization = authorization_policy
        self._redaction = redaction_policy or ExplicitSensitivityRedactionPolicy()
        self._policy = delivery_policy or EventDeliveryPolicy()
        self._sleeper = sleeper
        self._observations: asyncio.Queue[_QueuedObservation] = asyncio.Queue(
            maxsize=self._policy.observability_queue_capacity
        )
        self._worker: asyncio.Task[None] | None = None
        # Deterministic publishers may ask for the same EventId again after a
        # local retry.  Reuse the already allocated timestamp/sequence when the
        # logical event is identical, and reject identity reuse with new data.
        # Durable at-least-once recovery remains the AuditOutbox's responsibility.
        self._created_events: dict[EventId, RuntimeEvent] = {}
        self.diagnostics: list[EventDiagnostic] = []

    async def create_event(
        self,
        *,
        event_type: str,
        schema_version: str,
        run_id: RunId,
        root_run_id: RunId,
        parent_run_id: RunId | None,
        scope: ScopeRef,
        context: RuntimeCallContext,
        sensitivity: Sensitivity,
        delivery_class: DeliveryClass,
        payload: Mapping[str, PayloadField],
        event_id: EventId | None = None,
    ) -> RuntimeEvent:
        if event_id is not None and event_id in self._created_events:
            existing = self._created_events[event_id]
            if (
                existing.event_type != event_type
                or existing.schema_version != schema_version
                or existing.run_id != run_id
                or existing.root_run_id != root_run_id
                or existing.parent_run_id != parent_run_id
                or existing.scope != scope
                or existing.correlation_id != context.trace.correlation_id
                or existing.causation_id != context.trace.causation_id
                or existing.sensitivity is not sensitivity
                or existing.delivery_class is not delivery_class
                or existing.payload != ClassifiedPayload(payload)
            ):
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "event_identity_conflict",
                    "event identity was reused with different Event data",
                )
            return existing
        event = RuntimeEvent(
            event_id=event_id or EventId.new(),
            event_type=event_type,
            schema_version=schema_version,
            occurred_at=self._clock.now(),
            run_id=run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            sequence=await self._sequences.next_sequence(run_id),
            scope=scope,
            correlation_id=context.trace.correlation_id,
            causation_id=context.trace.causation_id,
            sensitivity=sensitivity,
            delivery_class=delivery_class,
            payload=ClassifiedPayload(payload),
        )
        self._schemas.validate(event)
        if event_id is not None:
            self._created_events[event.event_id] = event
        return event

    async def publish(
        self,
        event: RuntimeEvent,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        self._schemas.validate(event)
        if event.delivery_class is DeliveryClass.AUDIT:
            await self._publish_audit(event, context, principal)
            return
        self._ensure_worker()
        try:
            self._observations.put_nowait(_QueuedObservation(event, context, principal))
        except asyncio.QueueFull:
            self.diagnostics.append(
                EventDiagnostic(
                    event.event_id,
                    None,
                    "observability_queue_full",
                    "observability event was dropped after the queue reached capacity",
                )
            )

    async def flush(self) -> None:
        await self._observations.join()

    async def close(self) -> None:
        await self.flush()
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def recover_pending(self, limit: int = 100) -> int:
        recovered = 0
        registrations = {item.sink.sink_id: item for item in self._sinks}
        for pending in await self._outbox.pending(limit):
            registration = registrations.get(pending.sink_id)
            if registration is None:
                self._diagnose_pending(pending, "sink_not_registered")
                continue
            context = replace(
                pending.context,
                deadline=None,
                cancellation=CancellationToken(),
            )
            try:
                await self._authorize_sink(
                    registration.sink, pending.event, context, pending.principal
                )
                await self._deliver_audit(
                    pending.event,
                    context,
                    registration.sink,
                    starting_attempt=pending.attempts,
                )
                recovered += 1
            except CoreError as error:
                self.diagnostics.append(
                    EventDiagnostic(
                        pending.event.event_id,
                        pending.sink_id,
                        error.detail.code,
                        error.detail.message,
                    )
                )
        return recovered

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._observation_worker())

    async def _observation_worker(self) -> None:
        while True:
            queued = await self._observations.get()
            try:
                await self._publish_observation(queued)
            finally:
                self._observations.task_done()

    async def _publish_observation(self, queued: _QueuedObservation) -> None:
        for registration in self._sinks:
            sink = registration.sink
            if DeliveryClass.OBSERVABILITY not in sink.capabilities.delivery_classes:
                continue
            try:
                event = await self._prepare_for_sink(
                    queued.event, queued.context, queued.principal, sink
                )
                await self._deliver_observation(event, queued.context, sink)
            except CoreError as error:
                self.diagnostics.append(
                    EventDiagnostic(
                        queued.event.event_id,
                        sink.sink_id,
                        error.detail.code,
                        error.detail.message,
                    )
                )

    async def _publish_audit(
        self,
        event: RuntimeEvent,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        required = [item for item in self._sinks if item.required_for_audit]
        if not required:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "required_audit_sink_missing",
                "no required Audit EventSink is configured",
                retryable=True,
            )
        for registration in required:
            sink = registration.sink
            prepared = await self._prepare_for_sink(event, context, principal, sink)
            await self._outbox.enqueue(prepared, sink.sink_id, context, principal)
            await self._deliver_audit(prepared, context, sink)

        for registration in self._sinks:
            if registration.required_for_audit:
                continue
            sink = registration.sink
            if DeliveryClass.AUDIT not in sink.capabilities.delivery_classes:
                continue
            try:
                prepared = await self._prepare_for_sink(event, context, principal, sink)
                await self._outbox.enqueue(prepared, sink.sink_id, context, principal)
                await self._deliver_audit(prepared, context, sink)
            except CoreError as error:
                self.diagnostics.append(
                    EventDiagnostic(
                        event.event_id,
                        sink.sink_id,
                        error.detail.code,
                        error.detail.message,
                    )
                )

    async def _prepare_for_sink(
        self,
        event: RuntimeEvent,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
        sink: EventSink,
    ) -> RuntimeEvent:
        if event.delivery_class not in sink.capabilities.delivery_classes:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "unsupported_delivery_class",
                "EventSink does not support the required delivery class",
            )
        if (
            event.event_type,
            event.schema_version,
        ) not in sink.capabilities.schema_versions and (
            "*",
            "*",
        ) not in sink.capabilities.schema_versions:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "unsupported_event_schema",
                "EventSink does not support the event schema",
            )
        if (
            event.delivery_class is DeliveryClass.AUDIT
            and not sink.capabilities.acknowledgement
        ):
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "audit_acknowledgement_unsupported",
                "Audit EventSink must provide acknowledgements",
            )
        await self._authorize_sink(sink, event, context, principal)
        redacted = self._redaction.redact(event, sink.capabilities.maximum_sensitivity)
        try:
            self._schemas.validate(redacted)
        except CoreError as error:
            raise core_error(
                ErrorCategory.DENIED,
                "event_redaction_failed",
                "redaction removed data required by the Event schema",
            ) from error
        return redacted

    async def _authorize_sink(
        self,
        sink: EventSink,
        event: RuntimeEvent,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        context.check_active(self._clock)
        if self._authorization is None:
            raise core_error(
                ErrorCategory.DENIED,
                "event_sink_policy_missing",
                "EventSink dispatch is denied without an authorization policy",
            )
        request = AccessRequest(
            principal=principal,
            action=self._PUBLISH_ACTION,
            resource=ResourceRef(
                namespace="core",
                kind="event_sink",
                id=ResourceId(sink.sink_id),
            ),
            scope=event.scope,
            context=context,
            constraints={
                "event_type": event.event_type,
                "delivery_class": event.delivery_class.value,
            },
        )
        decision = await self._authorization.authorize(request)
        if decision.effect is not PolicyEffect.ALLOW or decision.grant is None:
            raise core_error(
                ErrorCategory.DENIED,
                decision.reason_code or "event_sink_denied",
                "EventSink dispatch was denied",
            )
        grant = decision.grant
        if grant.expires_at and self._clock.now() >= grant.expires_at:
            raise core_error(
                ErrorCategory.DENIED,
                "event_sink_grant_expired",
                "EventSink grant has expired",
            )
        if (
            grant.principal != request.principal
            or grant.action != request.action
            or grant.resource != request.resource
            or grant.source_scope.key != context.scope.key
            or grant.effective_scope.key != event.scope.key
        ):
            raise core_error(
                ErrorCategory.DENIED,
                "invalid_event_sink_grant",
                "EventSink grant does not match the dispatch request",
            )

    async def _deliver_observation(
        self, event: RuntimeEvent, context: RuntimeCallContext, sink: EventSink
    ) -> None:
        last_error: CoreError | None = None
        for attempt in range(self._policy.observability_max_attempts):
            try:
                await sink.deliver(event, context)
                return
            except CoreError as error:
                last_error = error
                if not error.detail.retryable:
                    break
                await self._backoff(attempt)
        if last_error:
            raise last_error

    async def _deliver_audit(
        self,
        event: RuntimeEvent,
        context: RuntimeCallContext,
        sink: EventSink,
        *,
        starting_attempt: int = 0,
    ) -> None:
        last_error: CoreError | None = None
        for attempt in range(starting_attempt, self._policy.audit_max_attempts):
            try:
                acknowledgement = await sink.deliver(event, context)
                if acknowledgement is None:
                    raise core_error(
                        ErrorCategory.PROTOCOL_FAILURE,
                        "audit_acknowledgement_missing",
                        "Audit EventSink returned no acknowledgement",
                    )
                await self._outbox.mark_attempt(event.event_id, sink.sink_id, None)
                await self._outbox.acknowledge(acknowledgement)
                return
            except CoreError as error:
                last_error = error
                await self._outbox.mark_attempt(
                    event.event_id, sink.sink_id, error.detail.code
                )
                if not error.detail.retryable:
                    break
                await self._backoff(attempt)
        raise core_error(
            ErrorCategory.UNAVAILABLE,
            "audit_delivery_failed",
            "required Audit EventSink did not acknowledge the Event",
            retryable=True,
            cause_id=last_error.detail.code if last_error else None,
        )

    async def _backoff(self, attempt: int) -> None:
        delay = min(
            self._policy.base_retry_delay * (2**attempt),
            self._policy.maximum_retry_delay,
        )
        if delay:
            await self._sleeper(delay)

    def _diagnose_pending(self, pending: PendingAuditDelivery, code: str) -> None:
        self.diagnostics.append(
            EventDiagnostic(
                pending.event.event_id,
                pending.sink_id,
                code,
                "pending Audit Event cannot be delivered",
            )
        )
