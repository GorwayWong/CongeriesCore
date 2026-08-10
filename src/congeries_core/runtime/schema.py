"""Schema references and dependency-injected value validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .errors import CoreError, ErrorCategory, core_error
from .json_types import JsonValue

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


@dataclass(frozen=True, slots=True, order=True)
class SchemaRef:
    namespace: str
    name: str
    version: str

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.namespace):
            raise ValueError("schema namespace is invalid")
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("schema name is invalid")
        if not self.version or self.version != self.version.strip():
            raise ValueError("schema version must be non-empty and trimmed")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.namespace, self.name, self.version

    def to_data(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SchemaRef:
        return cls(str(data["namespace"]), str(data["name"]), str(data["version"]))


class SchemaValidator(Protocol):
    def validate(self, value: JsonValue) -> None: ...


class SchemaRegistry:
    def __init__(self) -> None:
        self._validators: dict[tuple[str, str, str], SchemaValidator] = {}

    def register(self, schema: SchemaRef, validator: SchemaValidator) -> None:
        if schema.key in self._validators:
            raise core_error(
                ErrorCategory.CONFLICT,
                "schema_already_registered",
                "schema is already registered",
            )
        self._validators[schema.key] = validator

    def contains(self, schema: SchemaRef) -> bool:
        return schema.key in self._validators

    def validate(self, schema: SchemaRef, value: JsonValue) -> None:
        validator = self._validators.get(schema.key)
        if validator is None:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "schema_not_registered",
                "schema is not registered",
            )
        try:
            validator.validate(value)
        except CoreError:
            raise
        except (TypeError, ValueError) as error:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "schema_validation_failed",
                "value does not conform to its declared schema",
                cause_id=type(error).__name__,
            ) from error
