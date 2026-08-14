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
from congeries_core.provider.context import (
    ContextBinding,
    ContextCompleteness,
    ContextEntry,
    ContextKey,
    ContextUsage,
    ContextWarning,
    ResolvedContext,
)
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import require_utc
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import (
    AgentId,
    CheckpointRef,
    DefinitionId,
    ModelBindingRef,
    NodeId,
    ProviderId,
    ResourceId,
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
from congeries_core.skill import SkillResource
from congeries_core.tool import ToolCall, ToolDescriptor, ToolResult
from congeries_core.tool.operation import ToolOperationStatus


class WorkflowNodeType(StrEnum):
    AGENT = "agent"
    SKILL = "skill"
    TOOL = "tool"
    CONTEXT = "context"
    APPROVAL = "approval"
    EVALUATION = "evaluation"


CONTEXT_NODE_RESULT_SCHEMA = SchemaRef("core", "context_node_result", "1")
_CONTEXT_NODE_RESULT_CONTRACT_VERSION = "1"
SKILL_NODE_RESULT_SCHEMA = SchemaRef("core", "skill_node_result", "1")
_SKILL_NODE_RESULT_CONTRACT_VERSION = "1"
TOOL_NODE_REQUEST_SCHEMA = SchemaRef("core", "tool_node_request", "1")
TOOL_NODE_RESULT_SCHEMA = SchemaRef("core", "tool_node_result", "1")
_TOOL_NODE_CONTRACT_VERSION = "1"


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
class ContextNodeConfig:
    """Version 1 ContextNode configuration.

    The binding is embedded deliberately. A Workflow definition is therefore a
    complete, reviewable description of which Providers and Schemas the node may
    use; execution never depends on a hidden binding lookup or mutable registry.
    """

    binding: ContextBinding

    def to_data(self) -> dict[str, object]:
        return {"binding": self.binding.to_data()}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextNodeConfig:
        _require_keys(data, {"binding"}, "ContextNode config")
        raw_binding = _strict_context_binding_data(data["binding"])
        return cls(binding=ContextBinding.from_data(raw_binding))


@dataclass(frozen=True, slots=True)
class SkillNodeConfig:
    """Version 1 configuration for one declared, read-only Skill resource."""

    skill: CapabilityRef
    resource_id: ResourceId
    max_bytes: int

    def __post_init__(self) -> None:
        if self.skill.namespace != "core" or self.skill.kind != "skill":
            raise ValueError("SkillNode requires a core Skill reference")
        if self.skill.contract_version != "1":
            raise ValueError("SkillNode requires a Skill v1 reference")
        if isinstance(self.max_bytes, bool) or self.max_bytes < 1:
            raise ValueError("SkillNode max_bytes must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "skill": self.skill.to_data(),
            "resource_id": self.resource_id.value,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillNodeConfig:
        _require_keys(data, {"skill", "resource_id", "max_bytes"}, "SkillNode config")
        return cls(
            skill=CapabilityRef.from_data(as_object(data["skill"], "SkillNode skill")),
            resource_id=ResourceId(
                _as_string(data["resource_id"], "SkillNode resource")
            ),
            max_bytes=as_int(data["max_bytes"], "SkillNode max_bytes"),
        )


@dataclass(frozen=True, slots=True)
class SkillNodeResult:
    contract_version: str
    resource: SkillResource

    def __post_init__(self) -> None:
        if self.contract_version != _SKILL_NODE_RESULT_CONTRACT_VERSION:
            raise ValueError("SkillNode result contract version is unsupported")

    @classmethod
    def from_resource(cls, resource: SkillResource) -> SkillNodeResult:
        return cls(_SKILL_NODE_RESULT_CONTRACT_VERSION, resource)

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "resource": self.resource.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillNodeResult:
        _require_keys(data, {"contract_version", "resource"}, "SkillNode result")
        return cls(
            _as_string(data["contract_version"], "SkillNode result contract version"),
            SkillResource.from_data(as_object(data["resource"], "SkillNode resource")),
        )


class SkillNodeResultSchemaValidator:
    def validate(self, value: JsonValue) -> None:
        SkillNodeResult.from_data(as_object(value, "SkillNode result"))


@dataclass(frozen=True, slots=True)
class ToolNodeConfig:
    tool: CapabilityRef

    def __post_init__(self) -> None:
        if self.tool.namespace != "core" or self.tool.kind != "tool":
            raise ValueError("ToolNode requires a core Tool reference")
        if self.tool.contract_version != "1":
            raise ValueError("ToolNode requires a Tool v1 reference")

    def to_data(self) -> dict[str, object]:
        return {"tool": self.tool.to_data()}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolNodeConfig:
        _require_keys(data, {"tool"}, "ToolNode config")
        return cls(CapabilityRef.from_data(as_object(data["tool"], "ToolNode tool")))


@dataclass(frozen=True, slots=True)
class ToolNodeRequest:
    call: ToolCall
    descriptor: ToolDescriptor
    scope: ScopeRef
    timeout_seconds: int | None
    contract_version: str = _TOOL_NODE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != _TOOL_NODE_CONTRACT_VERSION:
            raise ValueError("ToolNode request contract version is unsupported")
        if self.call.tool != self.descriptor.ref:
            raise ValueError("ToolNode request descriptor does not match call")
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("ToolNode request timeout must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "call": self.call.to_data(),
            "descriptor": self.descriptor.to_data(),
            "scope": self.scope.to_data(),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolNodeRequest:
        _require_keys(
            data,
            {"contract_version", "call", "descriptor", "scope", "timeout_seconds"},
            "ToolNode request",
        )
        timeout = data["timeout_seconds"]
        return cls(
            call=ToolCall.from_data(as_object(data["call"], "ToolNode call")),
            descriptor=ToolDescriptor.from_data(
                as_object(data["descriptor"], "ToolNode descriptor")
            ),
            scope=ScopeRef.from_data(as_object(data["scope"], "ToolNode scope")),
            timeout_seconds=(
                as_int(timeout, "ToolNode timeout") if timeout is not None else None
            ),
            contract_version=_as_string(
                data["contract_version"], "ToolNode request contract version"
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolNodeResult:
    operation_id: ResourceId
    tool: CapabilityRef
    request_fingerprint: str
    status: ToolOperationStatus
    result: ToolResult | None = None
    error: ErrorDetail | None = None
    evidence_ref: CheckpointReference | None = None
    contract_version: str = _TOOL_NODE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != _TOOL_NODE_CONTRACT_VERSION:
            raise ValueError("ToolNode result contract version is unsupported")
        if len(self.request_fingerprint) != 64 or any(
            item not in "0123456789abcdef" for item in self.request_fingerprint
        ):
            raise ValueError("ToolNode result request fingerprint is invalid")
        if self.status not in {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.FAILED,
            ToolOperationStatus.UNKNOWN,
        }:
            raise ValueError("ToolNode result status is not durable")
        if self.status is ToolOperationStatus.SUCCEEDED:
            if self.result is None or self.error is not None:
                raise ValueError("successful ToolNode result requires only ToolResult")
            if self.result.tool != self.tool:
                raise ValueError("ToolNode result Tool identity does not match")
        elif self.result is not None or self.error is None:
            raise ValueError("non-success ToolNode result requires only an error")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "operation_id": self.operation_id.value,
            "tool": self.tool.to_data(),
            "request_fingerprint": self.request_fingerprint,
            "status": self.status.value,
            "result": self.result.to_data() if self.result else None,
            "error": self.error.to_data() if self.error else None,
            "evidence_ref": self.evidence_ref.to_data() if self.evidence_ref else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolNodeResult:
        _require_keys(
            data,
            {
                "contract_version",
                "operation_id",
                "tool",
                "request_fingerprint",
                "status",
                "result",
                "error",
                "evidence_ref",
            },
            "ToolNode result",
        )
        raw_result, raw_error, raw_evidence = (
            data["result"],
            data["error"],
            data["evidence_ref"],
        )
        return cls(
            operation_id=ResourceId(_as_string(data["operation_id"], "operation id")),
            tool=CapabilityRef.from_data(as_object(data["tool"], "ToolNode tool")),
            request_fingerprint=_as_string(data["request_fingerprint"], "fingerprint"),
            status=ToolOperationStatus(_as_string(data["status"], "ToolNode status")),
            result=ToolResult.from_data(as_object(raw_result, "ToolNode ToolResult"))
            if raw_result is not None
            else None,
            error=ErrorDetail.from_data(as_object(raw_error, "ToolNode error"))
            if raw_error is not None
            else None,
            evidence_ref=CheckpointReference.from_data(
                as_object(raw_evidence, "ToolNode evidence")
            )
            if raw_evidence is not None
            else None,
            contract_version=_as_string(
                data["contract_version"], "ToolNode result contract version"
            ),
        )


class ToolNodeRequestSchemaValidator:
    def validate(self, value: JsonValue) -> None:
        ToolNodeRequest.from_data(as_object(value, "ToolNode request"))


class ToolNodeResultSchemaValidator:
    def validate(self, value: JsonValue) -> None:
        ToolNodeResult.from_data(as_object(value, "ToolNode result"))


@dataclass(frozen=True, slots=True)
class ToolOperationResolution:
    operation_id: ResourceId
    expected_version: int
    status: ToolOperationStatus
    evidence_ref: CheckpointReference
    output: JsonValue = None
    error: ErrorDetail | None = None
    attempts: int = 1
    contract_version: str = _TOOL_NODE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != _TOOL_NODE_CONTRACT_VERSION:
            raise ValueError("Tool resolution contract version is unsupported")
        if self.expected_version < 0 or self.attempts < 1:
            raise ValueError("Tool resolution version and attempts must be valid")
        if self.status not in {
            ToolOperationStatus.SUCCEEDED,
            ToolOperationStatus.FAILED,
        }:
            raise ValueError("Tool resolution outcome must be succeeded or failed")
        object.__setattr__(
            self, "output", as_json_value(self.output, "Tool resolution output")
        )
        if self.status is ToolOperationStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful Tool resolution cannot contain an error")
        if self.status is ToolOperationStatus.FAILED and (
            self.error is None or self.output is not None
        ):
            raise ValueError("failed Tool resolution requires only an error")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "operation_id": self.operation_id.value,
            "expected_version": self.expected_version,
            "status": self.status.value,
            "evidence_ref": self.evidence_ref.to_data(),
            "output": self.output,
            "error": self.error.to_data() if self.error else None,
            "attempts": self.attempts,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolOperationResolution:
        _require_keys(
            data,
            {
                "contract_version",
                "operation_id",
                "expected_version",
                "status",
                "evidence_ref",
                "output",
                "error",
                "attempts",
            },
            "Tool operation resolution",
        )
        raw_error = data["error"]
        return cls(
            ResourceId(_as_string(data["operation_id"], "operation id")),
            as_int(data["expected_version"], "expected version"),
            ToolOperationStatus(_as_string(data["status"], "resolution status")),
            CheckpointReference.from_data(
                as_object(data["evidence_ref"], "resolution evidence")
            ),
            as_json_value(data["output"], "resolution output"),
            ErrorDetail.from_data(as_object(raw_error, "resolution error"))
            if raw_error is not None
            else None,
            as_int(data["attempts"], "resolution attempts"),
            _as_string(data["contract_version"], "resolution contract version"),
        )


@dataclass(frozen=True, slots=True)
class ContextNodeResult:
    """Stable JSON value persisted for a successful ContextNode.

    ResolvedContext is an in-process resolver result. This separate contract is
    the durable Workflow boundary: it has its own version, exact wire shape, and
    invariants so recovery does not depend on the resolver's internal objects.
    """

    contract_version: str
    entries: tuple[ContextEntry, ...]
    completeness: ContextCompleteness
    missing_keys: tuple[ContextKey, ...]
    warnings: tuple[ContextWarning, ...]
    selected_providers: tuple[ProviderId, ...]
    usage: ContextUsage

    def __post_init__(self) -> None:
        # These are persistence invariants, not merely Provider validation. A
        # recovered reader must never have to guess whether a duplicated key,
        # duplicated Provider, or present-and-missing key is authoritative.
        if self.contract_version != _CONTEXT_NODE_RESULT_CONTRACT_VERSION:
            raise ValueError("ContextNode result contract version is unsupported")
        if not self.selected_providers:
            raise ValueError("ContextNode result requires at least one provider")
        if len(set(self.selected_providers)) != len(self.selected_providers):
            raise ValueError("ContextNode result providers must be unique")
        entry_keys = tuple(entry.key for entry in self.entries)
        if len(set(entry_keys)) != len(entry_keys):
            raise ValueError("ContextNode result entry keys must be unique")
        if len(set(self.missing_keys)) != len(self.missing_keys):
            raise ValueError("ContextNode result missing keys must be unique")
        if set(entry_keys) & set(self.missing_keys):
            raise ValueError("ContextNode result keys cannot be present and missing")
        if self.completeness is ContextCompleteness.COMPLETE and self.missing_keys:
            raise ValueError("complete ContextNode result cannot report missing keys")
        if self.completeness is ContextCompleteness.PARTIAL and not self.missing_keys:
            raise ValueError("partial ContextNode result must report missing keys")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entries": [entry.to_data() for entry in self.entries],
            "completeness": self.completeness.value,
            "missing_keys": [key.to_data() for key in self.missing_keys],
            "warnings": [warning.to_data() for warning in self.warnings],
            "selected_providers": [item.value for item in self.selected_providers],
            "usage": self.usage.to_data(),
        }

    @classmethod
    def from_resolved(cls, resolved: ResolvedContext) -> ContextNodeResult:
        # Keep this conversion intentionally lossless. Provider selection, usage,
        # warnings, and missing keys are recovery/debugging evidence, not optional
        # observability metadata that may be dropped before persistence.
        return cls(
            contract_version=_CONTEXT_NODE_RESULT_CONTRACT_VERSION,
            entries=resolved.entries,
            completeness=resolved.completeness,
            missing_keys=resolved.missing_keys,
            warnings=resolved.warnings,
            selected_providers=resolved.selected_providers,
            usage=resolved.usage,
        )

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContextNodeResult:
        _require_keys(
            data,
            {
                "contract_version",
                "entries",
                "completeness",
                "missing_keys",
                "warnings",
                "selected_providers",
                "usage",
            },
            "ContextNode result",
        )
        entries = tuple(
            ContextEntry.from_data(_strict_context_entry_data(item))
            for item in as_array(data["entries"], "ContextNode result entries")
        )
        missing_keys = tuple(
            ContextKey.from_data(
                _strict_context_key_data(item, "ContextNode result missing key")
            )
            for item in as_array(
                data["missing_keys"], "ContextNode result missing keys"
            )
        )
        warnings = tuple(
            ContextWarning.from_data(_strict_context_warning_data(item))
            for item in as_array(data["warnings"], "ContextNode result warnings")
        )
        usage = _strict_context_object(
            data["usage"], {"byte_count", "token_count"}, "ContextNode result usage"
        )
        return cls(
            contract_version=_as_string(
                data["contract_version"], "ContextNode result contract version"
            ),
            entries=entries,
            completeness=ContextCompleteness(
                _as_string(data["completeness"], "ContextNode result completeness")
            ),
            missing_keys=missing_keys,
            warnings=warnings,
            selected_providers=tuple(
                ProviderId(_as_string(item, "ContextNode result provider"))
                for item in as_array(
                    data["selected_providers"], "ContextNode result providers"
                )
            ),
            usage=ContextUsage.from_data(usage),
        )


class ContextNodeResultSchemaValidator:
    """SchemaRegistry adapter for the fixed ContextNode durable result shape."""

    def validate(self, value: JsonValue) -> None:
        ContextNodeResult.from_data(as_object(value, "ContextNode result"))


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
class EvaluationNodeConfig:
    """Opaque bindings selected by a node; Core never embeds their business rules."""

    policy_ref: str
    quality_evaluator_id: ProviderId
    quality_profile_ref: str

    def __post_init__(self) -> None:
        for name, value in (
            ("policy_ref", self.policy_ref),
            ("quality_profile_ref", self.quality_profile_ref),
        ):
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and trimmed")

    def to_data(self) -> dict[str, str]:
        return {
            "policy_ref": self.policy_ref,
            "quality_evaluator_id": self.quality_evaluator_id.value,
            "quality_profile_ref": self.quality_profile_ref,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> EvaluationNodeConfig:
        _require_keys(
            data,
            {"policy_ref", "quality_evaluator_id", "quality_profile_ref"},
            "EvaluationNode config",
        )
        return cls(
            policy_ref=_as_string(data["policy_ref"], "Evaluation policy ref"),
            quality_evaluator_id=ProviderId(
                _as_string(data["quality_evaluator_id"], "quality evaluator id")
            ),
            quality_profile_ref=_as_string(
                data["quality_profile_ref"], "quality profile ref"
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


type WorkflowNodeConfig = (
    AgentNodeConfig
    | ApprovalNodeConfig
    | ContextNodeConfig
    | EvaluationNodeConfig
    | SkillNodeConfig
    | ToolNodeConfig
    | UnsupportedNodeConfig
)


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
        elif node_type == WorkflowNodeType.CONTEXT.value:
            config = ContextNodeConfig.from_data(raw_config)
        elif node_type == WorkflowNodeType.SKILL.value:
            config = SkillNodeConfig.from_data(raw_config)
        elif node_type == WorkflowNodeType.TOOL.value:
            config = ToolNodeConfig.from_data(raw_config)
        elif node_type == WorkflowNodeType.APPROVAL.value:
            config = ApprovalNodeConfig.from_data(raw_config)
        elif node_type == WorkflowNodeType.EVALUATION.value:
            config = EvaluationNodeConfig.from_data(raw_config)
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
class ToolOperationSuspension:
    run: WorkflowRun
    checkpoint_ref: CheckpointRef
    operation_id: ResourceId
    record_version: int

    def __post_init__(self) -> None:
        if self.run.status is not RunStatus.PAUSED:
            raise ValueError("Tool operation suspension requires PAUSED")
        if self.record_version < 0:
            raise ValueError("Tool operation suspension version is invalid")

    def to_data(self) -> dict[str, object]:
        return {
            "run": self.run.to_data(),
            "checkpoint_ref": self.checkpoint_ref.value,
            "operation_id": self.operation_id.value,
            "record_version": self.record_version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolOperationSuspension:
        _require_keys(
            data,
            {"run", "checkpoint_ref", "operation_id", "record_version"},
            "Tool operation suspension",
        )
        run = Run.from_data(as_object(data["run"], "Tool suspension run"))
        if not isinstance(run, WorkflowRun):
            raise ValueError("Tool operation suspension requires WorkflowRun")
        return cls(
            run,
            CheckpointRef(_as_string(data["checkpoint_ref"], "checkpoint ref")),
            ResourceId(_as_string(data["operation_id"], "operation id")),
            as_int(data["record_version"], "record version"),
        )


@dataclass(frozen=True, slots=True)
class WorkflowExecutionOutcome:
    result: WorkflowResult | None = None
    suspension: WorkflowSuspension | ToolOperationSuspension | None = None

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
            suspension = as_object(raw_suspension, "Workflow outcome suspension")
            if "approval" in suspension:
                return cls(suspension=WorkflowSuspension.from_data(suspension))
            return cls(suspension=ToolOperationSuspension.from_data(suspension))
        raise ValueError("Workflow outcome discriminator does not match its payload")


def _require_keys(
    data: dict[str, object], expected: Collection[str], field_name: str
) -> None:
    if set(data) != set(expected):
        raise ValueError(f"{field_name} contains unknown or missing fields")


def _strict_context_object(
    value: object, expected: Collection[str], field_name: str
) -> dict[str, object]:
    data = as_object(value, field_name)
    _require_keys(data, expected, field_name)
    return data


# ContextProvider v1 decoders predate ContextNode's byte-exact persistence
# contract and intentionally accept some optional fields. These wrappers close
# that gap at the Workflow boundary: nested objects are checked just as strictly
# as the top-level ContextNodeConfig and ContextNodeResult objects.


def _strict_context_key_data(value: object, field_name: str) -> dict[str, object]:
    data = _strict_context_object(value, {"namespace", "name"}, field_name)
    _as_string(data["namespace"], f"{field_name} namespace")
    _as_string(data["name"], f"{field_name} name")
    return data


def _strict_schema_data(value: object, field_name: str) -> dict[str, object]:
    data = _strict_context_object(value, {"namespace", "name", "version"}, field_name)
    _as_string(data["namespace"], f"{field_name} namespace")
    _as_string(data["name"], f"{field_name} name")
    _as_string(data["version"], f"{field_name} version")
    return data


def _strict_context_binding_data(value: object) -> dict[str, object]:
    data = _strict_context_object(
        value,
        {
            "provider_ids",
            "requirements",
            "merge_strategy",
            "completeness_policy",
            "budget",
        },
        "ContextNode binding",
    )
    for provider_id in as_array(data["provider_ids"], "ContextNode providers"):
        _as_string(provider_id, "ContextNode provider id")
    for value in as_array(data["requirements"], "ContextNode requirements"):
        requirement = _strict_context_object(
            value, {"key", "schema"}, "ContextNode requirement"
        )
        _strict_context_key_data(requirement["key"], "ContextNode requirement key")
        _strict_schema_data(requirement["schema"], "ContextNode requirement schema")
    _as_string(data["merge_strategy"], "ContextNode merge strategy")
    _as_string(data["completeness_policy"], "ContextNode completeness policy")
    _strict_context_object(
        data["budget"], {"max_bytes", "max_tokens"}, "ContextNode budget"
    )
    return data


def _strict_context_entry_data(value: object) -> dict[str, object]:
    data = _strict_context_object(
        value,
        {"key", "schema", "value", "provenance", "fresh_at", "expires_at"},
        "ContextNode result entry",
    )
    _strict_context_key_data(data["key"], "ContextNode result entry key")
    _strict_schema_data(data["schema"], "ContextNode result entry schema")
    for provenance in as_array(
        data["provenance"], "ContextNode result entry provenance"
    ):
        _as_string(provenance, "ContextNode result entry provenance")
    for field_name in ("fresh_at", "expires_at"):
        if data[field_name] is not None:
            _as_string(data[field_name], f"ContextNode result entry {field_name}")
    return data


def _strict_context_warning_data(value: object) -> dict[str, object]:
    data = _strict_context_object(
        value, {"code", "message", "key"}, "ContextNode result warning"
    )
    _as_string(data["code"], "ContextNode result warning code")
    _as_string(data["message"], "ContextNode result warning message")
    if data["key"] is not None:
        _strict_context_key_data(data["key"], "ContextNode result warning key")
    return data


def _as_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _as_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value
