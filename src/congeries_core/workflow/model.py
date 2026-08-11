"""Immutable public contracts for the minimal v0.2 Workflow runtime."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from congeries_core.checkpoint import (
    ApprovalOutcome,
    ApprovalRequest,
    CheckpointReference,
)
from congeries_core.policy.authorization import ActionRef, ResourceRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import require_utc
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import (
    AgentId,
    CheckpointRef,
    DefinitionId,
    ModelBindingRef,
    NodeId,
    RunId,
    WorkflowId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.run import Run, RunStatus, WorkflowRun
from congeries_core.runtime.schema import SchemaRef
from congeries_core.runtime.scope import ScopeRef


class WorkflowNodeType(StrEnum):
    AGENT = "agent"
    SKILL = "skill"
    TOOL = "tool"
    CONTEXT = "context"
    APPROVAL = "approval"
    EVALUATION = "evaluation"


class WorkflowInputSource(StrEnum):
    WORKFLOW_INPUT = "workflow_input"
    NODE_OUTPUT = "node_output"


class WorkflowFailureMode(StrEnum):
    FAIL_FAST = "fail_fast"


@dataclass(frozen=True, slots=True)
class WorkflowPermission:
    action: ActionRef
    resource: ResourceRef

    def to_data(self) -> dict[str, object]:
        return {"action": self.action.to_data(), "resource": self.resource.to_data()}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowPermission:
        _require_keys(data, {"action", "resource"}, "Workflow permission")
        return cls(
            action=ActionRef.from_data(as_object(data["action"], "permission action")),
            resource=ResourceRef.from_data(
                as_object(data["resource"], "permission resource")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowInputBinding:
    source: WorkflowInputSource
    source_node_id: NodeId | None = None

    def __post_init__(self) -> None:
        if (self.source is WorkflowInputSource.NODE_OUTPUT) != (
            self.source_node_id is not None
        ):
            raise ValueError("node output binding requires exactly one source node")

    def to_data(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "source_node_id": (
                self.source_node_id.value if self.source_node_id is not None else None
            ),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowInputBinding:
        _require_keys(data, {"source", "source_node_id"}, "Workflow input binding")
        raw_source_node = data["source_node_id"]
        return cls(
            source=WorkflowInputSource(_as_string(data["source"], "binding source")),
            source_node_id=(
                NodeId(_as_string(raw_source_node, "binding source node"))
                if raw_source_node is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowDependency:
    source_node_id: NodeId
    target_node_id: NodeId
    carries_output: bool = False

    def __post_init__(self) -> None:
        if self.source_node_id == self.target_node_id:
            raise ValueError("Workflow dependency cannot be a self edge")

    def to_data(self) -> dict[str, object]:
        return {
            "source_node_id": self.source_node_id.value,
            "target_node_id": self.target_node_id.value,
            "carries_output": self.carries_output,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowDependency:
        _require_keys(
            data,
            {"source_node_id", "target_node_id", "carries_output"},
            "Workflow dependency",
        )
        return cls(
            source_node_id=NodeId(
                _as_string(data["source_node_id"], "dependency source node")
            ),
            target_node_id=NodeId(
                _as_string(data["target_node_id"], "dependency target node")
            ),
            carries_output=_as_bool(
                data["carries_output"], "dependency carries_output"
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowOutputBinding:
    source_node_id: NodeId
    required: bool = True

    def to_data(self) -> dict[str, object]:
        return {"source_node_id": self.source_node_id.value, "required": self.required}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowOutputBinding:
        _require_keys(data, {"source_node_id", "required"}, "Workflow output binding")
        return cls(
            source_node_id=NodeId(
                _as_string(data["source_node_id"], "output source node")
            ),
            required=_as_bool(data["required"], "output required"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    max_concurrency: int = 1
    failure_mode: WorkflowFailureMode = WorkflowFailureMode.FAIL_FAST
    compensation_enabled: bool = False
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or self.max_attempts < 1:
            raise ValueError("Workflow policy limits must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "max_concurrency": self.max_concurrency,
            "failure_mode": self.failure_mode.value,
            "compensation_enabled": self.compensation_enabled,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ExecutionPolicy:
        _require_keys(
            data,
            {
                "max_concurrency",
                "failure_mode",
                "compensation_enabled",
                "max_attempts",
            },
            "Execution policy",
        )
        return cls(
            max_concurrency=as_int(data["max_concurrency"], "max concurrency"),
            failure_mode=WorkflowFailureMode(
                _as_string(data["failure_mode"], "failure mode")
            ),
            compensation_enabled=_as_bool(
                data["compensation_enabled"], "compensation enabled"
            ),
            max_attempts=as_int(data["max_attempts"], "max attempts"),
        )


@dataclass(frozen=True, slots=True)
class AgentNodeConfig:
    agent_id: AgentId
    definition_id: DefinitionId
    model_binding_ref: ModelBindingRef

    def to_data(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id.value,
            "definition_id": self.definition_id.value,
            "model_binding_ref": self.model_binding_ref.value,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> AgentNodeConfig:
        _require_keys(
            data,
            {"agent_id", "definition_id", "model_binding_ref"},
            "AgentNode config",
        )
        return cls(
            agent_id=AgentId(_as_string(data["agent_id"], "Agent id")),
            definition_id=DefinitionId(
                _as_string(data["definition_id"], "Agent definition id")
            ),
            model_binding_ref=ModelBindingRef(
                _as_string(data["model_binding_ref"], "model binding ref")
            ),
        )


@dataclass(frozen=True, slots=True)
class ApprovalNodeConfig:
    prompt_ref: CheckpointReference
    allowed_outcomes: tuple[ApprovalOutcome, ...] = (
        ApprovalOutcome.APPROVED,
        ApprovalOutcome.REJECTED,
        ApprovalOutcome.CANCELLED,
    )
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.allowed_outcomes:
            raise ValueError("ApprovalNode requires at least one allowed outcome")
        if len(set(self.allowed_outcomes)) != len(self.allowed_outcomes):
            raise ValueError("ApprovalNode outcomes must be unique")
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", require_utc(self.expires_at, "approval expires_at")
            )

    def to_data(self) -> dict[str, object]:
        return {
            "prompt_ref": self.prompt_ref.to_data(),
            "allowed_outcomes": [item.value for item in self.allowed_outcomes],
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ApprovalNodeConfig:
        _require_keys(
            data,
            {"prompt_ref", "allowed_outcomes", "expires_at"},
            "ApprovalNode config",
        )
        raw_expiry = data["expires_at"]
        return cls(
            prompt_ref=CheckpointReference.from_data(
                as_object(data["prompt_ref"], "approval prompt reference")
            ),
            allowed_outcomes=tuple(
                ApprovalOutcome(_as_string(item, "approval outcome"))
                for item in as_array(data["allowed_outcomes"], "approval outcomes")
            ),
            expires_at=(
                datetime.fromisoformat(_as_string(raw_expiry, "approval expires_at"))
                if raw_expiry is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class UnsupportedNodeConfig:
    data: JsonValue

    def __post_init__(self) -> None:
        normalized = as_json_value(self.data, "unsupported node config")
        if not isinstance(normalized, dict):
            raise ValueError("unsupported node config must be an object")
        object.__setattr__(self, "data", normalized)

    def to_data(self) -> JsonValue:
        return self.data


type WorkflowNodeConfig = AgentNodeConfig | ApprovalNodeConfig | UnsupportedNodeConfig


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    node_id: NodeId
    node_type: str
    contract_version: str
    input_schema: SchemaRef | None
    input_bindings: tuple[WorkflowInputBinding, ...]
    output_schema: SchemaRef | None
    scope: ScopeRef
    permissions: tuple[WorkflowPermission, ...]
    timeout_seconds: int | None
    retry_limit: int
    side_effecting: bool
    idempotency_required: bool
    checkpoint: bool
    config: WorkflowNodeConfig

    def __post_init__(self) -> None:
        for name, value in (
            ("node type", self.node_type),
            ("node contract version", self.contract_version),
        ):
            if not value or value != value.strip():
                raise ValueError(f"Workflow {name} must be non-empty and trimmed")
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("node timeout must be positive")
        if self.retry_limit < 0:
            raise ValueError("node retry limit must not be negative")

    def to_data(self) -> dict[str, object]:
        return {
            "node_id": self.node_id.value,
            "node_type": self.node_type,
            "contract_version": self.contract_version,
            "input_schema": self.input_schema.to_data() if self.input_schema else None,
            "input_bindings": [item.to_data() for item in self.input_bindings],
            "output_schema": (
                self.output_schema.to_data() if self.output_schema else None
            ),
            "scope": self.scope.to_data(),
            "permissions": [item.to_data() for item in self.permissions],
            "timeout_seconds": self.timeout_seconds,
            "retry_limit": self.retry_limit,
            "side_effecting": self.side_effecting,
            "idempotency_required": self.idempotency_required,
            "checkpoint": self.checkpoint,
            "config": self.config.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowNode:
        _require_keys(
            data,
            {
                "node_id",
                "node_type",
                "contract_version",
                "input_schema",
                "input_bindings",
                "output_schema",
                "scope",
                "permissions",
                "timeout_seconds",
                "retry_limit",
                "side_effecting",
                "idempotency_required",
                "checkpoint",
                "config",
            },
            "Workflow node",
        )
        node_type = _as_string(data["node_type"], "node type")
        raw_config = as_object(data["config"], "node config")
        if node_type == WorkflowNodeType.AGENT.value:
            config: WorkflowNodeConfig = AgentNodeConfig.from_data(raw_config)
        elif node_type == WorkflowNodeType.APPROVAL.value:
            config = ApprovalNodeConfig.from_data(raw_config)
        else:
            config = UnsupportedNodeConfig(as_json_value(raw_config, "node config"))
        raw_input_schema = data["input_schema"]
        raw_output_schema = data["output_schema"]
        raw_timeout = data["timeout_seconds"]
        return cls(
            node_id=NodeId(_as_string(data["node_id"], "node id")),
            node_type=node_type,
            contract_version=_as_string(
                data["contract_version"], "node contract version"
            ),
            input_schema=(
                SchemaRef.from_data(as_object(raw_input_schema, "node input schema"))
                if raw_input_schema is not None
                else None
            ),
            input_bindings=tuple(
                WorkflowInputBinding.from_data(as_object(item, "node input binding"))
                for item in as_array(data["input_bindings"], "node input bindings")
            ),
            output_schema=(
                SchemaRef.from_data(as_object(raw_output_schema, "node output schema"))
                if raw_output_schema is not None
                else None
            ),
            scope=ScopeRef.from_data(as_object(data["scope"], "node scope")),
            permissions=tuple(
                WorkflowPermission.from_data(as_object(item, "node permission"))
                for item in as_array(data["permissions"], "node permissions")
            ),
            timeout_seconds=(
                as_int(raw_timeout, "node timeout") if raw_timeout is not None else None
            ),
            retry_limit=as_int(data["retry_limit"], "node retry limit"),
            side_effecting=_as_bool(data["side_effecting"], "node side_effecting"),
            idempotency_required=_as_bool(
                data["idempotency_required"], "node idempotency_required"
            ),
            checkpoint=_as_bool(data["checkpoint"], "node checkpoint"),
            config=config,
        )


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: WorkflowId
    definition_id: DefinitionId
    version: str
    input_schema: SchemaRef
    nodes: tuple[WorkflowNode, ...]
    dependencies: tuple[WorkflowDependency, ...]
    output_schema: SchemaRef
    output_binding: WorkflowOutputBinding
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    contract_version: str = "1"

    CONTRACT_VERSION: ClassVar[str] = "1"

    def __post_init__(self) -> None:
        if self.contract_version != self.CONTRACT_VERSION:
            raise ValueError("unsupported Workflow contract version")
        if not self.version or self.version != self.version.strip():
            raise ValueError("Workflow version must be non-empty and trimmed")
        if not self.nodes:
            raise ValueError("Workflow requires at least one node")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "workflow_id": self.workflow_id.value,
            "definition_id": self.definition_id.value,
            "version": self.version,
            "input_schema": self.input_schema.to_data(),
            "nodes": [item.to_data() for item in self.nodes],
            "dependencies": [item.to_data() for item in self.dependencies],
            "output_schema": self.output_schema.to_data(),
            "output_binding": self.output_binding.to_data(),
            "execution_policy": self.execution_policy.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowDefinition:
        _require_keys(
            data,
            {
                "contract_version",
                "workflow_id",
                "definition_id",
                "version",
                "input_schema",
                "nodes",
                "dependencies",
                "output_schema",
                "output_binding",
                "execution_policy",
            },
            "Workflow definition",
        )
        return cls(
            contract_version=_as_string(
                data["contract_version"], "Workflow contract version"
            ),
            workflow_id=WorkflowId(_as_string(data["workflow_id"], "Workflow id")),
            definition_id=DefinitionId(
                _as_string(data["definition_id"], "Workflow definition id")
            ),
            version=_as_string(data["version"], "Workflow version"),
            input_schema=SchemaRef.from_data(
                as_object(data["input_schema"], "Workflow input schema")
            ),
            nodes=tuple(
                WorkflowNode.from_data(as_object(item, "Workflow node"))
                for item in as_array(data["nodes"], "Workflow nodes")
            ),
            dependencies=tuple(
                WorkflowDependency.from_data(as_object(item, "Workflow dependency"))
                for item in as_array(data["dependencies"], "Workflow dependencies")
            ),
            output_schema=SchemaRef.from_data(
                as_object(data["output_schema"], "Workflow output schema")
            ),
            output_binding=WorkflowOutputBinding.from_data(
                as_object(data["output_binding"], "Workflow output binding")
            ),
            execution_policy=ExecutionPolicy.from_data(
                as_object(data["execution_policy"], "Workflow execution policy")
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeOutputReference:
    node_id: NodeId
    schema: SchemaRef
    reference: CheckpointReference

    def to_data(self) -> dict[str, object]:
        return {
            "node_id": self.node_id.value,
            "schema": self.schema.to_data(),
            "reference": self.reference.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> NodeOutputReference:
        _require_keys(data, {"node_id", "schema", "reference"}, "node output ref")
        return cls(
            node_id=NodeId(_as_string(data["node_id"], "output node id")),
            schema=SchemaRef.from_data(as_object(data["schema"], "output schema")),
            reference=CheckpointReference.from_data(
                as_object(data["reference"], "output reference")
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class WorkflowContext:
    run_id: RunId
    input: JsonValue
    runtime: RuntimeCallContext
    output_refs: tuple[NodeOutputReference, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input", as_json_value(self.input, "Workflow input"))
        if self.runtime.run_id != self.run_id:
            raise ValueError("Workflow context Run does not match RuntimeCallContext")
        if len({item.node_id for item in self.output_refs}) != len(self.output_refs):
            raise ValueError("Workflow output references must have unique nodes")

    def to_data(self) -> dict[str, object]:
        return {
            "run_id": self.run_id.value,
            "input": self.input,
            "runtime": self.runtime.to_data(),
            "output_refs": [item.to_data() for item in self.output_refs],
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WorkflowContext) and self.to_data() == other.to_data()

    def __hash__(self) -> int:
        return hash(self.run_id)

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowContext:
        _require_keys(
            data, {"run_id", "input", "runtime", "output_refs"}, "Workflow context"
        )
        return cls(
            run_id=RunId(_as_string(data["run_id"], "Workflow context run id")),
            input=as_json_value(data["input"], "Workflow input"),
            runtime=RuntimeCallContext.from_data(
                as_object(data["runtime"], "Workflow runtime context")
            ),
            output_refs=tuple(
                NodeOutputReference.from_data(as_object(item, "node output ref"))
                for item in as_array(data["output_refs"], "node output refs")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    run: WorkflowRun
    output: JsonValue = None
    error: ErrorDetail | None = None
    output_refs: tuple[NodeOutputReference, ...] = field(default_factory=tuple)
    final_checkpoint_ref: CheckpointRef | None = None

    def __post_init__(self) -> None:
        if not self.run.status.terminal:
            raise ValueError("WorkflowResult requires a terminal WorkflowRun")
        object.__setattr__(
            self, "output", as_json_value(self.output, "Workflow output")
        )
        if self.run.status is RunStatus.SUCCEEDED:
            if self.error is not None:
                raise ValueError("successful WorkflowResult cannot contain an error")
        elif self.error is None:
            raise ValueError("unsuccessful WorkflowResult requires an error")

    def to_data(self) -> dict[str, object]:
        return {
            "run": self.run.to_data(),
            "output": self.output,
            "error": self.error.to_data() if self.error else None,
            "output_refs": [item.to_data() for item in self.output_refs],
            "final_checkpoint_ref": (
                self.final_checkpoint_ref.value if self.final_checkpoint_ref else None
            ),
            "attempt": self.run.attempt,
            "started_at": self.run.started_at.isoformat()
            if self.run.started_at
            else None,
            "ended_at": self.run.ended_at.isoformat() if self.run.ended_at else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowResult:
        _require_keys(
            data,
            {
                "run",
                "output",
                "error",
                "output_refs",
                "final_checkpoint_ref",
                "attempt",
                "started_at",
                "ended_at",
            },
            "Workflow result",
        )
        decoded_run = Run.from_data(as_object(data["run"], "Workflow result run"))
        if not isinstance(decoded_run, WorkflowRun):
            raise ValueError("WorkflowResult requires a WorkflowRun")
        if as_int(data["attempt"], "Workflow result attempt") != decoded_run.attempt:
            raise ValueError("Workflow result attempt does not match Run")
        expected_started = (
            decoded_run.started_at.isoformat() if decoded_run.started_at else None
        )
        expected_ended = (
            decoded_run.ended_at.isoformat() if decoded_run.ended_at else None
        )
        if data["started_at"] != expected_started or data["ended_at"] != expected_ended:
            raise ValueError("Workflow result timing does not match Run")
        raw_error = data["error"]
        raw_checkpoint = data["final_checkpoint_ref"]
        return cls(
            run=decoded_run,
            output=as_json_value(data["output"], "Workflow output"),
            error=(
                ErrorDetail.from_data(as_object(raw_error, "Workflow result error"))
                if raw_error is not None
                else None
            ),
            output_refs=tuple(
                NodeOutputReference.from_data(as_object(item, "node output ref"))
                for item in as_array(data["output_refs"], "node output refs")
            ),
            final_checkpoint_ref=(
                CheckpointRef(_as_string(raw_checkpoint, "final checkpoint ref"))
                if raw_checkpoint is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowSuspension:
    run: WorkflowRun
    checkpoint_ref: CheckpointRef
    approval: ApprovalRequest

    def __post_init__(self) -> None:
        if self.run.status is not RunStatus.WAITING_APPROVAL:
            raise ValueError("Workflow suspension requires WAITING_APPROVAL")
        if self.run.run_id != self.approval.run_id:
            raise ValueError("Workflow suspension approval Run mismatch")

    def to_data(self) -> dict[str, object]:
        return {
            "run": self.run.to_data(),
            "checkpoint_ref": self.checkpoint_ref.value,
            "approval": self.approval.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowSuspension:
        _require_keys(
            data, {"run", "checkpoint_ref", "approval"}, "Workflow suspension"
        )
        decoded_run = Run.from_data(as_object(data["run"], "suspension run"))
        if not isinstance(decoded_run, WorkflowRun):
            raise ValueError("Workflow suspension requires a WorkflowRun")
        return cls(
            run=decoded_run,
            checkpoint_ref=CheckpointRef(
                _as_string(data["checkpoint_ref"], "suspension checkpoint ref")
            ),
            approval=ApprovalRequest.from_data(
                as_object(data["approval"], "suspension approval")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowExecutionOutcome:
    result: WorkflowResult | None = None
    suspension: WorkflowSuspension | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.suspension is None):
            raise ValueError("Workflow outcome requires exactly one payload")

    def to_data(self) -> dict[str, object]:
        return {
            "kind": "terminal" if self.result is not None else "suspended",
            "result": self.result.to_data() if self.result else None,
            "suspension": self.suspension.to_data() if self.suspension else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> WorkflowExecutionOutcome:
        _require_keys(data, {"kind", "result", "suspension"}, "Workflow outcome")
        kind = _as_string(data["kind"], "Workflow outcome kind")
        raw_result = data["result"]
        raw_suspension = data["suspension"]
        if kind == "terminal" and raw_result is not None and raw_suspension is None:
            return cls(
                result=WorkflowResult.from_data(
                    as_object(raw_result, "Workflow outcome result")
                )
            )
        if kind == "suspended" and raw_result is None and raw_suspension is not None:
            return cls(
                suspension=WorkflowSuspension.from_data(
                    as_object(raw_suspension, "Workflow outcome suspension")
                )
            )
        raise ValueError("Workflow outcome discriminator does not match its payload")


def _require_keys(
    data: dict[str, object], expected: Collection[str], field_name: str
) -> None:
    if set(data) != set(expected):
        raise ValueError(f"{field_name} contains unknown or missing fields")


def _as_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _as_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value
