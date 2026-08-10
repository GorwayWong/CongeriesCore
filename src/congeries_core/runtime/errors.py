"""Structured errors shared across runtime capability boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .json_types import as_object

type JsonScalar = str | int | float | bool | None


class ErrorCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    VERSION_MISMATCH = "version_mismatch"
    PARTIAL_RESULT = "partial_result"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PROTOCOL_FAILURE = "protocol_failure"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    category: ErrorCategory
    code: str
    message: str
    retryable: bool = False
    cause_id: str | None = None
    metadata: dict[str, JsonScalar] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("error code and message are required")

    def to_data(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "cause_id": self.cause_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ErrorDetail:
        raw_metadata = as_object(data.get("metadata", {}), "error metadata")
        metadata: dict[str, JsonScalar] = {}
        for key, value in raw_metadata.items():
            if not (value is None or isinstance(value, str | int | float | bool)):
                raise ValueError("error metadata must contain JSON scalar values")
            metadata[key] = value
        return cls(
            category=ErrorCategory(str(data["category"])),
            code=str(data["code"]),
            message=str(data["message"]),
            retryable=bool(data.get("retryable", False)),
            cause_id=(str(data["cause_id"]) if data.get("cause_id") else None),
            metadata=metadata,
        )


class CoreError(Exception):
    """Base exception carrying a serializable public error detail."""

    def __init__(self, detail: ErrorDetail) -> None:
        self.detail = detail
        super().__init__(detail.message)


def core_error(
    category: ErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    cause_id: str | None = None,
) -> CoreError:
    return CoreError(
        ErrorDetail(
            category=category,
            code=code,
            message=message,
            retryable=retryable,
            cause_id=cause_id,
        )
    )
