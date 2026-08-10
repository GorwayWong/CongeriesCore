"""Lightweight SessionRef lifecycle state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from congeries_core.runtime.context import SessionRef
from congeries_core.runtime.control import require_utc
from congeries_core.runtime.errors import ErrorCategory, core_error


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SessionState:
    ref: SessionRef
    status: SessionStatus
    created_at: datetime
    state_version: int = 0
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, "created_at")
        )
        if self.closed_at:
            object.__setattr__(
                self, "closed_at", require_utc(self.closed_at, "closed_at")
            )
        if self.state_version < 0:
            raise ValueError("session state version must not be negative")
        if (self.status is SessionStatus.CLOSED) != (self.closed_at is not None):
            raise ValueError("closed Session requires closed_at")


class SessionRepository(Protocol):
    async def add(self, session: SessionState) -> None: ...

    async def get(self, ref: SessionRef) -> SessionState: ...

    async def close(
        self, ref: SessionRef, expected_version: int, now: datetime
    ) -> SessionState: ...

    async def require_open(self, ref: SessionRef) -> SessionState: ...


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[SessionRef, SessionState] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: SessionState) -> None:
        async with self._lock:
            if session.ref in self._sessions:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "session_already_exists",
                    "Session identity already exists",
                )
            self._sessions[session.ref] = session

    async def get(self, ref: SessionRef) -> SessionState:
        async with self._lock:
            session = self._sessions.get(ref)
            if session is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "session_not_found",
                    "Session does not exist",
                )
            return session

    async def close(
        self, ref: SessionRef, expected_version: int, now: datetime
    ) -> SessionState:
        async with self._lock:
            session = self._sessions.get(ref)
            if session is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "session_not_found",
                    "Session does not exist",
                )
            if session.status is SessionStatus.CLOSED:
                return session
            if session.state_version != expected_version:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_state_version",
                    "Session state version does not match",
                    retryable=True,
                )
            closed = replace(
                session,
                status=SessionStatus.CLOSED,
                closed_at=now,
                state_version=session.state_version + 1,
            )
            self._sessions[ref] = closed
            return closed

    async def require_open(self, ref: SessionRef) -> SessionState:
        session = await self.get(ref)
        if session.status is SessionStatus.CLOSED:
            raise core_error(
                ErrorCategory.CONFLICT,
                "session_closed",
                "closed Session cannot accept a new Run association",
            )
        return session
