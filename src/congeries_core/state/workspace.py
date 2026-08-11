"""Durable Workspace state ownership without application semantics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import ClassVar

from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import ArtifactId, WorkspaceId
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.scope import ScopeRef


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    CONTRACT_VERSION: ClassVar[str] = "1"

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

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.CONTRACT_VERSION,
            "workspace_id": self.workspace_id.value,
            "scope": self.scope.to_data(),
            "state_version": self.state_version,
            "values": dict(self.values),
            "artifact_refs": [item.value for item in self.artifact_refs],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkspaceState:
        expected = {
            "contract_version",
            "workspace_id",
            "scope",
            "state_version",
            "values",
            "artifact_refs",
        }
        if set(data) != expected:
            raise ValueError("workspace state fields do not match contract version 1")
        version = data["contract_version"]
        if not isinstance(version, str):
            raise ValueError("workspace contract version must be a string")
        if version != cls.CONTRACT_VERSION:
            raise ValueError("unsupported workspace contract version")
        raw_values = as_object(data["values"], "workspace values")
        values = {
            key: as_json_value(value, f"workspace value {key}")
            for key, value in raw_values.items()
        }
        return cls(
            workspace_id=WorkspaceId(str(data["workspace_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "workspace scope")),
            state_version=as_int(data["state_version"], "workspace state version"),
            values=values,
            artifact_refs=tuple(
                ArtifactId(str(item))
                for item in as_array(data["artifact_refs"], "workspace artifact refs")
            ),
        )
