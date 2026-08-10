"""SQLite Event sequence and AuditOutbox reference adapter."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from congeries_core.event.model import EventAcknowledgement, RuntimeEvent
from congeries_core.event.ports import PendingAuditDelivery
from congeries_core.policy.authorization import RuntimePrincipal
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import EventId, RunId


class SqliteEventLedger:
    """Durable per-Run sequencing and at-least-once Audit delivery state."""

    def __init__(self, path: str | Path, *, busy_timeout_seconds: float = 5.0) -> None:
        self._path = str(Path(path))
        self._timeout = busy_timeout_seconds
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def next_sequence(self, run_id: RunId) -> int:
        await self._ensure_initialized()
        return await asyncio.to_thread(self._next_sequence_sync, run_id)

    async def enqueue(
        self,
        event: RuntimeEvent,
        sink_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        await self._ensure_initialized()
        await asyncio.to_thread(self._enqueue_sync, event, sink_id, context, principal)

    async def mark_attempt(
        self, event_id: EventId, sink_id: str, error: str | None
    ) -> None:
        await self._ensure_initialized()
        await asyncio.to_thread(self._mark_attempt_sync, event_id, sink_id, error)

    async def acknowledge(self, acknowledgement: EventAcknowledgement) -> None:
        await self._ensure_initialized()
        await asyncio.to_thread(self._acknowledge_sync, acknowledgement)

    async def pending(self, limit: int = 100) -> tuple[PendingAuditDelivery, ...]:
        if limit < 1:
            raise ValueError("pending limit must be positive")
        await self._ensure_initialized()
        return await asyncio.to_thread(self._pending_sync, limit)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if not self._initialized:
                await asyncio.to_thread(self._initialize_sync)
                self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_sync(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_sequences (
                    run_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 1)
                );

                CREATE TABLE IF NOT EXISTS audit_outbox (
                    event_id TEXT NOT NULL,
                    sink_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    principal_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    last_error TEXT,
                    acknowledgement_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, sink_id)
                );

                CREATE INDEX IF NOT EXISTS audit_outbox_pending
                ON audit_outbox(status, created_at);
                """
            )

    def _next_sequence_sync(self, run_id: RunId) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_sequence FROM event_sequences WHERE run_id = ?",
                (run_id.value,),
            ).fetchone()
            sequence = 1 if row is None else int(row["last_sequence"]) + 1
            connection.execute(
                """
                INSERT INTO event_sequences(run_id, last_sequence)
                VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET last_sequence = excluded.last_sequence
                """,
                (run_id.value, sequence),
            )
            connection.commit()
            return sequence

    def _enqueue_sync(
        self,
        event: RuntimeEvent,
        sink_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        digest = event.payload_digest
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload_digest FROM audit_outbox
                WHERE event_id = ? AND sink_id = ?
                """,
                (event.event_id.value, sink_id),
            ).fetchone()
            if row is not None:
                if str(row["payload_digest"]) != digest:
                    connection.rollback()
                    raise core_error(
                        ErrorCategory.CONFLICT,
                        "event_identity_conflict",
                        "Event identity was reused with a different payload",
                    )
                connection.commit()
                return
            connection.execute(
                """
                INSERT INTO audit_outbox(
                    event_id, sink_id, event_json, context_json, principal_json,
                    payload_digest, status, attempts, last_error,
                    acknowledgement_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?)
                """,
                (
                    event.event_id.value,
                    sink_id,
                    _json_dump(event.to_data()),
                    _json_dump(context.to_data()),
                    _json_dump(principal.to_data()),
                    digest,
                    now,
                    now,
                ),
            )
            connection.commit()

    def _mark_attempt_sync(
        self, event_id: EventId, sink_id: str, error: str | None
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE audit_outbox
                SET attempts = attempts + 1, last_error = ?, updated_at = ?
                WHERE event_id = ? AND sink_id = ? AND status = 'pending'
                """,
                (
                    error,
                    datetime.now(UTC).isoformat(),
                    event_id.value,
                    sink_id,
                ),
            )
            if cursor.rowcount != 1:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "outbox_entry_not_found",
                    "pending Audit delivery does not exist",
                )

    def _acknowledge_sync(self, acknowledgement: EventAcknowledgement) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload_digest, status FROM audit_outbox
                WHERE event_id = ? AND sink_id = ?
                """,
                (acknowledgement.event_id.value, acknowledgement.sink_id),
            ).fetchone()
            if (
                row is None
                or str(row["payload_digest"]) != acknowledgement.payload_digest
            ):
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "acknowledgement_conflict",
                    "acknowledgement does not match the AuditOutbox payload",
                )
            if str(row["status"]) == "acknowledged":
                connection.commit()
                return
            connection.execute(
                """
                UPDATE audit_outbox
                SET status = 'acknowledged', acknowledgement_id = ?,
                    last_error = NULL, updated_at = ?
                WHERE event_id = ? AND sink_id = ?
                """,
                (
                    acknowledgement.acknowledgement_id.value,
                    acknowledgement.acknowledged_at.isoformat(),
                    acknowledgement.event_id.value,
                    acknowledgement.sink_id,
                ),
            )
            connection.commit()

    def _pending_sync(self, limit: int) -> tuple[PendingAuditDelivery, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json, sink_id, context_json, principal_json,
                       payload_digest, attempts, last_error
                FROM audit_outbox
                WHERE status = 'pending'
                ORDER BY created_at, event_id, sink_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        pending: list[PendingAuditDelivery] = []
        for row in rows:
            event_data = _json_object(str(row["event_json"]))
            context_data = _json_object(str(row["context_json"]))
            principal_data = _json_object(str(row["principal_json"]))
            pending.append(
                PendingAuditDelivery(
                    event=RuntimeEvent.from_data(event_data),
                    sink_id=str(row["sink_id"]),
                    context=RuntimeCallContext.from_data(context_data),
                    principal=RuntimePrincipal.from_data(principal_data),
                    payload_digest=str(row["payload_digest"]),
                    attempts=int(row["attempts"]),
                    last_error=(str(row["last_error"]) if row["last_error"] else None),
                )
            )
        return tuple(pending)


def _json_dump(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, object]:
    decoded: Any = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON value must be an object")
    return cast(dict[str, object], decoded)
