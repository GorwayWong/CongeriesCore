"""Immutable Tool v1 descriptors and invocation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from congeries_core.policy.authorization import ActionRef
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.json_types import (
    JsonValue,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.schema import SchemaRef

TOOL_CONTRACT_VERSION = "1"
TOOL_EXECUTE_ACTION = ActionRef("core", "tool.execute", "1")


def tool_actions() -> tuple[ActionRef, ...]:
    return (TOOL_EXECUTE_ACTION,)


class ToolSideEffect(StrEnum):
    NONE = "none"
    EXTERNAL = "external"


class ToolIdempotencyMode(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    CALLER_KEY = "caller_key"


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    timeout_ms: int | None = None
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.timeout_ms is not None:
            _require_positive_int(self.timeout_ms, "Tool timeout_ms")
        _require_positive_int(self.max_attempts, "Tool max_attempts")

    def to_data(self) -> dict[str, object]:
        return {"timeout_ms": self.timeout_ms, "max_attempts": self.max_attempts}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolExecutionPolicy:
        if set(data) != {"timeout_ms", "max_attempts"}:
            raise ValueError("Tool execution policy fields are invalid")
        raw_timeout = data.get("timeout_ms")
        return cls(
            timeout_ms=(
                as_int(raw_timeout, "Tool timeout_ms")
                if raw_timeout is not None
                else None
            ),
            max_attempts=as_int(data["max_attempts"], "Tool max_attempts"),
        )


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    ref: CapabilityRef
    title: str
    summary: str
    input_schema: SchemaRef
    output_schema: SchemaRef
    action: ActionRef
    execution_policy: ToolExecutionPolicy
    side_effect: ToolSideEffect
    idempotency: ToolIdempotencyMode

    def __post_init__(self) -> None:
        if self.ref.namespace != "core" or self.ref.kind != "tool":
            raise ValueError("Tool descriptor requires a core Tool reference")
        if self.ref.contract_version != TOOL_CONTRACT_VERSION:
            raise ValueError("Tool descriptor contract version is unsupported")
        _require_text(self.title, "Tool title")
        _require_text(self.summary, "Tool summary")
        if self.side_effect is ToolSideEffect.EXTERNAL:
            if self.idempotency is not ToolIdempotencyMode.CALLER_KEY:
                raise ValueError("side-effecting Tool requires caller-key idempotency")
        elif self.idempotency is not ToolIdempotencyMode.NOT_APPLICABLE:
            raise ValueError(
                "side-effect-free Tool must use not_applicable idempotency"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_data(),
            "title": self.title,
            "summary": self.summary,
            "input_schema": self.input_schema.to_data(),
            "output_schema": self.output_schema.to_data(),
            "action": self.action.to_data(),
            "execution_policy": self.execution_policy.to_data(),
            "side_effect": self.side_effect.value,
            "idempotency": self.idempotency.value,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolDescriptor:
        if set(data) != {
            "ref",
            "title",
            "summary",
            "input_schema",
            "output_schema",
            "action",
            "execution_policy",
            "side_effect",
            "idempotency",
        }:
            raise ValueError("Tool descriptor fields are invalid")
        return cls(
            ref=CapabilityRef.from_data(as_object(data["ref"], "Tool reference")),
            title=str(data["title"]),
            summary=str(data["summary"]),
            input_schema=SchemaRef.from_data(
                as_object(data["input_schema"], "Tool input schema")
            ),
            output_schema=SchemaRef.from_data(
                as_object(data["output_schema"], "Tool output schema")
            ),
            action=ActionRef.from_data(as_object(data["action"], "Tool action")),
            execution_policy=ToolExecutionPolicy.from_data(
                as_object(data["execution_policy"], "Tool execution policy")
            ),
            side_effect=ToolSideEffect(str(data["side_effect"])),
            idempotency=ToolIdempotencyMode(str(data["idempotency"])),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: CapabilityRef
    input: JsonValue

    def __post_init__(self) -> None:
        if (
            self.tool.namespace != "core"
            or self.tool.kind != "tool"
            or self.tool.contract_version != TOOL_CONTRACT_VERSION
        ):
            raise ValueError("Tool call requires a Tool v1 reference")
        object.__setattr__(self, "input", as_json_value(self.input, "Tool input"))

    def to_data(self) -> dict[str, object]:
        return {"tool": self.tool.to_data(), "input": self.input}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolCall:
        if set(data) != {"tool", "input"}:
            raise ValueError("Tool call fields are invalid")
        return cls(
            CapabilityRef.from_data(as_object(data["tool"], "Tool reference")),
            as_json_value(data.get("input"), "Tool input"),
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: CapabilityRef
    output: JsonValue
    attempts: int
    operation_identity: str

    def __post_init__(self) -> None:
        if (
            self.tool.namespace != "core"
            or self.tool.kind != "tool"
            or self.tool.contract_version != TOOL_CONTRACT_VERSION
        ):
            raise ValueError("Tool result requires a Tool v1 reference")
        object.__setattr__(self, "output", as_json_value(self.output, "Tool output"))
        _require_positive_int(self.attempts, "Tool result attempts")
        _require_text(self.operation_identity, "Tool operation identity")

    def to_data(self) -> dict[str, object]:
        return {
            "tool": self.tool.to_data(),
            "output": self.output,
            "attempts": self.attempts,
            "operation_identity": self.operation_identity,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolResult:
        if set(data) != {"tool", "output", "attempts", "operation_identity"}:
            raise ValueError("Tool result fields are invalid")
        return cls(
            tool=CapabilityRef.from_data(as_object(data["tool"], "Tool reference")),
            output=as_json_value(data.get("output"), "Tool output"),
            attempts=as_int(data["attempts"], "Tool attempts"),
            operation_identity=str(data["operation_identity"]),
        )


class ToolExecutor(Protocol):
    async def execute(
        self, call: ToolCall, context: RuntimeCallContext
    ) -> JsonValue: ...


@dataclass(frozen=True, slots=True)
class ToolImplementation:
    descriptor: ToolDescriptor
    executor: ToolExecutor


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")


def _require_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
