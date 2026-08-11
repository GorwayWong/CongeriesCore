"""Run persistence and lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import CheckpointRef, DefinitionId, RunId
from congeries_core.runtime.run import (
    AuditFailureMode,
    Run,
    RunStateMachine,
    RunStatus,
    RunTransition,
)

from .repository import RunRepository


class RunEventPublisher(Protocol):
    async def run_state_changed(self, transition: RunTransition) -> None: ...


class NullRunEventPublisher:
    async def run_state_changed(self, transition: RunTransition) -> None:
        del transition


Mutation = Callable[[Run, int, datetime], RunTransition]


class RunService:
    def __init__(
        self,
        repository: RunRepository,
        clock: Clock,
        publisher: RunEventPublisher | None = None,
        state_machine: RunStateMachine | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._publisher = publisher or NullRunEventPublisher()
        self._machine = state_machine or RunStateMachine()

    async def create(self, run: Run) -> Run:
        await self._repository.add(run)
        return run

    async def get(self, run_id: RunId) -> Run:
        return await self._repository.get(run_id)

    async def _apply(
        self, run_id: RunId, expected_version: int, mutation: Mutation
    ) -> Run:
        previous = await self._repository.get(run_id)
        transition = mutation(previous, expected_version, self._clock.now())
        if transition.current is transition.previous:
            return transition.current
        committed = await self._repository.compare_and_set(
            transition.current, expected_version
        )
        await self._publisher.run_state_changed(
            RunTransition(transition.previous, committed, transition.reason)
        )
        return committed

    async def start(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(run_id, expected_version, self._machine.start)

    async def advance(
        self, run_id: RunId, expected_version: int, target: RunStatus
    ) -> Run:
        return await self._apply(
            run_id,
            expected_version,
            lambda run, version, now: self._machine.advance(run, target, version, now),
        )

    async def pause(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(run_id, expected_version, self._machine.pause)

    async def resume(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(run_id, expected_version, self._machine.resume)

    async def retry(
        self,
        run_id: RunId,
        expected_version: int,
        error: ErrorDetail,
    ) -> Run:
        return await self._apply(
            run_id,
            expected_version,
            lambda run, version, now: self._machine.retry(run, version, now, error),
        )

    async def redispatch_retry(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(
            run_id, expected_version, self._machine.redispatch_retry
        )

    async def recover(
        self,
        run_id: RunId,
        expected_version: int,
        checkpoint_ref: CheckpointRef | None = None,
    ) -> Run:
        return await self._apply(
            run_id,
            expected_version,
            lambda run, version, now: self._machine.recover(
                run, version, now, checkpoint_ref
            ),
        )

    async def commit_checkpoint(
        self,
        run_id: RunId,
        expected_version: int,
        checkpoint_ref: CheckpointRef,
        expected_previous_ref: CheckpointRef | None,
        *,
        definition_id: DefinitionId | None = None,
        graph_version: str | None = None,
    ) -> Run:
        return await self._apply(
            run_id,
            expected_version,
            lambda run, version, now: self._machine.commit_checkpoint(
                run,
                version,
                now,
                checkpoint_ref,
                expected_previous_ref,
                definition_id=definition_id,
                graph_version=graph_version,
            ),
        )

    async def complete(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(run_id, expected_version, self._machine.complete)

    async def fail(
        self,
        run_id: RunId,
        expected_version: int,
        error: ErrorDetail,
    ) -> Run:
        return await self._apply(
            run_id,
            expected_version,
            lambda run, version, now: self._machine.fail(run, version, now, error),
        )

    async def cancel(self, run_id: RunId, expected_version: int) -> Run:
        return await self._apply(run_id, expected_version, self._machine.cancel)

    async def handle_audit_failure(self, run_id: RunId, error: ErrorDetail) -> Run:
        run = await self._repository.get(run_id)
        if run.status.terminal:
            return run
        if run.control_policy.audit_failure_mode is AuditFailureMode.FAIL:
            return await self.fail(run_id, run.state_version, error)
        if run.status is RunStatus.PAUSED:
            return run
        return await self.pause(run_id, run.state_version)
