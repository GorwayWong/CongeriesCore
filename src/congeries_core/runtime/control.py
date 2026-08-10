"""Clock, deadline, cancellation, and trace primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from typing import Protocol

from .errors import ErrorCategory, core_error
from .ids import CancellationId, CausationId, CorrelationId, SpanId, TraceId


def require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Deadline:
    at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", require_utc(self.at, "deadline"))

    def expired(self, clock: Clock) -> bool:
        return clock.now() >= self.at

    def raise_if_expired(self, clock: Clock) -> None:
        if self.expired(clock):
            raise core_error(
                ErrorCategory.TIMEOUT,
                "deadline_exceeded",
                "runtime call deadline has expired",
                retryable=True,
            )


class CancellationToken:
    """Thread-safe cancellation signal that can cross async adapter boundaries."""

    __slots__ = ("_event", "token_id")

    def __init__(
        self,
        token_id: CancellationId | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        self.token_id = token_id or CancellationId.new()
        self._event = Event()
        if cancelled:
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        was_cancelled = self.cancelled
        self._event.set()
        return not was_cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise core_error(
                ErrorCategory.CANCELLED,
                "call_cancelled",
                "runtime call was cancelled",
            )


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: TraceId
    span_id: SpanId
    correlation_id: CorrelationId
    causation_id: CausationId | None = None

    @classmethod
    def new(cls) -> TraceContext:
        return cls(
            trace_id=TraceId.new(),
            span_id=SpanId.new(),
            correlation_id=CorrelationId.new(),
        )

    def child(self, causation_id: CausationId | None = None) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            span_id=SpanId.new(),
            correlation_id=self.correlation_id,
            causation_id=causation_id or self.causation_id,
        )
