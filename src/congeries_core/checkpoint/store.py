"""CheckpointStore protocol, registry, and in-memory reference adapter."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import CheckpointRef, ProviderId

from .model import (
    Checkpoint,
    CheckpointCursor,
    CheckpointPage,
    CheckpointQuery,
    DeleteCheckpointRequest,
    DeleteCheckpointResult,
)


class CheckpointStore(Protocol):
    async def save(
        self, checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef: ...

    async def load(
        self, checkpoint_ref: CheckpointRef, context: RuntimeCallContext
    ) -> Checkpoint: ...

    async def list(
        self, query: CheckpointQuery, context: RuntimeCallContext
    ) -> CheckpointPage: ...

    async def delete(
        self, request: DeleteCheckpointRequest, context: RuntimeCallContext
    ) -> DeleteCheckpointResult: ...


class CheckpointStoreRegistry:
    def __init__(self) -> None:
        self._stores: dict[ProviderId, CheckpointStore] = {}

    def register(self, provider_id: ProviderId, store: CheckpointStore) -> None:
        if provider_id in self._stores:
            raise core_error(
                ErrorCategory.CONFLICT,
                "checkpoint_store_already_registered",
                "CheckpointStore provider is already registered",
            )
        self._stores[provider_id] = store

    def get(self, provider_id: ProviderId) -> CheckpointStore:
        try:
            return self._stores[provider_id]
        except KeyError as error:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "checkpoint_store_unavailable",
                "CheckpointStore provider is not registered",
                retryable=True,
            ) from error


class InMemoryCheckpointStore:
    """Thread-safe atomic Store with per-Run sequence uniqueness."""

    def __init__(self, provider_id: ProviderId) -> None:
        self.provider_id = provider_id
        self._checkpoints: dict[CheckpointRef, Checkpoint] = {}
        self._sequences: dict[tuple[str, int], CheckpointRef] = {}
        self._lock = RLock()

    async def save(
        self, checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> CheckpointRef:
        checkpoint.verify_integrity()
        checkpoint.scope.require_narrower_than(context.scope)
        with self._lock:
            existing = self._checkpoints.get(checkpoint.ref)
            if existing is not None:
                if existing.integrity.digest == checkpoint.integrity.digest:
                    return checkpoint.ref
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "checkpoint_identity_conflict",
                    "checkpoint reference already exists with different content",
                )
            sequence_key = checkpoint.run_id.value, checkpoint.sequence
            if sequence_key in self._sequences:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "checkpoint_sequence_conflict",
                    "checkpoint sequence is already occupied",
                    retryable=True,
                )
            run_sequences = [
                sequence
                for (run_id, sequence) in self._sequences
                if run_id == checkpoint.run_id.value
            ]
            expected_sequence = max(run_sequences, default=0) + 1
            if checkpoint.sequence != expected_sequence:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_checkpoint_sequence",
                    "checkpoint sequence is not the next Store sequence",
                    retryable=True,
                )
            if checkpoint.previous_checkpoint_ref is not None:
                previous = self._checkpoints.get(checkpoint.previous_checkpoint_ref)
                if previous is None or previous.run_id != checkpoint.run_id:
                    raise core_error(
                        ErrorCategory.CONFLICT,
                        "invalid_checkpoint_predecessor",
                        "checkpoint predecessor is missing or belongs to another Run",
                    )
                previous_effects = {
                    item.idempotency_key: item for item in previous.side_effects
                }
                current_effects = {
                    item.idempotency_key: item for item in checkpoint.side_effects
                }
                if any(
                    current_effects.get(key) != value
                    for key, value in previous_effects.items()
                ):
                    raise core_error(
                        ErrorCategory.CONFLICT,
                        "checkpoint_idempotency_conflict",
                        "checkpoint changed or removed a durable side-effect record",
                    )
            self._checkpoints[checkpoint.ref] = checkpoint
            self._sequences[sequence_key] = checkpoint.ref
            return checkpoint.ref

    async def load(
        self, checkpoint_ref: CheckpointRef, context: RuntimeCallContext
    ) -> Checkpoint:
        with self._lock:
            checkpoint = self._checkpoints.get(checkpoint_ref)
        if checkpoint is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_not_found",
                "checkpoint does not exist",
            )
        checkpoint.scope.require_narrower_than(context.scope)
        checkpoint.verify_integrity()
        return checkpoint

    async def list(
        self, query: CheckpointQuery, context: RuntimeCallContext
    ) -> CheckpointPage:
        if query.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_provider_mismatch",
                "checkpoint query provider does not match Store",
            )
        query.scope.require_narrower_than(context.scope)
        self._validate_cursor(query)
        before_sequence = query.cursor.next_sequence if query.cursor else None
        with self._lock:
            items = [
                item
                for item in self._checkpoints.values()
                if item.run_id == query.run_id
                and item.scope.key == query.scope.key
                and (
                    query.graph_version is None
                    or item.graph_version == query.graph_version
                )
                and (before_sequence is None or item.sequence < before_sequence)
            ]
        items.sort(key=lambda item: item.sequence, reverse=True)
        selected = tuple(items[: query.limit])
        next_cursor = None
        if len(items) > query.limit:
            next_cursor = CheckpointCursor(
                provider_id=self.provider_id,
                run_id=query.run_id,
                scope=query.scope,
                graph_version=query.graph_version,
                limit=query.limit,
                query_fingerprint=query.query_fingerprint,
                next_sequence=selected[-1].sequence,
            )
        return CheckpointPage(selected, next_cursor)

    async def delete(
        self, request: DeleteCheckpointRequest, context: RuntimeCallContext
    ) -> DeleteCheckpointResult:
        if request.provider_id != self.provider_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_provider_mismatch",
                "delete provider does not match Store",
            )
        request.scope.require_narrower_than(context.scope)
        with self._lock:
            checkpoint = self._checkpoints.get(request.checkpoint_ref)
            if checkpoint is None:
                return DeleteCheckpointResult(request.checkpoint_ref, False)
            if (
                checkpoint.run_id != request.run_id
                or checkpoint.scope.key != request.scope.key
            ):
                raise core_error(
                    ErrorCategory.DENIED,
                    "checkpoint_identity_mismatch",
                    "checkpoint delete identity does not match stored ownership",
                )
            del self._checkpoints[request.checkpoint_ref]
            self._sequences.pop((checkpoint.run_id.value, checkpoint.sequence), None)
        return DeleteCheckpointResult(request.checkpoint_ref, True)

    def _validate_cursor(self, query: CheckpointQuery) -> None:
        cursor = query.cursor
        if cursor is None:
            return
        if (
            cursor.provider_id != self.provider_id
            or cursor.run_id != query.run_id
            or cursor.scope.key != query.scope.key
            or cursor.graph_version != query.graph_version
            or cursor.limit != query.limit
            or cursor.query_fingerprint != query.query_fingerprint
        ):
            raise core_error(
                ErrorCategory.CONFLICT,
                "checkpoint_cursor_drift",
                "checkpoint cursor does not match the effective query",
            )
