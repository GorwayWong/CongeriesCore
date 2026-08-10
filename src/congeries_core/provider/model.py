"""Vendor-neutral ModelProvider contracts and authorized gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import (
    ModelBindingRef,
    ModelId,
    PrincipalId,
    ProviderId,
    ResourceId,
)
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from ._control import await_provider
from .context import ContextEntry
from .events import NullProviderEventPublisher, ProviderEventPublisher

MODEL_CAPABILITIES_ACTION = ActionRef("core", "model.capabilities", "1")
MODEL_GENERATE_ACTION = ActionRef("core", "model.generate", "1")
MODEL_STREAM_ACTION = ActionRef("core", "model.stream", "1")

MODEL_INVOCATION_STARTED = "core.model.invocation_started"
MODEL_INVOCATION_COMPLETED = "core.model.invocation_completed"
MODEL_INVOCATION_FAILED = "core.model.invocation_failed"


def model_actions() -> tuple[ActionRef, ActionRef, ActionRef]:
    return MODEL_CAPABILITIES_ACTION, MODEL_GENERATE_ACTION, MODEL_STREAM_ACTION


@dataclass(frozen=True, slots=True, order=True)
class ModelSelector:
    provider_id: ProviderId
    model_id: ModelId

    def to_data(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id.value,
            "model_id": self.model_id.value,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelSelector:
        return cls(ProviderId(str(data["provider_id"])), ModelId(str(data["model_id"])))


class ModelOperation(StrEnum):
    GENERATE = "generate"
    STREAM = "stream"


@dataclass(frozen=True, slots=True)
class ModelBudget:
    max_input_units: int | None = None
    max_output_units: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_units", self.max_input_units),
            ("max_output_units", self.max_output_units),
        ):
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ValueError(f"model {name} must be positive")

    def to_data(self) -> dict[str, int | None]:
        return {
            "max_input_units": self.max_input_units,
            "max_output_units": self.max_output_units,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelBudget:
        raw_input = data.get("max_input_units")
        raw_output = data.get("max_output_units")
        return cls(
            max_input_units=(
                as_int(raw_input, "max_input_units") if raw_input is not None else None
            ),
            max_output_units=(
                as_int(raw_output, "max_output_units")
                if raw_output is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_units: int
    output_units: int
    unit: str = "token"

    def __post_init__(self) -> None:
        if self.input_units < 0 or self.output_units < 0:
            raise ValueError("model usage cannot be negative")
        if not self.unit or self.unit != self.unit.strip():
            raise ValueError("model usage unit must be non-empty and trimmed")

    @property
    def total_units(self) -> int:
        return self.input_units + self.output_units

    def to_data(self) -> dict[str, object]:
        return {
            "input_units": self.input_units,
            "output_units": self.output_units,
            "total_units": self.total_units,
            "unit": self.unit,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelUsage:
        return cls(
            as_int(data["input_units"], "input_units"),
            as_int(data["output_units"], "output_units"),
            str(data["unit"]),
        )


@dataclass(frozen=True, slots=True)
class ModelRequestPolicy:
    allow_partial: bool = False
    allow_tool_requests: bool = False

    def to_data(self) -> dict[str, bool]:
        return {
            "allow_partial": self.allow_partial,
            "allow_tool_requests": self.allow_tool_requests,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelRequestPolicy:
        return cls(
            allow_partial=bool(data["allow_partial"]),
            allow_tool_requests=bool(data["allow_tool_requests"]),
        )


@dataclass(frozen=True, slots=True)
class ModelCapabilityRequirements:
    streaming: bool = False
    structured_output: bool = False
    tool_calls: bool = False
    input_kinds: frozenset[ContentKind] = field(
        default_factory=lambda: frozenset({ContentKind.TEXT})
    )
    output_kinds: frozenset[ContentKind] = field(
        default_factory=lambda: frozenset({ContentKind.TEXT})
    )

    def to_data(self) -> dict[str, object]:
        return {
            "streaming": self.streaming,
            "structured_output": self.structured_output,
            "tool_calls": self.tool_calls,
            "input_kinds": sorted(item.value for item in self.input_kinds),
            "output_kinds": sorted(item.value for item in self.output_kinds),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelCapabilityRequirements:
        return cls(
            streaming=bool(data["streaming"]),
            structured_output=bool(data["structured_output"]),
            tool_calls=bool(data["tool_calls"]),
            input_kinds=frozenset(
                ContentKind(str(item))
                for item in as_array(data["input_kinds"], "model input kinds")
            ),
            output_kinds=frozenset(
                ContentKind(str(item))
                for item in as_array(data["output_kinds"], "model output kinds")
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelBinding:
    ref: ModelBindingRef
    selector: ModelSelector
    required: ModelCapabilityRequirements = field(
        default_factory=ModelCapabilityRequirements
    )
    default_policy: ModelRequestPolicy = field(default_factory=ModelRequestPolicy)
    default_budget: ModelBudget = field(default_factory=ModelBudget)
    fallbacks: tuple[ModelSelector, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        selectors = (self.selector, *self.fallbacks)
        if len(set(selectors)) != len(selectors):
            raise ValueError("model binding selectors must be unique")

    @property
    def selectors(self) -> tuple[ModelSelector, ...]:
        return self.selector, *self.fallbacks

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.value,
            "selector": self.selector.to_data(),
            "required": self.required.to_data(),
            "default_policy": self.default_policy.to_data(),
            "default_budget": self.default_budget.to_data(),
            "fallbacks": [item.to_data() for item in self.fallbacks],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelBinding:
        return cls(
            ref=ModelBindingRef(str(data["ref"])),
            selector=ModelSelector.from_data(
                as_object(data["selector"], "model selector")
            ),
            required=ModelCapabilityRequirements.from_data(
                as_object(data["required"], "model requirements")
            ),
            default_policy=ModelRequestPolicy.from_data(
                as_object(data["default_policy"], "model policy")
            ),
            default_budget=ModelBudget.from_data(
                as_object(data["default_budget"], "model budget")
            ),
            fallbacks=tuple(
                ModelSelector.from_data(as_object(item, "fallback model selector"))
                for item in as_array(data["fallbacks"], "fallback model selectors")
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolCallProposal:
    call_id: str
    tool: ResourceRef
    arguments: JsonValue

    def __post_init__(self) -> None:
        if not self.call_id or self.call_id != self.call_id.strip():
            raise ValueError("tool call id must be non-empty and trimmed")
        object.__setattr__(
            self, "arguments", as_json_value(self.arguments, "tool call arguments")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "tool": self.tool.to_data(),
            "arguments": self.arguments,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ToolCallProposal:
        return cls(
            call_id=str(data["call_id"]),
            tool=ResourceRef.from_data(as_object(data["tool"], "tool reference")),
            arguments=as_json_value(data.get("arguments"), "tool arguments"),
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    selector: ModelSelector
    input: tuple[ContentBlock, ...]
    instructions: tuple[ContentBlock, ...] = field(default_factory=tuple)
    context_entries: tuple[ContextEntry, ...] = field(default_factory=tuple)
    tools: tuple[ResourceRef, ...] = field(default_factory=tuple)
    output_schema: SchemaRef | None = None
    policy: ModelRequestPolicy = field(default_factory=ModelRequestPolicy)
    budget: ModelBudget = field(default_factory=ModelBudget)

    def __post_init__(self) -> None:
        if not self.input:
            raise ValueError("model request input must not be empty")
        if len({item.key for item in self.tools}) != len(self.tools):
            raise ValueError("model request tool references must be unique")


class ModelFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_REQUEST = "tool_request"
    FILTERED = "filtered"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ModelResponse:
    output: tuple[ContentBlock, ...]
    finish_reason: ModelFinishReason
    usage: ModelUsage
    provider_id: ProviderId
    model_id: ModelId
    structured_output: JsonValue | None = None
    tool_requests: tuple[ToolCallProposal, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    provenance: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            not self.output
            and self.structured_output is None
            and not self.tool_requests
        ):
            raise ValueError("model response must contain output or a tool request")
        if self.structured_output is not None:
            object.__setattr__(
                self,
                "structured_output",
                as_json_value(self.structured_output, "structured model output"),
            )
        if any(not item or item != item.strip() for item in self.warnings):
            raise ValueError("model response warnings must be non-empty and trimmed")
        if any(not item or item != item.strip() for item in self.provenance):
            raise ValueError("model response provenance must be non-empty and trimmed")

    def to_data(self) -> dict[str, object]:
        return {
            "output": [item.to_data() for item in self.output],
            "structured_output": self.structured_output,
            "tool_requests": [item.to_data() for item in self.tool_requests],
            "finish_reason": self.finish_reason.value,
            "usage": self.usage.to_data(),
            "provider_id": self.provider_id.value,
            "model_id": self.model_id.value,
            "warnings": list(self.warnings),
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelResponse:
        return cls(
            output=tuple(
                ContentBlock.from_data(as_object(item, "model output block"))
                for item in as_array(data["output"], "model output")
            ),
            structured_output=(
                as_json_value(data["structured_output"], "structured model output")
                if data.get("structured_output") is not None
                else None
            ),
            tool_requests=tuple(
                ToolCallProposal.from_data(as_object(item, "tool request"))
                for item in as_array(data["tool_requests"], "tool requests")
            ),
            finish_reason=ModelFinishReason(str(data["finish_reason"])),
            usage=ModelUsage.from_data(as_object(data["usage"], "model usage")),
            provider_id=ProviderId(str(data["provider_id"])),
            model_id=ModelId(str(data["model_id"])),
            warnings=tuple(
                str(item) for item in as_array(data["warnings"], "model warnings")
            ),
            provenance=tuple(
                str(item) for item in as_array(data["provenance"], "model provenance")
            ),
        )


class ModelEventType(StrEnum):
    START = "start"
    CONTENT_DELTA = "content_delta"
    STRUCTURED_DELTA = "structured_delta"
    TOOL_REQUEST = "tool_request"
    USAGE_UPDATE = "usage_update"
    COMPLETION = "completion"
    FAILURE = "failure"

    @property
    def terminal(self) -> bool:
        return self in {ModelEventType.COMPLETION, ModelEventType.FAILURE}


@dataclass(frozen=True, slots=True)
class ModelEvent:
    type: ModelEventType
    content: ContentBlock | None = None
    structured_delta: JsonValue | None = None
    tool_request: ToolCallProposal | None = None
    usage: ModelUsage | None = None
    response: ModelResponse | None = None
    error: ErrorDetail | None = None

    def __post_init__(self) -> None:
        if self.structured_delta is not None:
            object.__setattr__(
                self,
                "structured_delta",
                as_json_value(self.structured_delta, "structured model delta"),
            )
        expected = {
            ModelEventType.START: (),
            ModelEventType.CONTENT_DELTA: ("content",),
            ModelEventType.STRUCTURED_DELTA: ("structured_delta",),
            ModelEventType.TOOL_REQUEST: ("tool_request",),
            ModelEventType.USAGE_UPDATE: ("usage",),
            ModelEventType.COMPLETION: ("response",),
            ModelEventType.FAILURE: ("error",),
        }[self.type]
        values = {
            "content": self.content,
            "structured_delta": self.structured_delta,
            "tool_request": self.tool_request,
            "usage": self.usage,
            "response": self.response,
            "error": self.error,
        }
        if any(values[name] is None for name in expected):
            raise ValueError(f"{self.type.value} model event is missing its payload")
        if any(
            value is not None for name, value in values.items() if name not in expected
        ):
            raise ValueError(f"{self.type.value} model event has an unexpected payload")

    @classmethod
    def start(cls) -> ModelEvent:
        return cls(ModelEventType.START)

    @classmethod
    def completion(cls, response: ModelResponse) -> ModelEvent:
        return cls(ModelEventType.COMPLETION, response=response)

    @classmethod
    def failure(cls, error: ErrorDetail) -> ModelEvent:
        return cls(ModelEventType.FAILURE, error=error)

    def to_data(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "content": self.content.to_data() if self.content else None,
            "structured_delta": self.structured_delta,
            "tool_request": self.tool_request.to_data() if self.tool_request else None,
            "usage": self.usage.to_data() if self.usage else None,
            "response": self.response.to_data() if self.response else None,
            "error": self.error.to_data() if self.error else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelEvent:
        raw_content = data.get("content")
        raw_tool = data.get("tool_request")
        raw_usage = data.get("usage")
        raw_response = data.get("response")
        raw_error = data.get("error")
        return cls(
            type=ModelEventType(str(data["type"])),
            content=(
                ContentBlock.from_data(as_object(raw_content, "model event content"))
                if raw_content is not None
                else None
            ),
            structured_delta=(
                as_json_value(data["structured_delta"], "structured model delta")
                if data.get("structured_delta") is not None
                else None
            ),
            tool_request=(
                ToolCallProposal.from_data(
                    as_object(raw_tool, "model event tool request")
                )
                if raw_tool is not None
                else None
            ),
            usage=(
                ModelUsage.from_data(as_object(raw_usage, "model event usage"))
                if raw_usage is not None
                else None
            ),
            response=(
                ModelResponse.from_data(as_object(raw_response, "model event response"))
                if raw_response is not None
                else None
            ),
            error=(
                ErrorDetail.from_data(as_object(raw_error, "model event error"))
                if raw_error is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    selector: ModelSelector
    operations: frozenset[ModelOperation]
    structured_output: bool
    tool_calls: bool
    input_kinds: frozenset[ContentKind]
    output_kinds: frozenset[ContentKind]
    maximum_budget: ModelBudget
    usage_reporting: bool
    contract_version: str

    def __post_init__(self) -> None:
        if not self.contract_version:
            raise ValueError("model provider contract version is required")

    def satisfies(self, required: ModelCapabilityRequirements) -> bool:
        return (
            ModelOperation.GENERATE in self.operations
            and (not required.streaming or ModelOperation.STREAM in self.operations)
            and (not required.structured_output or self.structured_output)
            and (not required.tool_calls or self.tool_calls)
            and required.input_kinds.issubset(self.input_kinds)
            and required.output_kinds.issubset(self.output_kinds)
        )

    def to_data(self) -> dict[str, object]:
        return {
            "selector": self.selector.to_data(),
            "operations": sorted(item.value for item in self.operations),
            "structured_output": self.structured_output,
            "tool_calls": self.tool_calls,
            "input_kinds": sorted(item.value for item in self.input_kinds),
            "output_kinds": sorted(item.value for item in self.output_kinds),
            "maximum_budget": self.maximum_budget.to_data(),
            "usage_reporting": self.usage_reporting,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelCapabilities:
        return cls(
            selector=ModelSelector.from_data(
                as_object(data["selector"], "model selector")
            ),
            operations=frozenset(
                ModelOperation(str(item))
                for item in as_array(data["operations"], "model operations")
            ),
            structured_output=bool(data["structured_output"]),
            tool_calls=bool(data["tool_calls"]),
            input_kinds=frozenset(
                ContentKind(str(item))
                for item in as_array(data["input_kinds"], "model input kinds")
            ),
            output_kinds=frozenset(
                ContentKind(str(item))
                for item in as_array(data["output_kinds"], "model output kinds")
            ),
            maximum_budget=ModelBudget.from_data(
                as_object(data["maximum_budget"], "maximum model budget")
            ),
            usage_reporting=bool(data["usage_reporting"]),
            contract_version=str(data["contract_version"]),
        )


class ModelProvider(Protocol):
    async def generate(
        self, request: ModelRequest, context: RuntimeCallContext
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, context: RuntimeCallContext
    ) -> AsyncIterator[ModelEvent]: ...

    async def capabilities(
        self, selector: ModelSelector, context: RuntimeCallContext
    ) -> ModelCapabilities: ...


@runtime_checkable
class _AsyncClosable(Protocol):
    async def aclose(self) -> None: ...


class ModelProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderId, ModelProvider] = {}

    def register(self, provider_id: ProviderId, provider: ModelProvider) -> None:
        if provider_id in self._providers:
            raise core_error(
                ErrorCategory.CONFLICT,
                "model_provider_already_registered",
                "model provider is already registered",
            )
        self._providers[provider_id] = provider

    def get(self, provider_id: ProviderId) -> ModelProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "model_provider_not_registered",
                "model provider is not registered",
                retryable=True,
            )
        return provider


class ModelGateway:
    def __init__(
        self,
        *,
        providers: ModelProviderRegistry,
        schemas: SchemaRegistry,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._providers = providers
        self._schemas = schemas
        self._dispatcher = dispatcher
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def capabilities(
        self, selector: ModelSelector, context: RuntimeCallContext
    ) -> ModelCapabilities:
        provider = self._providers.get(selector.provider_id)
        access = self._access_request(
            selector,
            context,
            MODEL_CAPABILITIES_ACTION,
            {"model": selector.model_id.value},
        )

        async def operation(call: AuthorizedCall) -> object:
            self._validate_capability_grant(selector, call)
            try:
                return await await_provider(
                    provider.capabilities(selector, call.context),
                    call.context,
                    self._clock,
                )
            except CoreError:
                raise
            except Exception as error:
                raise self._provider_failure(error) from error

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, ModelCapabilities):
            self._protocol_failure("model provider returned invalid capabilities")
        capabilities = cast(ModelCapabilities, raw)
        if capabilities.selector != selector:
            self._protocol_failure("model capabilities returned a mismatched selector")
        return capabilities

    async def generate(
        self, request: ModelRequest, context: RuntimeCallContext
    ) -> ModelResponse:
        started_at = self._clock.now()
        provider = self._providers.get(request.selector.provider_id)
        access = self._access_request(
            request.selector,
            context,
            MODEL_GENERATE_ACTION,
            self._constraints(request),
        )
        await self._emit(
            MODEL_INVOCATION_STARTED,
            context,
            {
                "operation": ModelOperation.GENERATE.value,
                "provider_id": request.selector.provider_id.value,
                "model_id": request.selector.model_id.value,
            },
        )
        try:

            async def operation(call: AuthorizedCall) -> object:
                constrained = self._constrain_request(request, call)
                try:
                    return await await_provider(
                        provider.generate(constrained, call.context),
                        call.context,
                        self._clock,
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise self._provider_failure(error) from error

            raw = await self._dispatcher.dispatch(access, operation)
            if not isinstance(raw, ModelResponse):
                self._protocol_failure("model provider returned an invalid response")
            response = cast(ModelResponse, raw)
            self._validate_response(request, response)
            await self._emit(
                MODEL_INVOCATION_COMPLETED,
                context,
                {
                    "operation": ModelOperation.GENERATE.value,
                    "provider_id": response.provider_id.value,
                    "model_id": response.model_id.value,
                    "finish_reason": response.finish_reason.value,
                    "input_units": response.usage.input_units,
                    "output_units": response.usage.output_units,
                    "latency_ms": self._elapsed_ms(started_at),
                    "outcome": "completed",
                },
            )
            return response
        except CoreError as error:
            await self._emit_failure(
                context,
                ModelOperation.GENERATE,
                error.detail,
                self._elapsed_ms(started_at),
            )
            raise

    async def stream(
        self, request: ModelRequest, context: RuntimeCallContext
    ) -> AsyncIterator[ModelEvent]:
        started_at = self._clock.now()
        provider = self._providers.get(request.selector.provider_id)
        access = self._access_request(
            request.selector, context, MODEL_STREAM_ACTION, self._constraints(request)
        )
        iterator: AsyncIterator[ModelEvent] | None = None
        terminal = False
        started = False
        try:

            async def operation(call: AuthorizedCall) -> object:
                nonlocal iterator
                constrained = self._constrain_request(request, call)
                iterator = provider.stream(constrained, call.context)
                return iterator

            raw = await self._dispatcher.dispatch(access, operation)
            iterator = cast(AsyncIterator[ModelEvent], raw)
            await self._emit(
                MODEL_INVOCATION_STARTED,
                context,
                {
                    "operation": ModelOperation.STREAM.value,
                    "provider_id": request.selector.provider_id.value,
                    "model_id": request.selector.model_id.value,
                },
            )
            while True:
                try:
                    event = await await_provider(anext(iterator), context, self._clock)
                except StopAsyncIteration:
                    if not terminal:
                        detail = self._protocol_detail(
                            "model_stream_missing_terminal",
                            "model stream ended without a terminal event",
                        )
                        await self._emit_failure(
                            context,
                            ModelOperation.STREAM,
                            detail,
                            self._elapsed_ms(started_at),
                        )
                        yield ModelEvent.failure(detail)
                    return
                except CoreError as error:
                    await self._emit_failure(
                        context,
                        ModelOperation.STREAM,
                        error.detail,
                        self._elapsed_ms(started_at),
                    )
                    yield ModelEvent.failure(error.detail)
                    return
                except Exception as error:
                    detail = self._provider_failure(error).detail
                    await self._emit_failure(
                        context,
                        ModelOperation.STREAM,
                        detail,
                        self._elapsed_ms(started_at),
                    )
                    yield ModelEvent.failure(detail)
                    return
                if not started:
                    if event.type is not ModelEventType.START:
                        detail = self._protocol_detail(
                            "model_stream_missing_start",
                            "model stream must begin with a start event",
                        )
                        await self._emit_failure(
                            context,
                            ModelOperation.STREAM,
                            detail,
                            self._elapsed_ms(started_at),
                        )
                        yield ModelEvent.failure(detail)
                        return
                    started = True
                if event.type is ModelEventType.COMPLETION:
                    if event.response is None:
                        raise AssertionError("completion event requires response")
                    self._validate_response(request, event.response)
                    terminal = True
                    await self._emit(
                        MODEL_INVOCATION_COMPLETED,
                        context,
                        {
                            "operation": ModelOperation.STREAM.value,
                            "provider_id": event.response.provider_id.value,
                            "model_id": event.response.model_id.value,
                            "finish_reason": event.response.finish_reason.value,
                            "input_units": event.response.usage.input_units,
                            "output_units": event.response.usage.output_units,
                            "latency_ms": self._elapsed_ms(started_at),
                            "outcome": "completed",
                        },
                    )
                    yield event
                    return
                if event.type is ModelEventType.FAILURE:
                    if event.error is None:
                        raise AssertionError("failure event requires error")
                    terminal = True
                    await self._emit_failure(
                        context,
                        ModelOperation.STREAM,
                        event.error,
                        self._elapsed_ms(started_at),
                    )
                    yield event
                    return
                yield event
        except CoreError as error:
            if not terminal:
                await self._emit_failure(
                    context,
                    ModelOperation.STREAM,
                    error.detail,
                    self._elapsed_ms(started_at),
                )
                yield ModelEvent.failure(error.detail)
        finally:
            if isinstance(iterator, _AsyncClosable):
                await iterator.aclose()

    def _validate_response(
        self, request: ModelRequest, response: ModelResponse
    ) -> None:
        if (
            response.provider_id != request.selector.provider_id
            or response.model_id != request.selector.model_id
        ):
            self._protocol_failure("model response identity does not match request")
        if request.output_schema is not None:
            if response.structured_output is None:
                self._protocol_failure(
                    "structured output was requested but not returned"
                )
            self._schemas.validate(request.output_schema, response.structured_output)
        if response.tool_requests and not request.policy.allow_tool_requests:
            self._protocol_failure(
                "model returned tool requests when they were disabled"
            )
        allowed_tools = {item.key for item in request.tools}
        if any(item.tool.key not in allowed_tools for item in response.tool_requests):
            self._protocol_failure("model proposed an undeclared tool")
        if (
            request.budget.max_input_units is not None
            and response.usage.input_units > request.budget.max_input_units
        ) or (
            request.budget.max_output_units is not None
            and response.usage.output_units > request.budget.max_output_units
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "model_budget_exceeded",
                "model response exceeds the request budget",
            )
        if (
            response.finish_reason is ModelFinishReason.PARTIAL
            and not request.policy.allow_partial
        ):
            raise core_error(
                ErrorCategory.PARTIAL_RESULT,
                "model_partial_result_rejected",
                "model returned a partial result that policy rejects",
            )

    def _constrain_request(
        self, request: ModelRequest, call: AuthorizedCall
    ) -> ModelRequest:
        constraints = call.grant.constraints
        allowed = {"model", "tools", "max_input_units", "max_output_units"}
        if set(constraints).difference(allowed):
            self._invalid_grant("model grant contains unknown constraints")
        raw_model = constraints.get("model")
        if raw_model is not None and raw_model != request.selector.model_id.value:
            self._invalid_grant("model grant changes the requested model")
        tools = request.tools
        raw_tools = constraints.get("tools")
        if raw_tools is not None:
            if not isinstance(raw_tools, list) or not all(
                isinstance(item, str) for item in raw_tools
            ):
                self._invalid_grant("model tool constraint is invalid")
            names = cast(list[str], raw_tools)
            requested = {self._resource_name(item) for item in tools}
            if not set(names).issubset(requested):
                self._invalid_grant("model grant broadens requested tools")
            tools = tuple(item for item in tools if self._resource_name(item) in names)
        budget = ModelBudget(
            max_input_units=self._narrow_limit(
                "max_input_units",
                request.budget.max_input_units,
                constraints.get("max_input_units"),
            ),
            max_output_units=self._narrow_limit(
                "max_output_units",
                request.budget.max_output_units,
                constraints.get("max_output_units"),
            ),
        )
        return replace(request, tools=tools, budget=budget)

    def _validate_capability_grant(
        self, selector: ModelSelector, call: AuthorizedCall
    ) -> None:
        constraints = call.grant.constraints
        if set(constraints).difference({"model"}):
            self._invalid_grant("model capability grant contains unknown constraints")
        raw_model = constraints.get("model")
        if raw_model is not None and raw_model != selector.model_id.value:
            self._invalid_grant("model grant changes the requested model")

    def _narrow_limit(
        self, name: str, requested: int | None, raw_granted: JsonValue | None
    ) -> int | None:
        if raw_granted is None:
            return requested
        if isinstance(raw_granted, bool) or not isinstance(raw_granted, int):
            self._invalid_grant(f"model {name} constraint is invalid")
        granted = cast(int, raw_granted)
        if granted < 1 or (requested is not None and granted > requested):
            self._invalid_grant(f"model {name} grant broadens the request")
        return granted

    def _constraints(self, request: ModelRequest) -> Mapping[str, JsonValue]:
        return {
            "model": request.selector.model_id.value,
            "tools": [self._resource_name(item) for item in request.tools],
            "max_input_units": request.budget.max_input_units,
            "max_output_units": request.budget.max_output_units,
        }

    def _access_request(
        self,
        selector: ModelSelector,
        context: RuntimeCallContext,
        action: ActionRef,
        constraints: Mapping[str, JsonValue],
    ) -> AccessRequest:
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core",
                "model",
                ResourceId(f"{selector.provider_id.value}:{selector.model_id.value}"),
            ),
            scope=context.scope,
            context=context,
            constraints=constraints,
        )

    async def _emit_failure(
        self,
        context: RuntimeCallContext,
        operation: ModelOperation,
        error: ErrorDetail,
        latency_ms: int,
    ) -> None:
        await self._emit(
            MODEL_INVOCATION_FAILED,
            context,
            {
                "operation": operation.value,
                "category": error.category.value,
                "error_code": error.code,
                "latency_ms": latency_ms,
                "outcome": "failed",
            },
        )

    async def _emit(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with suppress(Exception):
            await self._events.provider_event(event_type, context, payload)

    def _provider_failure(self, error: Exception) -> CoreError:
        return core_error(
            ErrorCategory.UNAVAILABLE,
            "model_provider_failure",
            "model provider failed",
            retryable=True,
            cause_id=type(error).__name__,
        )

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((self._clock.now() - started_at).total_seconds() * 1_000))

    def _protocol_detail(self, code: str, message: str) -> ErrorDetail:
        return ErrorDetail(ErrorCategory.PROTOCOL_FAILURE, code, message)

    def _protocol_failure(self, message: str) -> None:
        raise core_error(
            ErrorCategory.PROTOCOL_FAILURE,
            "model_provider_protocol_failure",
            message,
        )

    def _invalid_grant(self, message: str) -> None:
        raise core_error(ErrorCategory.DENIED, "invalid_grant", message)

    def _resource_name(self, resource: ResourceRef) -> str:
        return ":".join(resource.key)
