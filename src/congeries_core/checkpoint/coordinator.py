"""Checkpoint commit, migration, fallback, deletion, and recovery coordination."""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import suppress
from typing import Protocol

from congeries_core.policy.authorization import AuditFailureHandler
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import CheckpointRef, ProviderId, RunId
from congeries_core.runtime.run import RunStatus, WorkflowRun
from congeries_core.state.service import RunService

from .gateway import CheckpointGateway
from .model import (
    ApprovalDecision,
    ApprovalRequest,
    Checkpoint,
    CheckpointMigrationRequest,
    CheckpointMigrator,
    CheckpointQuery,
    CheckpointRestorer,
    DeleteCheckpointRequest,
    DeleteCheckpointResult,
    RecoveryRequest,
    RecoveryResult,
)


class CheckpointEventPublisher(Protocol):
    async def checkpoint_saved(
        self, checkpoint: Checkpoint, run: WorkflowRun, context: RuntimeCallContext
    ) -> None: ...

    async def checkpoint_failed(
        self, checkpoint: Checkpoint, error: ErrorDetail, context: RuntimeCallContext
    ) -> None: ...

    async def checkpoint_migration_authorized(
        self,
        source: Checkpoint,
        migrated: Checkpoint,
        context: RuntimeCallContext,
    ) -> None: ...

    async def checkpoint_fallback_authorized(
        self,
        source_ref: CheckpointRef,
        fallback: Checkpoint,
        context: RuntimeCallContext,
    ) -> None: ...

    async def approval_requested(
        self, request: ApprovalRequest, context: RuntimeCallContext
    ) -> None: ...

    async def approval_decided(
        self, decision: ApprovalDecision, context: RuntimeCallContext
    ) -> None: ...


class NullCheckpointEventPublisher:
    async def checkpoint_saved(
        self, checkpoint: Checkpoint, run: WorkflowRun, context: RuntimeCallContext
    ) -> None:
        del checkpoint, run, context

    async def checkpoint_failed(
        self, checkpoint: Checkpoint, error: ErrorDetail, context: RuntimeCallContext
    ) -> None:
        del checkpoint, error, context

    async def checkpoint_migration_authorized(
        self,
        source: Checkpoint,
        migrated: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        del source, migrated, context

    async def checkpoint_fallback_authorized(
        self,
        source_ref: CheckpointRef,
        fallback: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        del source_ref, fallback, context

    async def approval_requested(
        self, request: ApprovalRequest, context: RuntimeCallContext
    ) -> None:
        del request, context

    async def approval_decided(
        self, decision: ApprovalDecision, context: RuntimeCallContext
    ) -> None:
        del decision, context


class CheckpointMigratorRegistry:
    def __init__(self) -> None:
        self._migrators: dict[tuple[str, str, str, str, str], CheckpointMigrator] = {}

    @staticmethod
    def key(request: CheckpointMigrationRequest) -> tuple[str, str, str, str, str]:
        return (
            request.workflow_id.value,
            request.source_definition_id.value,
            request.source_graph_version,
            request.target_definition_id.value,
            request.target_graph_version,
        )

    def register(
        self, request: CheckpointMigrationRequest, migrator: CheckpointMigrator
    ) -> None:
        key = self.key(request)
        if key in self._migrators:
            raise core_error(
                ErrorCategory.CONFLICT,
                "checkpoint_migrator_already_registered",
                "CheckpointMigrator version pair is already registered",
            )
        self._migrators[key] = migrator

    def get(self, request: CheckpointMigrationRequest) -> CheckpointMigrator:
        try:
            return self._migrators[self.key(request)]
        except KeyError as error:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "checkpoint_migrator_missing",
                "no CheckpointMigrator supports the requested version pair",
            ) from error


class CheckpointCoordinator:
    def __init__(
        self,
        gateway: CheckpointGateway,
        runs: RunService,
        publisher: CheckpointEventPublisher | None = None,
    ) -> None:
        self._gateway = gateway
        self._runs = runs
        self._publisher = publisher or NullCheckpointEventPublisher()

    async def save(
        self,
        provider_id: ProviderId,
        checkpoint: Checkpoint,
        expected_run_version: int,
        context: RuntimeCallContext,
    ) -> WorkflowRun:
        run = await self._workflow_run(checkpoint.run_id)
        self._validate_for_run(checkpoint, run, context)
        if run.latest_checkpoint_ref == checkpoint.ref:
            existing = await self._gateway.load(
                provider_id, checkpoint.ref, run.run_id, run.scope, context
            )
            if existing.integrity.digest != checkpoint.integrity.digest:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "checkpoint_identity_conflict",
                    "committed checkpoint reference has different content",
                )
            return run
        if run.state_version != expected_run_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_state_version",
                "Run state version does not match the expected version",
                retryable=True,
            )
        expected_sequence = 1
        if run.latest_checkpoint_ref is not None:
            previous = await self._gateway.load(
                provider_id,
                run.latest_checkpoint_ref,
                run.run_id,
                run.scope,
                context,
            )
            expected_sequence = previous.sequence + 1
        if (
            checkpoint.previous_checkpoint_ref != run.latest_checkpoint_ref
            or checkpoint.sequence != expected_sequence
        ):
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_checkpoint_marker",
                "checkpoint sequence or predecessor does not match the Run marker",
                retryable=True,
            )
        try:
            await self._gateway.save(provider_id, checkpoint, context)
            committed = await self._runs.commit_checkpoint(
                run.run_id,
                expected_run_version,
                checkpoint.ref,
                run.latest_checkpoint_ref,
            )
        except CoreError as error:
            await self._publish_failed(checkpoint, error.detail, context)
            raise
        if not isinstance(committed, WorkflowRun):
            raise AssertionError("checkpoint commit did not return WorkflowRun")
        await self._publish_saved(checkpoint, committed, context)
        return committed

    async def delete_orphan(
        self, request: DeleteCheckpointRequest, context: RuntimeCallContext
    ) -> DeleteCheckpointResult:
        run = await self._workflow_run(request.run_id)
        if run.scope.key != request.scope.key:
            raise core_error(
                ErrorCategory.DENIED,
                "checkpoint_scope_mismatch",
                "delete Scope does not match WorkflowRun",
            )
        reachable: set[CheckpointRef] = set()
        current_ref = run.latest_checkpoint_ref
        while current_ref is not None:
            if current_ref in reachable:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "checkpoint_chain_cycle",
                    "committed checkpoint chain contains a cycle",
                )
            reachable.add(current_ref)
            checkpoint = await self._gateway.load(
                request.provider_id,
                current_ref,
                run.run_id,
                run.scope,
                context,
            )
            current_ref = checkpoint.previous_checkpoint_ref
        if request.checkpoint_ref in reachable:
            raise core_error(
                ErrorCategory.CONFLICT,
                "checkpoint_not_orphan",
                "committed checkpoint chain cannot be deleted",
            )
        return await self._gateway.delete(request, context)

    async def _workflow_run(self, run_id: RunId) -> WorkflowRun:
        run = await self._runs.get(run_id)
        if not isinstance(run, WorkflowRun):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_requires_workflow_run",
                "checkpoint coordination requires a WorkflowRun",
            )
        return run

    def _validate_for_run(
        self,
        checkpoint: Checkpoint,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> None:
        checkpoint.verify_integrity()
        if context.run_id != run.run_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_context_run_mismatch",
                "RuntimeCallContext does not own the WorkflowRun",
            )
        if (
            checkpoint.run_id != run.run_id
            or checkpoint.workflow_id != run.workflow_id
            or checkpoint.definition_id != run.definition_id
            or checkpoint.graph_version != run.graph_version
            or checkpoint.scope.key != run.scope.key
            or checkpoint.attempt != run.attempt
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "checkpoint_run_identity_mismatch",
                "checkpoint identity does not match WorkflowRun",
            )
        if run.status.terminal:
            raise core_error(
                ErrorCategory.CONFLICT,
                "terminal_checkpoint_commit",
                "terminal WorkflowRun cannot commit a checkpoint",
            )

    async def _publish_failed(
        self,
        checkpoint: Checkpoint,
        error: ErrorDetail,
        context: RuntimeCallContext,
    ) -> None:
        with suppress(Exception):
            await self._publisher.checkpoint_failed(checkpoint, error, context)

    async def _publish_saved(
        self,
        checkpoint: Checkpoint,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> None:
        with suppress(Exception):
            await self._publisher.checkpoint_saved(checkpoint, run, context)


class RecoveryCoordinator:
    def __init__(
        self,
        *,
        gateway: CheckpointGateway,
        runs: RunService,
        migrators: CheckpointMigratorRegistry,
        restorer: CheckpointRestorer,
        audit_failure_handler: AuditFailureHandler,
        publisher: CheckpointEventPublisher | None = None,
    ) -> None:
        self._gateway = gateway
        self._runs = runs
        self._migrators = migrators
        self._restorer = restorer
        self._audit_failure = audit_failure_handler
        self._publisher = publisher or NullCheckpointEventPublisher()

    async def recover(
        self, request: RecoveryRequest, context: RuntimeCallContext
    ) -> RecoveryResult:
        run = await self._runs.get(request.run_id)
        if not isinstance(run, WorkflowRun):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "recovery_requires_workflow_run",
                "checkpoint recovery requires a WorkflowRun",
            )
        if context.run_id != run.run_id or context.scope.key != run.scope.key:
            raise core_error(
                ErrorCategory.DENIED,
                "recovery_context_mismatch",
                "recovery context does not match WorkflowRun ownership",
            )
        if run.status.terminal or run.status is RunStatus.CREATED:
            raise core_error(
                ErrorCategory.CONFLICT,
                "illegal_recovery_state",
                "WorkflowRun state cannot be recovered",
            )
        if run.latest_checkpoint_ref is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "missing_recovery_checkpoint",
                "WorkflowRun has no committed checkpoint marker",
            )
        if run.state_version != request.expected_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_state_version",
                "Run state version does not match the expected version",
                retryable=True,
            )

        checkpoint, fell_back, run = await self._load_with_fallback(
            request, run, context
        )
        self._validate_loaded(checkpoint, run)
        migrated = False
        if (
            checkpoint.definition_id != request.target_definition_id
            or checkpoint.graph_version != request.target_graph_version
        ):
            checkpoint, run = await self._migrate(request, run, checkpoint, context)
            migrated = True

        recovering = await self._runs.recover(
            run.run_id, run.state_version, checkpoint.ref
        )
        if not isinstance(recovering, WorkflowRun):
            raise AssertionError("recovery transition did not return WorkflowRun")
        try:
            await self._restorer.restore(checkpoint, context)
        except Exception as error:
            detail = (
                error.detail
                if isinstance(error, CoreError)
                else ErrorDetail(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "checkpoint_restore_failed",
                    "checkpoint restoration failed",
                )
            )
            if request.policy.fail_on_restore_error:
                await self._runs.fail(
                    recovering.run_id, recovering.state_version, detail
                )
            raise
        running = await self._runs.advance(
            recovering.run_id, recovering.state_version, RunStatus.RUNNING
        )
        if not isinstance(running, WorkflowRun):
            raise AssertionError("restored Run is not WorkflowRun")
        return RecoveryResult(running, checkpoint, migrated, fell_back)

    async def _load_with_fallback(
        self,
        request: RecoveryRequest,
        run: WorkflowRun,
        context: RuntimeCallContext,
    ) -> tuple[Checkpoint, bool, WorkflowRun]:
        marker = run.latest_checkpoint_ref
        if marker is None:
            raise AssertionError("recovery marker was validated")
        try:
            checkpoint = await self._gateway.load(
                request.provider_id, marker, run.run_id, run.scope, context
            )
            return checkpoint, False, run
        except CoreError as error:
            if (
                error.detail.code != "checkpoint_integrity_failure"
                or not request.policy.allow_corrupt_fallback
            ):
                raise
        query = CheckpointQuery(
            provider_id=request.provider_id,
            run_id=run.run_id,
            scope=run.scope,
            graph_version=None,
            limit=1000,
        )
        by_ref: dict[CheckpointRef, Checkpoint] = {}
        while True:
            page = await self._gateway.list(query, context)
            by_ref.update((item.ref, item) for item in page.items)
            if page.next_cursor is None:
                break
            query = CheckpointQuery(
                provider_id=request.provider_id,
                run_id=run.run_id,
                scope=run.scope,
                graph_version=None,
                limit=query.limit,
                cursor=page.next_cursor,
            )
        corrupt = by_ref.get(marker)
        if corrupt is None:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_fallback_source_missing",
                "corrupt marker is absent from checkpoint listing",
            )
        candidate_ref = corrupt.previous_checkpoint_ref
        candidate: Checkpoint | None = None
        seen = {marker}
        while candidate_ref is not None:
            if candidate_ref in seen:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "checkpoint_chain_cycle",
                    "checkpoint fallback chain contains a cycle",
                )
            seen.add(candidate_ref)
            item = by_ref.get(candidate_ref)
            if item is None:
                break
            try:
                item.verify_integrity()
            except CoreError:
                candidate_ref = item.previous_checkpoint_ref
                continue
            candidate = item
            break
        if candidate is None:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_fallback_unavailable",
                "no earlier valid committed checkpoint is available",
            )
        await self._audit(
            run.run_id,
            context,
            self._publisher.checkpoint_fallback_authorized(marker, candidate, context),
        )
        committed = await self._runs.commit_checkpoint(
            run.run_id,
            run.state_version,
            candidate.ref,
            marker,
        )
        if not isinstance(committed, WorkflowRun):
            raise AssertionError("fallback marker commit did not return WorkflowRun")
        return candidate, True, committed

    async def _migrate(
        self,
        request: RecoveryRequest,
        run: WorkflowRun,
        source: Checkpoint,
        context: RuntimeCallContext,
    ) -> tuple[Checkpoint, WorkflowRun]:
        migration_request = CheckpointMigrationRequest(
            workflow_id=run.workflow_id,
            source_definition_id=source.definition_id,
            source_graph_version=source.graph_version,
            target_definition_id=request.target_definition_id,
            target_graph_version=request.target_graph_version,
        )
        migrator = self._migrators.get(migration_request)
        migrated = await migrator.migrate(source, migration_request, context)
        migrated.verify_integrity()
        if (
            migrated.ref == source.ref
            or migrated.run_id != source.run_id
            or migrated.workflow_id != source.workflow_id
            or migrated.scope.key != source.scope.key
            or migrated.sequence != source.sequence + 1
            or migrated.previous_checkpoint_ref != source.ref
            or migrated.definition_id != request.target_definition_id
            or migrated.graph_version != request.target_graph_version
            or migrated.external_refs != source.external_refs
            or migrated.side_effects != source.side_effects
        ):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "invalid_checkpoint_migration",
                "CheckpointMigrator changed protected identity or references",
            )
        await self._gateway.save(request.provider_id, migrated, context)
        await self._audit(
            run.run_id,
            context,
            self._publisher.checkpoint_migration_authorized(source, migrated, context),
        )
        committed = await self._runs.commit_checkpoint(
            run.run_id,
            run.state_version,
            migrated.ref,
            source.ref,
            definition_id=request.target_definition_id,
            graph_version=request.target_graph_version,
        )
        if not isinstance(committed, WorkflowRun):
            raise AssertionError("migration marker commit did not return WorkflowRun")
        with suppress(Exception):
            await self._publisher.checkpoint_saved(migrated, committed, context)
        return migrated, committed

    def _validate_loaded(self, checkpoint: Checkpoint, run: WorkflowRun) -> None:
        checkpoint.verify_integrity()
        if (
            checkpoint.ref != run.latest_checkpoint_ref
            or checkpoint.run_id != run.run_id
            or checkpoint.workflow_id != run.workflow_id
            or checkpoint.scope.key != run.scope.key
        ):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_run_identity_mismatch",
                "committed checkpoint does not match WorkflowRun identity",
            )

    async def _audit(
        self,
        run_id: RunId,
        context: RuntimeCallContext,
        operation: Awaitable[None],
    ) -> None:
        del context
        try:
            await operation
        except CoreError as error:
            await self._audit_failure.handle(run_id, error.detail)
            raise
