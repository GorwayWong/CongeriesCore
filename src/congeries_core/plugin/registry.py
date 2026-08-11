"""Atomic ownership-aware Plugin capability publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType

from congeries_core.runtime.errors import ErrorCategory
from congeries_core.runtime.json_types import as_array, as_int, as_object

from .errors import plugin_error
from .loader import LoadedCapability
from .model import CapabilityDeclaration, PluginRef

type CapabilityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class CapabilityRegistration:
    registration_id: str
    owner: PluginRef
    declaration: CapabilityDeclaration
    implementation: object


@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    version: int
    registrations: Mapping[CapabilityKey, CapabilityRegistration]

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("registry version must be non-negative")
        object.__setattr__(
            self,
            "registrations",
            MappingProxyType(dict(self.registrations)),
        )


@dataclass(frozen=True, slots=True)
class CapabilityRegistrationPlan:
    owner: PluginRef
    capabilities: tuple[LoadedCapability, ...]

    def __post_init__(self) -> None:
        keys = [item.declaration.key for item in self.capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError("registration plan contains duplicate capabilities")


@dataclass(frozen=True, slots=True)
class RegistrationReceipt:
    registration_id: str
    owner: PluginRef
    registry_version: int
    capability_keys: tuple[CapabilityKey, ...]

    def __post_init__(self) -> None:
        if not self.registration_id or self.registry_version < 1:
            raise ValueError("registration identity and committed version are required")
        if tuple(sorted(self.capability_keys)) != self.capability_keys:
            raise ValueError("receipt capability keys must use stable order")

    def to_data(self) -> dict[str, object]:
        return {
            "registration_id": self.registration_id,
            "owner": self.owner.to_data(),
            "registry_version": self.registry_version,
            "capability_keys": [list(key) for key in self.capability_keys],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> RegistrationReceipt:
        if set(data) != {
            "registration_id",
            "owner",
            "registry_version",
            "capability_keys",
        }:
            raise ValueError("registration receipt fields are invalid")
        keys: list[CapabilityKey] = []
        for item in as_array(data["capability_keys"], "receipt capability keys"):
            values = as_array(item, "receipt capability key")
            if len(values) != 3 or not all(isinstance(value, str) for value in values):
                raise ValueError("receipt capability key must contain three strings")
            keys.append((str(values[0]), str(values[1]), str(values[2])))
        return cls(
            registration_id=str(data["registration_id"]),
            owner=PluginRef.from_data(as_object(data["owner"], "receipt owner")),
            registry_version=as_int(data["registry_version"], "registry version"),
            capability_keys=tuple(keys),
        )


class CapabilityRegistry:
    """Readers observe only complete immutable committed snapshots."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = CapabilityRegistrySnapshot(0, {})
        self._retired_receipts: set[str] = set()

    def snapshot(self) -> CapabilityRegistrySnapshot:
        with self._lock:
            return self._snapshot

    def commit(
        self,
        plan: CapabilityRegistrationPlan,
        *,
        expected_version: int,
    ) -> RegistrationReceipt:
        with self._lock:
            # Validate the complete plan against one version before publishing
            # anything. The final snapshot assignment is the only visibility
            # point, so readers see either the old set or the complete new set.
            current = self._snapshot
            if current.version != expected_version:
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "registration_conflict",
                    "Capability registry version is stale",
                    plugin=plan.owner.name,
                )
            registrations = dict(current.registrations)
            ordered = sorted(plan.capabilities, key=lambda item: item.declaration.key)
            for capability in ordered:
                key = capability.declaration.key
                if key in registrations:
                    raise plugin_error(
                        ErrorCategory.CONFLICT,
                        "registration_conflict",
                        "Capability is already registered",
                        plugin=plan.owner.name,
                        capability=capability.declaration.capability_id,
                    )
            version = current.version + 1
            keys = tuple(item.declaration.key for item in ordered)
            registration_id = _registration_id(plan.owner, keys, version)
            for capability in ordered:
                registrations[capability.declaration.key] = CapabilityRegistration(
                    registration_id,
                    plan.owner,
                    capability.declaration,
                    capability.implementation,
                )
            receipt = RegistrationReceipt(
                registration_id,
                plan.owner,
                version,
                keys,
            )
            self._snapshot = CapabilityRegistrySnapshot(version, registrations)
            return receipt

    def unregister(self, receipt: RegistrationReceipt) -> CapabilityRegistrySnapshot:
        with self._lock:
            # A retired receipt is a harmless retry. For a live receipt, every key
            # must still belong to this exact registration generation before any
            # key is removed; a stale receipt cannot delete its replacement.
            if receipt.registration_id in self._retired_receipts:
                return self._snapshot
            registrations = dict(self._snapshot.registrations)
            for key in receipt.capability_keys:
                existing = registrations.get(key)
                if (
                    existing is None
                    or existing.owner != receipt.owner
                    or existing.registration_id != receipt.registration_id
                ):
                    raise plugin_error(
                        ErrorCategory.CONFLICT,
                        "registration_identity_conflict",
                        "Registration receipt does not own the committed capability",
                        plugin=receipt.owner.name,
                        capability=key[1],
                    )
            for key in receipt.capability_keys:
                del registrations[key]
            self._retired_receipts.add(receipt.registration_id)
            self._snapshot = CapabilityRegistrySnapshot(
                self._snapshot.version + 1,
                registrations,
            )
            return self._snapshot

    def rollback(self, receipt: RegistrationReceipt) -> CapabilityRegistrySnapshot:
        return self.unregister(receipt)

    def get(self, key: CapabilityKey) -> CapabilityRegistration:
        registration = self.snapshot().registrations.get(key)
        if registration is None:
            raise plugin_error(
                ErrorCategory.UNAVAILABLE,
                "capability_not_registered",
                "Capability is not registered",
                retryable=True,
                capability=key[1],
            )
        return registration


def _registration_id(
    owner: PluginRef, keys: tuple[CapabilityKey, ...], registry_version: int
) -> str:
    encoded = json.dumps(
        {
            "owner": owner.to_data(),
            "capabilities": keys,
            "registry_version": registry_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "plugin-registration:" + hashlib.sha256(encoded).hexdigest()
