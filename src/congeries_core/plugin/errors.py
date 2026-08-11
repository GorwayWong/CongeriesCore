"""Structured Plugin SDK errors."""

from __future__ import annotations

from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    JsonScalar,
)


def plugin_error(
    category: ErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    cause_id: str | None = None,
    plugin: str | None = None,
    capability: str | None = None,
) -> CoreError:
    metadata: dict[str, JsonScalar] = {}
    if plugin is not None:
        metadata["plugin"] = plugin
    if capability is not None:
        metadata["capability"] = capability
    return CoreError(
        ErrorDetail(
            category=category,
            code=code,
            message=message,
            retryable=retryable,
            cause_id=cause_id,
            metadata=metadata,
        )
    )
