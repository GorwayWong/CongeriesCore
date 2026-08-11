"""Versioned Runtime Event envelope and delivery contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from types import MappingProxyType

from congeries_core.runtime.control import require_utc
from congeries_core.runtime.ids import (
    AcknowledgementId,
    CausationId,
    CorrelationId,
    EventId,
    RunId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.scope import ScopeRef


class DeliveryClass(StrEnum):
    OBSERVABILITY = "observability"
    AUDIT = "audit"


class Sensitivity(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    SENSITIVE = 2
    RESTRICTED = 3

    @property
    def wire_name(self) -> str:
        return self.name.lower()

    @classmethod
    def from_wire(cls, value: str) -> Sensitivity:
        return cls[value.upper()]


class CoreEventType(StrEnum):
    RUN_STATE_CHANGED = "core.run.state_changed"
    CONTEXT_RESOLUTION_STARTED = "core.context.resolution_started"
    CONTEXT_PROVIDER_SELECTED = "core.context.provider_selected"
    CONTEXT_RESOLUTION_COMPLETED = "core.context.resolution_completed"
    CONTEXT_RESOLUTION_FAILED = "core.context.resolution_failed"
    MODEL_INVOCATION_STARTED = "core.model.invocation_started"
    MODEL_INVOCATION_COMPLETED = "core.model.invocation_completed"
    MODEL_INVOCATION_FAILED = "core.model.invocation_failed"
    MEMORY_OPERATION_STARTED = "core.memory.operation_started"
    MEMORY_OPERATION_COMPLETED = "core.memory.operation_completed"
    MEMORY_OPERATION_FAILED = "core.memory.operation_failed"
    SKILL_RESOURCE_LOAD_STARTED = "core.skill.resource_load_started"
    SKILL_RESOURCE_LOAD_COMPLETED = "core.skill.resource_load_completed"
    SKILL_RESOURCE_LOAD_FAILED = "core.skill.resource_load_failed"
    TOOL_INVOCATION_STARTED = "core.tool.invocation_started"
    TOOL_INVOCATION_COMPLETED = "core.tool.invocation_completed"
    TOOL_INVOCATION_FAILED = "core.tool.invocation_failed"
    APPROVAL_REQUESTED = "core.approval.requested"
    APPROVAL_DECIDED = "core.approval.decided"
    AUTHORIZATION_DENIED = "core.authorization.denied"
    AUTHORIZATION_CROSS_SCOPE_GRANTED = "core.authorization.cross_scope_granted"
    CHECKPOINT_SAVED = "core.checkpoint.saved"
    CHECKPOINT_FAILED = "core.checkpoint.failed"
    CHECKPOINT_MIGRATION_AUTHORIZED = "core.checkpoint.migration_authorized"
    CHECKPOINT_FALLBACK_AUTHORIZED = "core.checkpoint.fallback_authorized"
    EVALUATION_STARTED = "core.evaluation.started"
    EVALUATION_VERDICT_RECORDED = "core.evaluation.verdict_recorded"
    ARTIFACT_CREATED = "core.artifact.created"
    PLUGIN_LIFECYCLE_TRANSITION_REQUESTED = "core.plugin.lifecycle_transition_requested"
    PLUGIN_LIFECYCLE_CHANGED = "core.plugin.lifecycle_changed"
    PLUGIN_LIFECYCLE_FAILED = "core.plugin.lifecycle_failed"


@dataclass(frozen=True, slots=True)
class PayloadField:
    value: JsonValue
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    def to_data(self) -> dict[str, object]:
        return {
            "value": self.value,
            "sensitivity": self.sensitivity.wire_name,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PayloadField:
        return cls(
            value=as_json_value(data.get("value"), "payload field value"),
            sensitivity=Sensitivity.from_wire(str(data["sensitivity"])),
        )


@dataclass(frozen=True, slots=True)
class ClassifiedPayload:
    fields: Mapping[str, PayloadField] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        if any(not key or key != key.strip() for key in self.fields):
            raise ValueError("payload field names must be non-empty and trimmed")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def visible_data(self) -> dict[str, JsonValue]:
        return {key: item.value for key, item in self.fields.items()}

    def to_data(self) -> dict[str, object]:
        return {key: item.to_data() for key, item in self.fields.items()}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ClassifiedPayload:
        fields: dict[str, PayloadField] = {}
        for key, value in data.items():
            fields[key] = PayloadField.from_data(
                as_object(value, "classified payload field")
            )
        return cls(fields)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: EventId
    event_type: str
    schema_version: str
    occurred_at: datetime
    run_id: RunId
    root_run_id: RunId
    parent_run_id: RunId | None
    sequence: int
    scope: ScopeRef
    correlation_id: CorrelationId
    causation_id: CausationId | None
    sensitivity: Sensitivity
    delivery_class: DeliveryClass
    payload: ClassifiedPayload

    def __post_init__(self) -> None:
        if not self.event_type or "." not in self.event_type:
            raise ValueError("event_type must be namespaced")
        if not self.schema_version:
            raise ValueError("schema_version is required")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        object.__setattr__(
            self, "occurred_at", require_utc(self.occurred_at, "occurred_at")
        )

    @property
    def payload_digest(self) -> str:
        # Acknowledgement proves the logical event, not one envelope allocation.
        # Excluding event_id, occurred_at, and sequence keeps the digest stable
        # when the same deterministic audit event is reconstructed after restart.
        encoded = json.dumps(
            {
                "event_type": self.event_type,
                "schema_version": self.schema_version,
                "run_id": self.run_id.value,
                "root_run_id": self.root_run_id.value,
                "parent_run_id": (
                    self.parent_run_id.value if self.parent_run_id else None
                ),
                "scope": self.scope.to_data(),
                "correlation_id": self.correlation_id.value,
                "causation_id": (
                    self.causation_id.value if self.causation_id else None
                ),
                "sensitivity": self.sensitivity.wire_name,
                "delivery_class": self.delivery_class.value,
                "payload": self.payload.to_data(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_data(self) -> dict[str, object]:
        return {
            "event_id": self.event_id.value,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at.isoformat(),
            "run_id": self.run_id.value,
            "root_run_id": self.root_run_id.value,
            "parent_run_id": self.parent_run_id.value if self.parent_run_id else None,
            "sequence": self.sequence,
            "scope": self.scope.to_data(),
            "correlation_id": self.correlation_id.value,
            "causation_id": self.causation_id.value if self.causation_id else None,
            "sensitivity": self.sensitivity.wire_name,
            "delivery_class": self.delivery_class.value,
            "payload": self.payload.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> RuntimeEvent:
        raw_scope = as_object(data["scope"], "event Scope")
        raw_payload = as_object(data["payload"], "event payload")
        return cls(
            event_id=EventId(str(data["event_id"])),
            event_type=str(data["event_type"]),
            schema_version=str(data["schema_version"]),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
            run_id=RunId(str(data["run_id"])),
            root_run_id=RunId(str(data["root_run_id"])),
            parent_run_id=(
                RunId(str(data["parent_run_id"])) if data.get("parent_run_id") else None
            ),
            sequence=as_int(data["sequence"], "event sequence"),
            scope=ScopeRef.from_data(raw_scope),
            correlation_id=CorrelationId(str(data["correlation_id"])),
            causation_id=(
                CausationId(str(data["causation_id"]))
                if data.get("causation_id")
                else None
            ),
            sensitivity=Sensitivity.from_wire(str(data["sensitivity"])),
            delivery_class=DeliveryClass(str(data["delivery_class"])),
            payload=ClassifiedPayload.from_data(raw_payload),
        )


@dataclass(frozen=True, slots=True)
class EventAcknowledgement:
    acknowledgement_id: AcknowledgementId
    event_id: EventId
    sink_id: str
    payload_digest: str
    acknowledged_at: datetime

    def __post_init__(self) -> None:
        if not self.sink_id or not self.payload_digest:
            raise ValueError("sink identity and payload digest are required")
        object.__setattr__(
            self,
            "acknowledged_at",
            require_utc(self.acknowledged_at, "acknowledged_at"),
        )


@dataclass(frozen=True, slots=True)
class EventSinkCapabilities:
    delivery_classes: frozenset[DeliveryClass]
    schema_versions: frozenset[tuple[str, str]]
    maximum_sensitivity: Sensitivity
    acknowledgement: bool
    batch: bool = False
    retry_safe: bool = True

    def supports(self, event: RuntimeEvent) -> bool:
        schema_supported = (
            event.event_type,
            event.schema_version,
        ) in self.schema_versions or ("*", "*") in self.schema_versions
        return (
            event.delivery_class in self.delivery_classes
            and schema_supported
            and event.sensitivity <= self.maximum_sensitivity
            and (
                event.delivery_class is not DeliveryClass.AUDIT or self.acknowledgement
            )
        )
