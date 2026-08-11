"""Runtime Event payload schema registry."""

from __future__ import annotations

from collections.abc import Callable, Collection

from congeries_core.runtime.errors import ErrorCategory, core_error

from .model import CoreEventType, RuntimeEvent

PayloadValidator = Callable[[RuntimeEvent], None]


class EventSchemaRegistry:
    def __init__(self) -> None:
        self._validators: dict[tuple[str, str], PayloadValidator] = {}

    def register(
        self, event_type: str, schema_version: str, validator: PayloadValidator
    ) -> None:
        key = event_type, schema_version
        if key in self._validators:
            raise core_error(
                ErrorCategory.CONFLICT,
                "event_schema_already_registered",
                "event schema is already registered",
            )
        self._validators[key] = validator

    def validate(self, event: RuntimeEvent) -> None:
        validator = self._validators.get((event.event_type, event.schema_version))
        if validator is None:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "unsupported_event_schema",
                "event type or schema version is not registered",
            )
        validator(event)


def require_payload_fields(*required: str) -> PayloadValidator:
    names = frozenset(required)

    def validate(event: RuntimeEvent) -> None:
        missing = names.difference(event.payload.fields)
        if missing:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "invalid_event_payload",
                "event payload is missing required fields: "
                + ", ".join(sorted(missing)),
            )

    return validate


def core_schema_registry() -> EventSchemaRegistry:
    registry = EventSchemaRegistry()
    requirements: dict[CoreEventType, Collection[str]] = {
        CoreEventType.RUN_STATE_CHANGED: (
            "previous_status",
            "new_status",
            "attempt",
            "reason",
            "state_version",
        ),
        CoreEventType.CONTEXT_RESOLUTION_STARTED: ("key_count", "strategy"),
        CoreEventType.CONTEXT_PROVIDER_SELECTED: ("provider_id", "key_count"),
        CoreEventType.CONTEXT_RESOLUTION_COMPLETED: (
            "provider_count",
            "entry_count",
            "completeness",
            "byte_count",
            "latency_ms",
            "outcome",
        ),
        CoreEventType.CONTEXT_RESOLUTION_FAILED: (
            "error_code",
            "category",
            "latency_ms",
            "outcome",
        ),
        CoreEventType.MODEL_INVOCATION_STARTED: (
            "operation",
            "provider_id",
            "model_id",
        ),
        CoreEventType.MODEL_INVOCATION_COMPLETED: (
            "operation",
            "provider_id",
            "model_id",
            "finish_reason",
            "input_units",
            "output_units",
            "latency_ms",
            "outcome",
        ),
        CoreEventType.MODEL_INVOCATION_FAILED: (
            "operation",
            "category",
            "error_code",
            "latency_ms",
            "outcome",
        ),
        CoreEventType.MEMORY_OPERATION_STARTED: ("operation", "provider_id"),
        CoreEventType.MEMORY_OPERATION_COMPLETED: (
            "operation",
            "provider_id",
            "record_count",
            "affected_count",
            "outcome",
            "latency_ms",
        ),
        CoreEventType.MEMORY_OPERATION_FAILED: (
            "operation",
            "provider_id",
            "error_code",
            "category",
            "outcome",
            "latency_ms",
        ),
        CoreEventType.AUTHORIZATION_DENIED: (
            "principal",
            "action",
            "resource",
            "reason_code",
            "policy_effect",
        ),
        CoreEventType.AUTHORIZATION_CROSS_SCOPE_GRANTED: (
            "principal",
            "action",
            "resource",
            "source_scope",
            "destination_scope",
            "policy_version",
        ),
        CoreEventType.CHECKPOINT_SAVED: (
            "checkpoint_ref",
            "sequence",
            "graph_version",
            "outcome",
        ),
        CoreEventType.CHECKPOINT_FAILED: (
            "checkpoint_ref",
            "sequence",
            "error_code",
            "category",
            "outcome",
        ),
        CoreEventType.CHECKPOINT_MIGRATION_AUTHORIZED: (
            "source_checkpoint_ref",
            "migrated_checkpoint_ref",
            "source_graph_version",
            "target_graph_version",
        ),
        CoreEventType.CHECKPOINT_FALLBACK_AUTHORIZED: (
            "source_checkpoint_ref",
            "fallback_checkpoint_ref",
            "fallback_sequence",
        ),
        CoreEventType.APPROVAL_REQUESTED: (
            "approval_id",
            "node_id",
            "correlation_id",
            "outcome",
        ),
        CoreEventType.APPROVAL_DECIDED: (
            "approval_id",
            "node_id",
            "correlation_id",
            "actor",
            "outcome",
        ),
    }
    for event_type in CoreEventType:
        registry.register(
            event_type.value,
            "1",
            require_payload_fields(*requirements.get(event_type, ())),
        )
    return registry
