"""Strict, versioned public contracts for Plugin v1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import total_ordering
from typing import cast

from congeries_core.policy.authorization import ActionRef
from congeries_core.runtime.json_types import as_array, as_object
from congeries_core.runtime.scope import ScopeRef

PLUGIN_MANIFEST_CONTRACT_VERSION = "1"

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_COMPARATOR_PATTERN = re.compile(r"^(>=|<=|>|<|=)?(.+)$")
_NAMESPACED_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SCOPE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.major, self.minor, self.patch) < 0:
            raise ValueError("semantic version numbers must be non-negative")
        for identifier in (*self.prerelease, *self.build):
            if not identifier:
                raise ValueError("semantic version identifiers must be non-empty")

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _SEMVER_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("version must be canonical SemVer 2.0")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
            build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        left = self.major, self.minor, self.patch
        right = other.major, other.minor, other.patch
        if left != right:
            return left < right
        return _compare_prerelease(self.prerelease, other.prerelease) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return False
        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease,
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease,
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


@dataclass(frozen=True, slots=True)
class VersionComparator:
    operator: str
    version: SemVer

    def __post_init__(self) -> None:
        if self.operator not in {"=", ">", ">=", "<", "<="}:
            raise ValueError("unsupported version comparator")

    def matches(self, version: SemVer) -> bool:
        if self.operator == "=":
            return version == self.version
        if self.operator == ">":
            return version > self.version
        if self.operator == ">=":
            return version >= self.version
        if self.operator == "<":
            return version < self.version
        return version <= self.version

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"


@dataclass(frozen=True, slots=True)
class VersionRange:
    comparators: tuple[VersionComparator, ...]

    def __post_init__(self) -> None:
        if not self.comparators:
            raise ValueError("version range requires at least one comparator")

    @classmethod
    def parse(cls, value: str) -> VersionRange:
        if not value or value != value.strip():
            raise ValueError("version range must be non-empty and trimmed")
        parts = value.split(",")
        comparators: list[VersionComparator] = []
        for part in parts:
            if not part or part != part.strip():
                raise ValueError("version comparators must not contain whitespace")
            match = _COMPARATOR_PATTERN.fullmatch(part)
            if match is None:
                raise ValueError("invalid version comparator")
            operator = match.group(1) or "="
            comparators.append(
                VersionComparator(operator, SemVer.parse(match.group(2)))
            )
        return cls(tuple(comparators))

    def matches(self, version: SemVer) -> bool:
        return all(comparator.matches(version) for comparator in self.comparators)

    def __str__(self) -> str:
        if len(self.comparators) == 1 and self.comparators[0].operator == "=":
            return str(self.comparators[0].version)
        return ",".join(str(item) for item in self.comparators)


class CapabilityType(StrEnum):
    WORKFLOW = "workflow"
    SKILL = "skill"
    TOOL = "tool"
    CONTEXT_PROVIDER = "context_provider"
    MEMORY_PROVIDER = "memory_provider"
    MODEL_PROVIDER = "model_provider"
    STORAGE_PROVIDER = "storage_provider"
    MCP_ADAPTER = "mcp_adapter"


class DependencyKind(StrEnum):
    PLUGIN = "plugin"
    CAPABILITY = "capability"


class PluginLifecycleState(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    LOADED = "loaded"
    REGISTERED = "registered"
    ACTIVE = "active"
    DRAINING = "draining"
    UNREGISTERED = "unregistered"
    UNLOADED = "unloaded"


class PluginHookName(StrEnum):
    ON_LOAD = "on_load"
    ON_ACTIVATE = "on_activate"
    ON_DRAIN = "on_drain"
    ON_UNLOAD = "on_unload"


@dataclass(frozen=True, slots=True, order=True)
class PluginRef:
    name: str
    version: SemVer

    def __post_init__(self) -> None:
        _require_namespaced_id(self.name, "Plugin name")

    def to_data(self) -> dict[str, str]:
        return {"name": self.name, "version": str(self.version)}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PluginRef:
        _require_fields(data, {"name", "version"}, "Plugin reference")
        return cls(
            _string(data["name"], "Plugin name"),
            SemVer.parse(_string(data["version"], "Plugin version")),
        )


@dataclass(frozen=True, slots=True)
class PluginPermission:
    action: ActionRef
    scope_pattern: str

    def __post_init__(self) -> None:
        namespace, kind, identifier = _scope_pattern_parts(self.scope_pattern)
        if not _SCOPE_NAME_PATTERN.fullmatch(namespace):
            raise ValueError("permission Scope namespace is invalid")
        if not _SCOPE_NAME_PATTERN.fullmatch(kind):
            raise ValueError("permission Scope kind is invalid")
        if (
            not identifier
            or identifier != identifier.strip()
            or len(identifier) > 255
            or ("*" in identifier and identifier != "*")
        ):
            raise ValueError("permission Scope identifier is invalid")

    def permits(self, scope: ScopeRef) -> bool:
        """Return whether the effective Scope or one of its ancestors matches."""

        namespace, kind, identifier = _scope_pattern_parts(self.scope_pattern)
        current: ScopeRef | None = scope
        while current is not None:
            if (
                current.namespace == namespace
                and current.kind == kind
                and (identifier == "*" or current.id == identifier)
            ):
                return True
            current = current.parent
        return False

    def to_data(self) -> dict[str, object]:
        return {"action": self.action.to_data(), "scope_pattern": self.scope_pattern}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PluginPermission:
        _require_fields(data, {"action", "scope_pattern"}, "Plugin permission")
        return cls(
            ActionRef.from_data(as_object(data["action"], "permission action")),
            _string(data["scope_pattern"], "permission Scope pattern"),
        )


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    type: CapabilityType
    capability_id: str
    contract_version: SemVer
    entry: str
    permissions: tuple[PluginPermission, ...] = ()

    def __post_init__(self) -> None:
        _require_namespaced_id(self.capability_id, "capability identifier")
        _require_trimmed(self.entry, "capability entry")
        if len({permission.action.key for permission in self.permissions}) != len(
            self.permissions
        ):
            raise ValueError("capability permissions must not duplicate actions")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.type.value, self.capability_id, str(self.contract_version)

    def to_data(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "capability_id": self.capability_id,
            "contract_version": str(self.contract_version),
            "entry": self.entry,
            "permissions": [item.to_data() for item in self.permissions],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CapabilityDeclaration:
        _require_fields(
            data,
            {"type", "capability_id", "contract_version", "entry", "permissions"},
            "capability declaration",
        )
        return cls(
            CapabilityType(_string(data["type"], "capability type")),
            _string(data["capability_id"], "capability identifier"),
            SemVer.parse(_string(data["contract_version"], "capability version")),
            _string(data["entry"], "capability entry"),
            tuple(
                PluginPermission.from_data(as_object(item, "capability permission"))
                for item in as_array(data["permissions"], "capability permissions")
            ),
        )


@dataclass(frozen=True, slots=True)
class PluginDependency:
    kind: DependencyKind
    version_range: VersionRange
    plugin_id: str | None = None
    capability_type: CapabilityType | None = None
    capability_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DependencyKind.PLUGIN:
            if self.plugin_id is None or self.capability_type or self.capability_id:
                raise ValueError("Plugin dependency fields are invalid")
            _require_namespaced_id(self.plugin_id, "dependency Plugin identifier")
        else:
            if (
                self.plugin_id
                or self.capability_type is None
                or self.capability_id is None
            ):
                raise ValueError("capability dependency fields are invalid")
            _require_namespaced_id(
                self.capability_id, "dependency capability identifier"
            )

    @property
    def stable_key(self) -> tuple[str, str, str]:
        identifier = self.plugin_id or cast(str, self.capability_id)
        capability_type = self.capability_type.value if self.capability_type else ""
        return self.kind.value, capability_type, identifier

    def to_data(self) -> dict[str, object]:
        if self.kind is DependencyKind.PLUGIN:
            return {
                "kind": self.kind.value,
                "plugin_id": self.plugin_id,
                "version_range": str(self.version_range),
            }
        return {
            "kind": self.kind.value,
            "capability_type": cast(CapabilityType, self.capability_type).value,
            "capability_id": self.capability_id,
            "version_range": str(self.version_range),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PluginDependency:
        kind = DependencyKind(_string(data.get("kind"), "dependency kind"))
        if kind is DependencyKind.PLUGIN:
            _require_fields(
                data, {"kind", "plugin_id", "version_range"}, "Plugin dependency"
            )
            return cls(
                kind,
                VersionRange.parse(_string(data["version_range"], "dependency range")),
                plugin_id=_string(data["plugin_id"], "dependency Plugin identifier"),
            )
        _require_fields(
            data,
            {"kind", "capability_type", "capability_id", "version_range"},
            "capability dependency",
        )
        return cls(
            kind,
            VersionRange.parse(_string(data["version_range"], "dependency range")),
            capability_type=CapabilityType(
                _string(data["capability_type"], "capability type")
            ),
            capability_id=_string(data["capability_id"], "capability identifier"),
        )


@dataclass(frozen=True, slots=True)
class PluginManifest:
    contract_version: str
    name: str
    version: SemVer
    core_api: VersionRange
    entrypoint: str
    provides: tuple[CapabilityDeclaration, ...]
    requires: tuple[PluginDependency, ...]
    permissions: tuple[PluginPermission, ...]
    lifecycle: frozenset[PluginHookName]

    def __post_init__(self) -> None:
        if self.contract_version != PLUGIN_MANIFEST_CONTRACT_VERSION:
            raise ValueError("unsupported Plugin manifest contract version")
        _require_namespaced_id(self.name, "Plugin name")
        _require_trimmed(self.entrypoint, "Plugin entrypoint")
        capability_ids = {(item.type, item.capability_id) for item in self.provides}
        if len(capability_ids) != len(self.provides):
            raise ValueError("Plugin capability identifiers must be unique by type")
        if len({item.stable_key for item in self.requires}) != len(self.requires):
            raise ValueError("Plugin dependencies must not be duplicated")
        if len({item.action.key for item in self.permissions}) != len(self.permissions):
            raise ValueError("Plugin permissions must not duplicate actions")

    @property
    def ref(self) -> PluginRef:
        return PluginRef(self.name, self.version)

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "name": self.name,
            "version": str(self.version),
            "core_api": str(self.core_api),
            "entrypoint": self.entrypoint,
            "provides": [item.to_data() for item in self.provides],
            "requires": [item.to_data() for item in self.requires],
            "permissions": [item.to_data() for item in self.permissions],
            "lifecycle": [item.value for item in sorted(self.lifecycle, key=str)],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PluginManifest:
        _require_fields(
            data,
            {
                "contract_version",
                "name",
                "version",
                "core_api",
                "entrypoint",
                "provides",
                "requires",
                "permissions",
                "lifecycle",
            },
            "Plugin manifest",
        )
        return cls(
            contract_version=_string(
                data["contract_version"], "manifest contract version"
            ),
            name=_string(data["name"], "Plugin name"),
            version=SemVer.parse(_string(data["version"], "Plugin version")),
            core_api=VersionRange.parse(_string(data["core_api"], "Core API range")),
            entrypoint=_string(data["entrypoint"], "Plugin entrypoint"),
            provides=tuple(
                CapabilityDeclaration.from_data(
                    as_object(item, "capability declaration")
                )
                for item in as_array(data["provides"], "provided capabilities")
            ),
            requires=tuple(
                PluginDependency.from_data(as_object(item, "Plugin dependency"))
                for item in as_array(data["requires"], "Plugin dependencies")
            ),
            permissions=tuple(
                PluginPermission.from_data(as_object(item, "Plugin permission"))
                for item in as_array(data["permissions"], "Plugin permissions")
            ),
            lifecycle=frozenset(
                PluginHookName(_string(item, "lifecycle hook"))
                for item in as_array(data["lifecycle"], "lifecycle hooks")
            ),
        )


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_item, right_item in zip(left, right, strict=False):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def _scope_pattern_parts(value: str) -> tuple[str, str, str]:
    _require_trimmed(value, "permission Scope pattern")
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise ValueError(
            "permission Scope pattern must be namespace:kind:id or namespace:kind:*"
        )
    return parts[0], parts[1], parts[2]


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    _require_trimmed(value, field_name)
    return value


def _require_trimmed(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")


def _require_namespaced_id(value: str, field_name: str) -> None:
    if len(value) > 127 or _NAMESPACED_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase namespaced identifier")


def _require_fields(data: dict[str, object], expected: set[str], subject: str) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(f"{subject} fields are invalid: {'; '.join(details)}")
