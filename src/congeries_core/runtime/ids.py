"""Validated opaque identifiers used by public runtime contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self
from uuid import uuid4


@dataclass(frozen=True, slots=True, order=True)
class Identifier:
    """Opaque, serializable identity with conservative boundary validation."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value != self.value.strip():
            raise ValueError("identifier must be non-empty and trimmed")
        if len(self.value) > 255:
            raise ValueError("identifier must not exceed 255 characters")
        if any(ord(character) < 32 for character in self.value):
            raise ValueError("identifier must not contain control characters")

    @classmethod
    def new(cls) -> Self:
        return cls(str(uuid4()))

    def __str__(self) -> str:
        return self.value


class RunId(Identifier):
    """Run identity."""


class DefinitionId(Identifier):
    """Agent or Workflow definition identity."""


class WorkspaceId(Identifier):
    """Workspace identity."""


class SessionId(Identifier):
    """Session identity."""


class AgentId(Identifier):
    """Agent identity."""


class WorkflowId(Identifier):
    """Workflow identity."""


class ModelBindingRef(Identifier):
    """Registered model binding reference."""


class ProviderId(Identifier):
    """Registered provider identity."""


class ModelId(Identifier):
    """Provider-neutral model identity."""


class MemoryId(Identifier):
    """Provider-neutral persistent memory identity."""


class CheckpointRef(Identifier):
    """Checkpoint reference."""


class ArtifactId(Identifier):
    """Artifact identity."""


class EventId(Identifier):
    """Runtime Event identity."""


class AcknowledgementId(Identifier):
    """Durable Event acknowledgement identity."""


class CorrelationId(Identifier):
    """Cross-component correlation identity."""


class CausationId(Identifier):
    """Command or Event causation identity."""


class TraceId(Identifier):
    """Trace identity."""


class SpanId(Identifier):
    """Trace span identity."""


class CancellationId(Identifier):
    """Cancellation signal identity."""


class IdempotencyKey(Identifier):
    """Logical side-effect identity preserved across retries."""


class PrincipalId(Identifier):
    """Runtime principal identity."""


class ResourceId(Identifier):
    """Authorized resource identity."""
