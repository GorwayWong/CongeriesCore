"""Replaceable state repositories and in-memory reference implementations."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import RunId
from congeries_core.runtime.run import Run


class RunRepository(Protocol):
    async def add(self, run: Run) -> None: ...

    async def get(self, run_id: RunId) -> Run: ...

    async def compare_and_set(self, run: Run, expected_version: int) -> Run: ...


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[RunId, Run] = {}
        self._lock = RLock()

    async def add(self, run: Run) -> None:
        with self._lock:
            if run.run_id in self._runs:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "run_already_exists",
                    "Run identity already exists",
                )
            self._runs[run.run_id] = run

    async def get(self, run_id: RunId) -> Run:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError as error:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "run_not_found",
                    "Run does not exist",
                ) from error

    async def compare_and_set(self, run: Run, expected_version: int) -> Run:
        with self._lock:
            current = self._runs.get(run.run_id)
            if current is None:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "run_not_found",
                    "Run does not exist",
                )
            if current.state_version != expected_version:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "stale_state_version",
                    "Run state version does not match the expected version",
                    retryable=True,
                )
            if run.state_version != expected_version + 1:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "invalid_next_state_version",
                    "committed Run must increment state version exactly once",
                )
            self._runs[run.run_id] = run
            return run
