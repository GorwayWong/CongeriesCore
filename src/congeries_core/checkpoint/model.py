"""Immutable public Checkpoint, approval, migration, and recovery contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Hashable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol

from congeries_core.policy.authorization import ResourceRef, RuntimePrincipal
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import require_utc
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import (
    ApprovalId,
    CheckpointRef,
    CorrelationId,
    DefinitionId,
    IdempotencyKey,
    NodeId,
    ProviderId,
    RunId,
    WorkflowId,
)
from congeries_core.runtime.json_types import as_array, as_int, as_object
from congeries_core.runtime.run import WorkflowRun
from congeries_core.runtime.scope import ScopeRef

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class NodeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_SCHEDULED = "retry_scheduled"


class SideEffectOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CheckpointReference:
    """A typed and scoped reference that never embeds resource content."""

    resource_type: str
    resource: ResourceRef
    scope: ScopeRef
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.resource_type or self.resource_type != self.resource_type.strip():
            raise ValueError("checkpoint reference type must be non-empty and trimmed")
        if self.version is not None and (
            not self.version or self.version != self.version.strip()
        ):
            raise ValueError(
                "checkpoint reference version must be non-empty and trimmed"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource": self.resource.to_data(),
            "scope": self.scope.to_data(),
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointReference:
        _require_keys(
            data,
            {"resource_type", "resource", "scope", "version"},
            "checkpoint reference",
        )
        return cls(
            resource_type=str(data["resource_type"]),
            resource=ResourceRef.from_data(as_object(data["resource"], "resource")),
            scope=ScopeRef.from_data(as_object(data["scope"], "reference scope")),
            version=str(data["version"]) if data.get("version") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: ApprovalId
    run_id: RunId
    node_id: NodeId
    correlation_id: CorrelationId
    scope: ScopeRef
    allowed_outcomes: tuple[ApprovalOutcome, ...]
    expires_at: datetime | None
    prompt_ref: CheckpointReference

    def __post_init__(self) -> None:
        if not self.allowed_outcomes:
            raise ValueError("approval requires at least one allowed outcome")
        if len(set(self.allowed_outcomes)) != len(self.allowed_outcomes):
            raise ValueError("approval outcomes must be unique")
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", require_utc(self.expires_at, "approval expires_at")
            )
        self.prompt_ref.scope.require_narrower_than(self.scope)

    def to_data(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id.value,
            "run_id": self.run_id.value,
            "node_id": self.node_id.value,
            "correlation_id": self.correlation_id.value,
            "scope": self.scope.to_data(),
            "allowed_outcomes": [item.value for item in self.allowed_outcomes],
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "prompt_ref": self.prompt_ref.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ApprovalRequest:
        outcomes = as_array(data["allowed_outcomes"], "approval outcomes")
        return cls(
            approval_id=ApprovalId(str(data["approval_id"])),
            run_id=RunId(str(data["run_id"])),
            node_id=NodeId(str(data["node_id"])),
            correlation_id=CorrelationId(str(data["correlation_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "approval scope")),
            allowed_outcomes=tuple(ApprovalOutcome(str(item)) for item in outcomes),
            expires_at=(
                datetime.fromisoformat(str(data["expires_at"]))
                if data.get("expires_at")
                else None
            ),
            prompt_ref=CheckpointReference.from_data(
                as_object(data["prompt_ref"], "approval prompt reference")
            ),
        )


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approval_id: ApprovalId
    run_id: RunId
    node_id: NodeId
    correlation_id: CorrelationId
    scope: ScopeRef
    actor: RuntimePrincipal
    outcome: ApprovalOutcome
    decided_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decided_at", require_utc(self.decided_at, "approval decided_at")
        )

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.approval_id.value,
            self.run_id.value,
            self.node_id.value,
            self.correlation_id.value,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id.value,
            "run_id": self.run_id.value,
            "node_id": self.node_id.value,
            "correlation_id": self.correlation_id.value,
            "scope": self.scope.to_data(),
            "actor": self.actor.to_data(),
            "outcome": self.outcome.value,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ApprovalDecision:
        return cls(
            approval_id=ApprovalId(str(data["approval_id"])),
            run_id=RunId(str(data["run_id"])),
            node_id=NodeId(str(data["node_id"])),
            correlation_id=CorrelationId(str(data["correlation_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "decision scope")),
            actor=RuntimePrincipal.from_data(
                as_object(data["actor"], "decision actor")
            ),
            outcome=ApprovalOutcome(str(data["outcome"])),
            decided_at=datetime.fromisoformat(str(data["decided_at"])),
        )


@dataclass(frozen=True, slots=True)
class ApprovalCheckpointState:
    request: ApprovalRequest
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        if self.decision is None:
            return
        request_identity = (
            self.request.approval_id.value,
            self.request.run_id.value,
            self.request.node_id.value,
            self.request.correlation_id.value,
        )
        if self.decision.identity != request_identity:
            raise ValueError("approval decision identity does not match request")
        if self.decision.scope.key != self.request.scope.key:
            raise ValueError("approval decision Scope does not match request")
        if self.decision.outcome not in self.request.allowed_outcomes:
            raise ValueError("approval decision outcome is not allowed")

    def to_data(self) -> dict[str, object]:
        return {
            "request": self.request.to_data(),
            "decision": self.decision.to_data() if self.decision else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ApprovalCheckpointState:
        raw_decision = data.get("decision")
        return cls(
            request=ApprovalRequest.from_data(
                as_object(data["request"], "approval request")
            ),
            decision=(
                ApprovalDecision.from_data(as_object(raw_decision, "approval decision"))
                if raw_decision is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeCheckpointState:
    node_id: NodeId
    outcome: NodeOutcome
    output_ref: CheckpointReference | None = None
    error_ref: CheckpointReference | None = None
    approval_state: ApprovalCheckpointState | None = None

    def __post_init__(self) -> None:
        if self.output_ref is not None and self.error_ref is not None:
            raise ValueError("node state cannot have both output and error references")
        if self.outcome is NodeOutcome.WAITING_APPROVAL and self.approval_state is None:
            raise ValueError("waiting approval node requires approval state")

    def to_data(self) -> dict[str, object]:
        return {
            "node_id": self.node_id.value,
            "outcome": self.outcome.value,
            "output_ref": self.output_ref.to_data() if self.output_ref else None,
            "error_ref": self.error_ref.to_data() if self.error_ref else None,
            "approval_state": (
                self.approval_state.to_data() if self.approval_state else None
            ),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> NodeCheckpointState:
        return cls(
            node_id=NodeId(str(data["node_id"])),
            outcome=NodeOutcome(str(data["outcome"])),
            output_ref=_optional_reference(data.get("output_ref"), "node output"),
            error_ref=_optional_reference(data.get("error_ref"), "node error"),
            approval_state=(
                ApprovalCheckpointState.from_data(
                    as_object(data["approval_state"], "node approval state")
                )
                if data.get("approval_state") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class SideEffectRecord:
    operation_ref: ResourceRef
    idempotency_key: IdempotencyKey
    request_fingerprint: str
    result_ref: CheckpointReference | None
    outcome: SideEffectOutcome

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.request_fingerprint):
            raise ValueError(
                "side-effect request fingerprint must be lowercase SHA-256"
            )
        if self.outcome is SideEffectOutcome.SUCCEEDED and self.result_ref is None:
            raise ValueError(
                "successful side effect requires a durable result reference"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "operation_ref": self.operation_ref.to_data(),
            "idempotency_key": self.idempotency_key.value,
            "request_fingerprint": self.request_fingerprint,
            "result_ref": self.result_ref.to_data() if self.result_ref else None,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SideEffectRecord:
        return cls(
            operation_ref=ResourceRef.from_data(
                as_object(data["operation_ref"], "side-effect operation")
            ),
            idempotency_key=IdempotencyKey(str(data["idempotency_key"])),
            request_fingerprint=str(data["request_fingerprint"]),
            result_ref=_optional_reference(
                data.get("result_ref"), "side-effect result"
            ),
            outcome=SideEffectOutcome(str(data["outcome"])),
        )


@dataclass(frozen=True, slots=True)
class CheckpointIntegrity:
    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise ValueError("checkpoint integrity algorithm must be sha256")
        if not _SHA256_PATTERN.fullmatch(self.digest):
            raise ValueError("checkpoint digest must be lowercase SHA-256")

    def to_data(self) -> dict[str, str]:
        return {"algorithm": self.algorithm, "digest": self.digest}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointIntegrity:
        return cls(algorithm=str(data["algorithm"]), digest=str(data["digest"]))


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkpoint:
    checkpoint_id: CheckpointRef
    run_id: RunId
    workflow_id: WorkflowId
    definition_id: DefinitionId
    graph_version: str
    scope: ScopeRef
    sequence: int
    attempt: int
    previous_checkpoint_ref: CheckpointRef | None
    node_states: tuple[NodeCheckpointState, ...] = field(default_factory=tuple)
    pending_nodes: tuple[NodeId, ...] = field(default_factory=tuple)
    external_refs: tuple[CheckpointReference, ...] = field(default_factory=tuple)
    side_effects: tuple[SideEffectRecord, ...] = field(default_factory=tuple)
    approvals: tuple[ApprovalCheckpointState, ...] = field(default_factory=tuple)
    created_at: datetime
    integrity: CheckpointIntegrity
    contract_version: str = "1"

    CONTRACT_VERSION: ClassVar[str] = "1"

    def __post_init__(self) -> None:
        if self.contract_version != self.CONTRACT_VERSION:
            raise ValueError("unsupported checkpoint contract version")
        if not self.graph_version or self.graph_version != self.graph_version.strip():
            raise ValueError("checkpoint graph version is required")
        if self.sequence < 1 or self.attempt < 1:
            raise ValueError("checkpoint sequence and attempt must be positive")
        if (self.sequence == 1) != (self.previous_checkpoint_ref is None):
            raise ValueError("only the first checkpoint may omit its predecessor")
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, "checkpoint created_at")
        )
        _require_unique((item.node_id.value for item in self.node_states), "node state")
        _require_unique((item.value for item in self.pending_nodes), "pending node")
        stable = {item.node_id for item in self.node_states}
        if stable.intersection(self.pending_nodes):
            raise ValueError("stable and pending node identities must be disjoint")
        _require_unique(
            (
                (item.resource_type, *item.resource.key, *item.scope.key, item.version)
                for item in self.external_refs
            ),
            "external reference",
        )
        _require_unique(
            (item.operation_ref.key for item in self.side_effects),
            "side-effect operation",
        )
        _require_unique(
            (item.idempotency_key.value for item in self.side_effects),
            "side-effect idempotency key",
        )
        _require_unique(
            (item.request.approval_id.value for item in self.approvals),
            "approval",
        )
        approvals = {item.request.approval_id: item for item in self.approvals}
        for state in self.node_states:
            if state.approval_state is not None:
                approval_id = state.approval_state.request.approval_id
                if approvals.get(approval_id) != state.approval_state:
                    raise ValueError(
                        "node approval state must match approvals collection"
                    )
        for reference in self.external_refs:
            reference.scope.require_narrower_than(self.scope)
        for state in self.approvals:
            if state.request.run_id != self.run_id:
                raise ValueError("approval Run must match checkpoint Run")
            state.request.scope.require_narrower_than(self.scope)

    @classmethod
    def create(
        cls,
        *,
        checkpoint_id: CheckpointRef,
        run_id: RunId,
        workflow_id: WorkflowId,
        definition_id: DefinitionId,
        graph_version: str,
        scope: ScopeRef,
        sequence: int,
        attempt: int,
        previous_checkpoint_ref: CheckpointRef | None,
        node_states: tuple[NodeCheckpointState, ...] = (),
        pending_nodes: tuple[NodeId, ...] = (),
        external_refs: tuple[CheckpointReference, ...] = (),
        side_effects: tuple[SideEffectRecord, ...] = (),
        approvals: tuple[ApprovalCheckpointState, ...] = (),
        created_at: datetime,
        contract_version: str = "1",
    ) -> Checkpoint:
        checkpoint = cls(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            workflow_id=workflow_id,
            definition_id=definition_id,
            graph_version=graph_version,
            scope=scope,
            sequence=sequence,
            attempt=attempt,
            previous_checkpoint_ref=previous_checkpoint_ref,
            node_states=node_states,
            pending_nodes=pending_nodes,
            external_refs=external_refs,
            side_effects=side_effects,
            approvals=approvals,
            created_at=created_at,
            contract_version=contract_version,
            integrity=CheckpointIntegrity("sha256", "0" * 64),
        )
        return replace(
            checkpoint,
            integrity=CheckpointIntegrity("sha256", checkpoint.canonical_digest()),
        )

    @property
    def ref(self) -> CheckpointRef:
        return self.checkpoint_id

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self._integrity_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify_integrity(self) -> None:
        if self.canonical_digest() != self.integrity.digest:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_integrity_failure",
                "checkpoint integrity verification failed",
            )

    def _integrity_data(self) -> dict[str, object]:
        data = self.to_data()
        data["integrity"] = {"algorithm": self.integrity.algorithm}
        return data

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "checkpoint_id": self.checkpoint_id.value,
            "run_id": self.run_id.value,
            "workflow_id": self.workflow_id.value,
            "definition_id": self.definition_id.value,
            "graph_version": self.graph_version,
            "scope": self.scope.to_data(),
            "sequence": self.sequence,
            "attempt": self.attempt,
            "previous_checkpoint_ref": (
                self.previous_checkpoint_ref.value
                if self.previous_checkpoint_ref is not None
                else None
            ),
            "node_states": [item.to_data() for item in self.node_states],
            "pending_nodes": [item.value for item in self.pending_nodes],
            "external_refs": [item.to_data() for item in self.external_refs],
            "side_effects": [item.to_data() for item in self.side_effects],
            "approvals": [item.to_data() for item in self.approvals],
            "created_at": self.created_at.isoformat(),
            "integrity": self.integrity.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> Checkpoint:
        _require_keys(
            data,
            {
                "contract_version",
                "checkpoint_id",
                "run_id",
                "workflow_id",
                "definition_id",
                "graph_version",
                "scope",
                "sequence",
                "attempt",
                "previous_checkpoint_ref",
                "node_states",
                "pending_nodes",
                "external_refs",
                "side_effects",
                "approvals",
                "created_at",
                "integrity",
            },
            "checkpoint",
        )
        checkpoint = cls(
            contract_version=str(data["contract_version"]),
            checkpoint_id=CheckpointRef(str(data["checkpoint_id"])),
            run_id=RunId(str(data["run_id"])),
            workflow_id=WorkflowId(str(data["workflow_id"])),
            definition_id=DefinitionId(str(data["definition_id"])),
            graph_version=str(data["graph_version"]),
            scope=ScopeRef.from_data(as_object(data["scope"], "checkpoint scope")),
            sequence=as_int(data["sequence"], "checkpoint sequence"),
            attempt=as_int(data["attempt"], "checkpoint attempt"),
            previous_checkpoint_ref=(
                CheckpointRef(str(data["previous_checkpoint_ref"]))
                if data.get("previous_checkpoint_ref")
                else None
            ),
            node_states=tuple(
                NodeCheckpointState.from_data(as_object(item, "node state"))
                for item in as_array(data["node_states"], "node states")
            ),
            pending_nodes=tuple(
                NodeId(str(item))
                for item in as_array(data["pending_nodes"], "pending nodes")
            ),
            external_refs=tuple(
                CheckpointReference.from_data(as_object(item, "external reference"))
                for item in as_array(data["external_refs"], "external references")
            ),
            side_effects=tuple(
                SideEffectRecord.from_data(as_object(item, "side effect"))
                for item in as_array(data["side_effects"], "side effects")
            ),
            approvals=tuple(
                ApprovalCheckpointState.from_data(as_object(item, "approval state"))
                for item in as_array(data["approvals"], "approvals")
            ),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            integrity=CheckpointIntegrity.from_data(
                as_object(data["integrity"], "checkpoint integrity")
            ),
        )
        checkpoint.verify_integrity()
        return checkpoint


@dataclass(frozen=True, slots=True)
class CheckpointCursor:
    provider_id: ProviderId
    run_id: RunId
    scope: ScopeRef
    graph_version: str | None
    limit: int
    query_fingerprint: str
    next_sequence: int

    def __post_init__(self) -> None:
        if self.limit < 1 or self.next_sequence < 1:
            raise ValueError("cursor limit and next sequence must be positive")
        if not _SHA256_PATTERN.fullmatch(self.query_fingerprint):
            raise ValueError("cursor query fingerprint must be lowercase SHA-256")

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "run_id": self.run_id.value,
            "scope": self.scope.to_data(),
            "graph_version": self.graph_version,
            "limit": self.limit,
            "query_fingerprint": self.query_fingerprint,
            "next_sequence": self.next_sequence,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointCursor:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            run_id=RunId(str(data["run_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "cursor scope")),
            graph_version=(
                str(data["graph_version"])
                if data.get("graph_version") is not None
                else None
            ),
            limit=as_int(data["limit"], "cursor limit"),
            query_fingerprint=str(data["query_fingerprint"]),
            next_sequence=as_int(data["next_sequence"], "cursor next sequence"),
        )


@dataclass(frozen=True, slots=True)
class CheckpointQuery:
    provider_id: ProviderId
    run_id: RunId
    scope: ScopeRef
    graph_version: str | None = None
    limit: int = 50
    cursor: CheckpointCursor | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("checkpoint query limit must be between 1 and 1000")
        if self.graph_version is not None and not self.graph_version:
            raise ValueError("graph version must not be empty")

    @property
    def query_fingerprint(self) -> str:
        data = {
            "provider_id": self.provider_id.value,
            "run_id": self.run_id.value,
            "scope": self.scope.to_data(),
            "graph_version": self.graph_version,
            "limit": self.limit,
        }
        return hashlib.sha256(
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "run_id": self.run_id.value,
            "scope": self.scope.to_data(),
            "graph_version": self.graph_version,
            "limit": self.limit,
            "cursor": self.cursor.to_data() if self.cursor else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointQuery:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            run_id=RunId(str(data["run_id"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "query scope")),
            graph_version=(
                str(data["graph_version"])
                if data.get("graph_version") is not None
                else None
            ),
            limit=as_int(data["limit"], "query limit"),
            cursor=(
                CheckpointCursor.from_data(
                    as_object(data["cursor"], "checkpoint cursor")
                )
                if data.get("cursor") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CheckpointPage:
    items: tuple[Checkpoint, ...]
    next_cursor: CheckpointCursor | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "items": [item.to_data() for item in self.items],
            "next_cursor": self.next_cursor.to_data() if self.next_cursor else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointPage:
        return cls(
            items=tuple(
                Checkpoint.from_data(as_object(item, "checkpoint page item"))
                for item in as_array(data["items"], "checkpoint page items")
            ),
            next_cursor=(
                CheckpointCursor.from_data(
                    as_object(data["next_cursor"], "checkpoint next cursor")
                )
                if data.get("next_cursor") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DeleteCheckpointRequest:
    provider_id: ProviderId
    run_id: RunId
    checkpoint_ref: CheckpointRef
    scope: ScopeRef

    def to_data(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id.value,
            "run_id": self.run_id.value,
            "checkpoint_ref": self.checkpoint_ref.value,
            "scope": self.scope.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> DeleteCheckpointRequest:
        return cls(
            provider_id=ProviderId(str(data["provider_id"])),
            run_id=RunId(str(data["run_id"])),
            checkpoint_ref=CheckpointRef(str(data["checkpoint_ref"])),
            scope=ScopeRef.from_data(as_object(data["scope"], "delete scope")),
        )


@dataclass(frozen=True, slots=True)
class DeleteCheckpointResult:
    checkpoint_ref: CheckpointRef
    deleted: bool

    def to_data(self) -> dict[str, object]:
        return {"checkpoint_ref": self.checkpoint_ref.value, "deleted": self.deleted}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> DeleteCheckpointResult:
        return cls(
            checkpoint_ref=CheckpointRef(str(data["checkpoint_ref"])),
            deleted=bool(data["deleted"]),
        )


@dataclass(frozen=True, slots=True)
class CheckpointMigrationRequest:
    workflow_id: WorkflowId
    source_definition_id: DefinitionId
    source_graph_version: str
    target_definition_id: DefinitionId
    target_graph_version: str

    def __post_init__(self) -> None:
        if not self.source_graph_version or not self.target_graph_version:
            raise ValueError("migration graph versions are required")

    def to_data(self) -> dict[str, str]:
        return {
            "workflow_id": self.workflow_id.value,
            "source_definition_id": self.source_definition_id.value,
            "source_graph_version": self.source_graph_version,
            "target_definition_id": self.target_definition_id.value,
            "target_graph_version": self.target_graph_version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CheckpointMigrationRequest:
        return cls(
            workflow_id=WorkflowId(str(data["workflow_id"])),
            source_definition_id=DefinitionId(str(data["source_definition_id"])),
            source_graph_version=str(data["source_graph_version"]),
            target_definition_id=DefinitionId(str(data["target_definition_id"])),
            target_graph_version=str(data["target_graph_version"]),
        )


class CheckpointMigrator(Protocol):
    async def migrate(
        self,
        checkpoint: Checkpoint,
        request: CheckpointMigrationRequest,
        context: RuntimeCallContext,
    ) -> Checkpoint: ...


class CheckpointRestorer(Protocol):
    async def restore(
        self, checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    allow_corrupt_fallback: bool = False
    fail_on_restore_error: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    run_id: RunId
    expected_version: int
    provider_id: ProviderId
    target_definition_id: DefinitionId
    target_graph_version: str
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    run: WorkflowRun
    checkpoint: Checkpoint
    migrated: bool = False
    fell_back: bool = False


def _optional_reference(value: object, field_name: str) -> CheckpointReference | None:
    if value is None:
        return None
    return CheckpointReference.from_data(as_object(value, field_name))


def _require_unique(values: Iterable[Hashable], field_name: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"{field_name} identities must be unique")


def _require_keys(
    data: dict[str, object], expected: Collection[str], field_name: str
) -> None:
    if set(data) != set(expected):
        raise ValueError(f"{field_name} contains unknown or missing fields")
