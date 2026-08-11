"""SQLite reference adapter for StorageProvider contract version 1."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from congeries_core.provider.storage import (
    ArtifactCursor,
    ArtifactPage,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactValue,
    StorageCapabilities,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import ArtifactId, ProviderId, WorkspaceId
from congeries_core.runtime.json_types import as_object
from congeries_core.runtime.scope import ScopeRef
from congeries_core.state.workspace import WorkspaceState


class SqliteStorageProvider:
    """Durable Workspace CAS and immutable Artifact storage using stdlib SQLite."""

    def __init__(
        self,
        provider_id: ProviderId,
        path: str | Path,
        *,
        max_artifact_bytes: int = 16 << 20,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.provider_id = provider_id
        self._path = str(Path(path))
        self._timeout = busy_timeout_seconds
        self._capabilities = StorageCapabilities(provider_id, max_artifact_bytes)
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def capabilities(self, context: RuntimeCallContext) -> StorageCapabilities:
        del context
        return self._capabilities

    async def create_workspace(
        self, workspace: WorkspaceState, context: RuntimeCallContext
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        if workspace.state_version != 0:
            raise core_error(
                ErrorCategory.CONFLICT,
                "invalid_initial_workspace_version",
                "new Workspace state version must be zero",
            )
        await self._ensure_initialized()
        return cast(
            WorkspaceState,
            await self._run(self._create_workspace_sync, workspace),
        )

    async def get_workspace(
        self,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace_id, scope, context)
        await self._ensure_initialized()
        workspace = cast(
            WorkspaceState,
            await self._run(self._get_workspace_sync, workspace_id),
        )
        if workspace.scope.key != scope.key:
            raise core_error(
                ErrorCategory.DENIED,
                "workspace_scope_mismatch",
                "Workspace does not belong to the requested Scope",
            )
        return workspace

    async def compare_and_set_workspace(
        self,
        workspace: WorkspaceState,
        expected_version: int,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        _require_workspace_context(workspace.workspace_id, workspace.scope, context)
        await self._ensure_initialized()
        return cast(
            WorkspaceState,
            await self._run(
                self._compare_and_set_workspace_sync, workspace, expected_version
            ),
        )

    async def put_artifact(
        self, value: ArtifactValue, context: RuntimeCallContext
    ) -> ArtifactRecord:
        record = value.record
        self._require_record(record, context)
        await self._ensure_initialized()
        return cast(
            ArtifactRecord,
            await self._run(self._put_artifact_sync, value),
        )

    async def get_artifact(
        self,
        artifact_id: ArtifactId,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> ArtifactValue:
        _require_workspace_context(workspace_id, scope, context)
        await self._ensure_initialized()
        value = cast(
            ArtifactValue,
            await self._run(self._get_artifact_sync, artifact_id),
        )
        if (
            value.record.workspace_id != workspace_id
            or value.record.scope.key != scope.key
        ):
            raise core_error(
                ErrorCategory.DENIED,
                "artifact_identity_mismatch",
                "Artifact does not belong to the requested Workspace and Scope",
            )
        return value

    async def list_artifacts(
        self, query: ArtifactQuery, context: RuntimeCallContext
    ) -> ArtifactPage:
        if query.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "storage_provider_mismatch",
                "Artifact query provider does not match storage provider",
            )
        _require_workspace_context(query.workspace_id, query.scope, context)
        await self._ensure_initialized()
        return cast(
            ArtifactPage,
            await self._run(self._list_artifacts_sync, query),
        )

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if not self._initialized:
                await self._run(self._initialize_sync)
                self._initialized = True

    async def _run(self, operation: Callable[..., object], *args: object) -> object:
        try:
            return await asyncio.to_thread(operation, *args)
        except CoreError:
            raise
        except sqlite3.Error as error:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "storage_backend_unavailable",
                "SQLite storage operation failed",
                retryable=True,
            ) from error

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
                CREATE TABLE IF NOT EXISTS storage_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    state_version INTEGER NOT NULL CHECK (state_version >= 0),
                    workspace_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
                    record_json TEXT NOT NULL,
                    content BLOB NOT NULL,
                    FOREIGN KEY (workspace_id)
                        REFERENCES storage_workspaces(workspace_id)
                );

                CREATE INDEX IF NOT EXISTS storage_artifacts_by_workspace
                ON storage_artifacts(
                    workspace_id, scope_key, created_at DESC, artifact_id DESC
                );
                """
            )

    def _create_workspace_sync(self, workspace: WorkspaceState) -> WorkspaceState:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO storage_workspaces(
                        workspace_id, scope_key, state_version, workspace_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        workspace.workspace_id.value,
                        _scope_key(workspace.scope),
                        workspace.state_version,
                        _json_dump(workspace.to_data()),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise core_error(
                ErrorCategory.CONFLICT,
                "workspace_already_exists",
                "Workspace identity already exists",
            ) from error
        return workspace

    def _get_workspace_sync(self, workspace_id: WorkspaceId) -> WorkspaceState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workspace_json FROM storage_workspaces WHERE workspace_id = ?",
                (workspace_id.value,),
            ).fetchone()
        if row is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workspace_not_found",
                "Workspace does not exist",
            )
        return WorkspaceState.from_data(_json_object(str(row["workspace_json"])))

    def _compare_and_set_workspace_sync(
        self, workspace: WorkspaceState, expected_version: int
    ) -> WorkspaceState:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT scope_key, state_version FROM storage_workspaces
                WHERE workspace_id = ?
                """,
                (workspace.workspace_id.value,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "workspace_not_found",
                    "Workspace does not exist",
                )
            if str(row["scope_key"]) != _scope_key(workspace.scope):
                connection.rollback()
                raise core_error(
                    ErrorCategory.DENIED,
                    "workspace_scope_mismatch",
                    "Workspace Scope cannot change during compare-and-set",
                )
            if int(row["state_version"]) != expected_version:
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_state_version",
                    "Workspace state version does not match",
                    retryable=True,
                )
            if workspace.state_version != expected_version + 1:
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "invalid_next_state_version",
                    "committed Workspace must increment state version exactly once",
                )
            cursor = connection.execute(
                """
                UPDATE storage_workspaces
                SET state_version = ?, workspace_json = ?
                WHERE workspace_id = ? AND state_version = ?
                """,
                (
                    workspace.state_version,
                    _json_dump(workspace.to_data()),
                    workspace.workspace_id.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_state_version",
                    "Workspace state changed during compare-and-set",
                    retryable=True,
                )
            connection.commit()
        return workspace

    def _put_artifact_sync(self, value: ArtifactValue) -> ArtifactRecord:
        record = value.record
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workspace = connection.execute(
                """
                SELECT workspace_json FROM storage_workspaces WHERE workspace_id = ?
                """,
                (record.workspace_id.value,),
            ).fetchone()
            if workspace is None:
                connection.rollback()
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "workspace_not_found",
                    "Artifact Workspace does not exist",
                )
            workspace_state = WorkspaceState.from_data(
                _json_object(str(workspace["workspace_json"]))
            )
            try:
                record.scope.require_narrower_than(workspace_state.scope)
            except CoreError as error:
                connection.rollback()
                raise core_error(
                    ErrorCategory.DENIED,
                    "artifact_scope_mismatch",
                    "Artifact Scope is outside its Workspace",
                ) from error
            existing = connection.execute(
                """
                SELECT record_json, content FROM storage_artifacts
                WHERE artifact_id = ?
                """,
                (record.artifact_id.value,),
            ).fetchone()
            if existing is not None:
                stored = ArtifactValue(
                    ArtifactRecord.from_data(
                        _json_object(str(existing["record_json"]))
                    ),
                    bytes(existing["content"]),
                )
                if stored == value:
                    connection.commit()
                    return stored.record
                connection.rollback()
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "artifact_identity_conflict",
                    "Artifact identity already exists with different content",
                )
            connection.execute(
                """
                INSERT INTO storage_artifacts(
                    artifact_id, workspace_id, scope_key, created_at, sha256,
                    byte_length, record_json, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.artifact_id.value,
                    record.workspace_id.value,
                    _scope_key(record.scope),
                    record.created_at.isoformat(),
                    record.sha256,
                    record.byte_length,
                    _json_dump(record.to_data()),
                    value.content,
                ),
            )
            connection.commit()
        return record

    def _get_artifact_sync(self, artifact_id: ArtifactId) -> ArtifactValue:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json, content FROM storage_artifacts
                WHERE artifact_id = ?
                """,
                (artifact_id.value,),
            ).fetchone()
        if row is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "artifact_not_found",
                "Artifact does not exist",
            )
        return ArtifactValue(
            ArtifactRecord.from_data(_json_object(str(row["record_json"]))),
            bytes(row["content"]),
        )

    def _list_artifacts_sync(self, query: ArtifactQuery) -> ArtifactPage:
        conditions = ["workspace_id = ?", "scope_key = ?"]
        params: list[object] = [query.workspace_id.value, _scope_key(query.scope)]
        if query.cursor is not None:
            conditions.append(
                "(created_at < ? OR (created_at = ? AND artifact_id < ?))"
            )
            created_at = query.cursor.before_created_at.isoformat()
            params.extend(
                [created_at, created_at, query.cursor.before_artifact_id.value]
            )
        params.append(query.limit + 1)
        statement = f"""
            SELECT record_json FROM storage_artifacts
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC, artifact_id DESC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(statement, params).fetchall()
        records = tuple(
            ArtifactRecord.from_data(_json_object(str(row["record_json"])))
            for row in rows[: query.limit]
        )
        next_cursor = None
        if len(rows) > query.limit:
            last = records[-1]
            next_cursor = ArtifactCursor(
                provider_id=self.provider_id,
                workspace_id=query.workspace_id,
                scope=query.scope,
                limit=query.limit,
                query_fingerprint=query.query_fingerprint,
                before_created_at=last.created_at,
                before_artifact_id=last.artifact_id,
            )
        return ArtifactPage(records, next_cursor)

    def _require_record(
        self, record: ArtifactRecord, context: RuntimeCallContext
    ) -> None:
        if record.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "storage_provider_mismatch",
                "Artifact provider does not match storage provider",
            )
        _require_workspace_context(record.workspace_id, record.scope, context)
        if record.byte_length > self._capabilities.max_artifact_bytes:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "artifact_too_large",
                "Artifact exceeds the provider byte limit",
            )


def _require_workspace_context(
    workspace_id: WorkspaceId, scope: ScopeRef, context: RuntimeCallContext
) -> None:
    scope.require_narrower_than(context.scope)
    if workspace_id != context.workspace_id:
        raise core_error(
            ErrorCategory.DENIED,
            "workspace_context_mismatch",
            "storage operation Workspace does not match RuntimeCallContext",
        )


def _scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.to_data(), sort_keys=True, separators=(",", ":"))


def _json_dump(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, object]:
    decoded: Any = json.loads(value)
    return as_object(decoded, "stored JSON value")
