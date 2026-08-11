"""Versioned references to Plugin-published runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from congeries_core.policy.authorization import ResourceRef

from .ids import ResourceId


@dataclass(frozen=True, slots=True, order=True)
class CapabilityRef:
    namespace: str
    kind: str
    id: ResourceId
    owning_extension: str
    contract_version: str

    def __post_init__(self) -> None:
        _ = self.resource
        if (
            not self.contract_version
            or self.contract_version != self.contract_version.strip()
        ):
            raise ValueError(
                "capability contract version must be non-empty and trimmed"
            )

    @property
    def key(self) -> tuple[str, str, str]:
        return self.kind, self.id.value, self.contract_version

    @property
    def registration_key(self) -> tuple[str, str, str]:
        """Map the wire contract major to Plugin registry SemVer identity."""

        version = (
            f"{self.contract_version}.0.0"
            if self.contract_version.isdigit()
            else self.contract_version
        )
        return self.kind, self.id.value, version

    @property
    def resource(self) -> ResourceRef:
        return ResourceRef(
            self.namespace,
            self.kind,
            self.id,
            owning_extension=self.owning_extension,
        )

    def to_data(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "kind": self.kind,
            "id": self.id.value,
            "owning_extension": self.owning_extension,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CapabilityRef:
        if set(data) != {
            "namespace",
            "kind",
            "id",
            "owning_extension",
            "contract_version",
        }:
            raise ValueError("capability reference fields are invalid")
        return cls(
            namespace=_string(data, "namespace"),
            kind=_string(data, "kind"),
            id=ResourceId(_string(data, "id")),
            owning_extension=_string(data, "owning_extension"),
            contract_version=_string(data, "contract_version"),
        )

    @classmethod
    def from_resource(
        cls, resource: ResourceRef, *, contract_version: str
    ) -> CapabilityRef:
        if resource.owning_extension is None:
            raise ValueError(
                "versioned capability reference requires an owning extension"
            )
        return cls(
            resource.namespace,
            resource.kind,
            resource.id,
            resource.owning_extension,
            contract_version,
        )


def _string(data: dict[str, object], field_name: str) -> str:
    value = data[field_name]
    if not isinstance(value, str):
        raise ValueError(f"capability reference {field_name} must be a string")
    return value
