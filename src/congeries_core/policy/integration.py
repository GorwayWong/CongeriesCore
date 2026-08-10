"""Adapters connecting authorization audit failure to Run control."""

from __future__ import annotations

from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import RunId
from congeries_core.state.service import RunService


class RunAuditFailureHandler:
    def __init__(self, run_service: RunService) -> None:
        self._runs = run_service

    async def handle(self, run_id: RunId, error: ErrorDetail) -> None:
        await self._runs.handle_audit_failure(run_id, error)
