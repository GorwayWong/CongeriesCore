"""Durable Workspace state ownership without application semantics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import ArtifactId, WorkspaceId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.scope import ScopeRef


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    workspace_id: WorkspaceId
    scope: ScopeRef
    state_version: int = 0
    values: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    artifact_refs: tuple[ArtifactId, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.state_version < 0:
            raise ValueError("workspace state version must not be negative")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def update(
        self,
        expected_version: int,
        patch: Mapping[str, JsonValue],
        *,
        artifact_refs: tuple[ArtifactId, ...] | None = None,
    ) -> WorkspaceState:
        if self.state_version != expected_version:
            raise core_error(
                ErrorCategory.CONFLICT,
                "stale_state_version",
                "Workspace state version does not match",
                retryable=True,
            )
        values = dict(self.values)
        values.update(patch)
        return replace(
            self,
            state_version=self.state_version + 1,
            values=values,
            artifact_refs=(
                artifact_refs if artifact_refs is not None else self.artifact_refs
            ),
        )
