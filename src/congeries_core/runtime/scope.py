"""Generic namespaced runtime Scope references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .errors import ErrorCategory, core_error
from .json_types import as_object

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


class CoreScopeKind(StrEnum):
    RUN = "run"
    WORKSPACE = "workspace"
    SESSION = "session"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class ScopeRef:
    namespace: str
    kind: str
    id: str
    parent: ScopeRef | None = None

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.namespace):
            raise ValueError("scope namespace is invalid")
        if not _NAME_PATTERN.fullmatch(self.kind):
            raise ValueError("scope kind is invalid")
        if not self.id or self.id != self.id.strip():
            raise ValueError("scope id must be non-empty and trimmed")
        if len(self.id) > 255:
            raise ValueError("scope id must not exceed 255 characters")
        if self.depth > 64:
            raise ValueError("scope ancestry exceeds 64 levels")

    @classmethod
    def core(
        cls,
        kind: CoreScopeKind,
        id: str,
        parent: ScopeRef | None = None,
    ) -> ScopeRef:
        return cls(namespace="core", kind=kind.value, id=id, parent=parent)

    @property
    def depth(self) -> int:
        depth = 1
        current = self.parent
        while current is not None:
            depth += 1
            current = current.parent
        return depth

    def is_equal_or_descendant_of(self, other: ScopeRef) -> bool:
        current: ScopeRef | None = self
        while current is not None:
            if current.key == other.key:
                return True
            current = current.parent
        return False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.namespace, self.kind, self.id

    def require_narrower_than(self, other: ScopeRef) -> None:
        if not self.is_equal_or_descendant_of(other):
            raise core_error(
                ErrorCategory.DENIED,
                "scope_broadening_denied",
                "child calls may not broaden their effective scope",
            )

    def to_data(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "kind": self.kind,
            "id": self.id,
            "parent": self.parent.to_data() if self.parent else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ScopeRef:
        raw_parent = data.get("parent")
        parent = (
            cls.from_data(as_object(raw_parent, "scope parent"))
            if raw_parent is not None
            else None
        )
        return cls(
            namespace=str(data["namespace"]),
            kind=str(data["kind"]),
            id=str(data["id"]),
            parent=parent,
        )
