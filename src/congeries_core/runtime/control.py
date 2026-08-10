"""Clock, deadline, cancellation, and trace primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock
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

    __slots__ = ("_callbacks", "_event", "_lock", "token_id")

    def __init__(
        self,
        token_id: CancellationId | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        self.token_id = token_id or CancellationId.new()
        self._event = Event()
        self._lock = Lock()
        self._callbacks: set[Callable[[], None]] = set()
        if cancelled:
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()
        return True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise core_error(
                ErrorCategory.CANCELLED,
                "call_cancelled",
                "runtime call was cancelled",
            )

    async def wait_cancelled(self) -> None:
        """Wait without polling and wake safely when another thread cancels."""

        if self.cancelled:
            return
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def wake() -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolve_future, future)

        with self._lock:
            if self._event.is_set():
                return
            self._callbacks.add(wake)
        try:
            await future
        finally:
            with self._lock:
                self._callbacks.discard(wake)


def _resolve_future(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


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
