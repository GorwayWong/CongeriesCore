"""Explicit field-classification redaction before EventSink dispatch."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from .model import ClassifiedPayload, RuntimeEvent, Sensitivity


class RedactionPolicy(Protocol):
    def redact(
        self, event: RuntimeEvent, maximum_sensitivity: Sensitivity
    ) -> RuntimeEvent: ...


class ExplicitSensitivityRedactionPolicy:
    def redact(
        self, event: RuntimeEvent, maximum_sensitivity: Sensitivity
    ) -> RuntimeEvent:
        visible = {
            key: field
            for key, field in event.payload.fields.items()
            if field.sensitivity <= maximum_sensitivity
        }
        resulting_sensitivity = max(
            (field.sensitivity for field in visible.values()),
            default=Sensitivity.PUBLIC,
        )
        return replace(
            event,
            sensitivity=min(event.sensitivity, resulting_sensitivity),
            payload=ClassifiedPayload(visible),
        )
