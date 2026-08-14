"""Standard-library SQLite adapter for the Tool Operation Log."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, SystemClock
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import ResourceId
from congeries_core.tool.operation import (
    ToolOperationRecord,
    ToolOperationStatus,
    TransitionToolOperation,
)


class SqliteToolOperationStore:
    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        self._path = str(path)
        self._clock = clock or SystemClock()
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def prepare(
        self, record: ToolOperationRecord, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        if (
            record.status is not ToolOperationStatus.PREPARED
            or record.version != 0
            or record.outcome_ref is not None
            or record.evidence_ref is not None
            or record.created_at != record.updated_at
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "tool_operation_prepare_invalid",
                "Tool operation prepare requires a clean version-zero intent",
            )
        _validate_context(record, context)
        try:
            await self._ensure_initialized()
            return await asyncio.to_thread(self._prepare_sync, record)
        except sqlite3.Error as error:
            raise _store_failure() from error

    async def read(
        self, operation_id: ResourceId, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        try:
            await self._ensure_initialized()
            record = await asyncio.to_thread(self._read_sync, operation_id)
        except sqlite3.Error as error:
            raise _store_failure() from error
        _validate_context(record, context)
        return record

    async def transition(
        self, request: TransitionToolOperation, context: RuntimeCallContext
    ) -> ToolOperationRecord:
        try:
            await self._ensure_initialized()
            current = await asyncio.to_thread(self._read_sync, request.operation_id)
        except sqlite3.Error as error:
            raise _store_failure() from error
        _validate_context(current, context)
        if current.run_id != request.run_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "tool_operation_run_mismatch",
                "Tool operation Run does not match",
            )
        if current.request_fingerprint != request.request_fingerprint:
            raise core_error(
                ErrorCategory.CONFLICT,
                "tool_operation_request_fingerprint_conflict",
                "Tool operation request fingerprint changed",
            )
        if (
            current.status is request.target
            and current.outcome_ref == request.outcome_ref
            and current.evidence_ref == request.evidence_ref
        ):
            return current
        if current.version != request.expected_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "tool_operation_version_conflict",
                "Tool operation version is stale",
            )
        if not _allowed(current.status, request.target):
            raise core_error(
                ErrorCategory.CONFLICT,
                "tool_operation_transition_invalid",
                "Tool operation transition is invalid",
            )
        updated = replace(
            current,
            status=request.target,
            version=current.version + 1,
            updated_at=self._clock.now(),
            outcome_ref=request.outcome_ref,
            evidence_ref=request.evidence_ref,
        )
        try:
            return await asyncio.to_thread(
                self._transition_sync, current.version, updated
            )
        except sqlite3.Error as error:
            raise _store_failure() from error

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await asyncio.to_thread(self._initialize_sync)
                self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_operations (
                    operation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(run_id, idempotency_key)
                )
                """
            )

    def _prepare_sync(self, record: ToolOperationRecord) -> ToolOperationRecord:
        payload = _payload(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM tool_operations WHERE operation_id = ? "
                "OR (run_id = ? AND idempotency_key = ?)",
                (
                    record.operation_id.value,
                    record.run_id.value,
                    record.idempotency_key.value,
                ),
            ).fetchone()
            if row is not None:
                existing = ToolOperationRecord.from_data(json.loads(str(row[0])))
                if _prepare_identity(existing) != _prepare_identity(record):
                    raise core_error(
                        ErrorCategory.CONFLICT,
                        "tool_operation_identity_conflict",
                        "Tool operation identity or payload changed",
                    )
                connection.commit()
                return existing
            connection.execute(
                "INSERT INTO tool_operations"
                "(operation_id, run_id, idempotency_key, version, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.operation_id.value,
                    record.run_id.value,
                    record.idempotency_key.value,
                    record.version,
                    payload,
                ),
            )
            connection.commit()
            return record
        finally:
            connection.close()

    def _read_sync(self, operation_id: ResourceId) -> ToolOperationRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tool_operations WHERE operation_id = ?",
                (operation_id.value,),
            ).fetchone()
        if row is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "tool_operation_not_found",
                "Tool operation was not found",
            )
        return ToolOperationRecord.from_data(json.loads(str(row[0])))

    def _transition_sync(
        self, expected_version: int, updated: ToolOperationRecord
    ) -> ToolOperationRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tool_operations SET version = ?, payload = ? "
                "WHERE operation_id = ? AND version = ?",
                (
                    updated.version,
                    _payload(updated),
                    updated.operation_id.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT payload FROM tool_operations WHERE operation_id = ?",
                    (updated.operation_id.value,),
                ).fetchone()
                if row is not None:
                    existing = ToolOperationRecord.from_data(json.loads(str(row[0])))
                    if (
                        existing.status is updated.status
                        and existing.outcome_ref == updated.outcome_ref
                        and existing.evidence_ref == updated.evidence_ref
                    ):
                        connection.commit()
                        return existing
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "tool_operation_version_conflict",
                    "Tool operation version is stale",
                )
            connection.commit()
            return updated
        finally:
            connection.close()


def _payload(record: ToolOperationRecord) -> str:
    return json.dumps(record.to_data(), sort_keys=True, separators=(",", ":"))


def _prepare_identity(record: ToolOperationRecord) -> tuple[object, ...]:
    return (
        record.operation_id,
        record.run_id,
        record.workspace_id,
        record.node_id,
        record.scope,
        record.tool,
        record.idempotency_key,
        record.request_fingerprint,
        record.request_ref,
        record.side_effect,
    )


def _allowed(source: ToolOperationStatus, target: ToolOperationStatus) -> bool:
    return target in {
        ToolOperationStatus.PREPARED: {
            ToolOperationStatus.DISPATCHING,
            ToolOperationStatus.FAILED,
        },
        ToolOperationStatus.DISPATCHING: {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.UNKNOWN,
            ToolOperationStatus.FAILED,
        },
        ToolOperationStatus.UNKNOWN: {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.FAILED,
        },
    }.get(source, set())


def _validate_context(record: ToolOperationRecord, context: RuntimeCallContext) -> None:
    if record.run_id != context.run_id or record.workspace_id != context.workspace_id:
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "tool_operation_context_mismatch",
            "Tool operation context does not match",
        )
    record.scope.require_narrower_than(context.scope)


def _store_failure() -> CoreError:
    return core_error(
        ErrorCategory.UNAVAILABLE,
        "tool_operation_store_unavailable",
        "Tool operation store is unavailable",
        retryable=True,
    )
