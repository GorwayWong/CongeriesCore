from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from congeries_core.policy.authorization import ResourceRef
from congeries_core.provider.model import (
    MODEL_CAPABILITIES_ACTION,
    MODEL_GENERATE_ACTION,
    ModelBinding,
    ModelBudget,
    ModelCapabilities,
    ModelCapabilityRequirements,
    ModelEvent,
    ModelEventType,
    ModelFinishReason,
    ModelGateway,
    ModelOperation,
    ModelProviderRegistry,
    ModelRequest,
    ModelRequestPolicy,
    ModelResponse,
    ModelSelector,
    ModelUsage,
    ToolCallProposal,
)
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, ErrorDetail
from congeries_core.runtime.ids import (
    ModelBindingRef,
    ModelId,
    ProviderId,
    ResourceId,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from .provider_support import (
    AlternateFakeModelProvider,
    FakeModelProvider,
    ProviderEventRecorder,
    RecordingPolicy,
    StringObjectValidator,
    authorized_dispatcher,
)
from .support import NOW, FixedClock, call_context

OUTPUT_SCHEMA = SchemaRef("test", "output", "1")


def selector(provider: str = "model-provider", model: str = "model-a") -> ModelSelector:
    return ModelSelector(ProviderId(provider), ModelId(model))


def capabilities(actual_selector: ModelSelector) -> ModelCapabilities:
    return ModelCapabilities(
        selector=actual_selector,
        operations=frozenset({ModelOperation.GENERATE, ModelOperation.STREAM}),
        structured_output=True,
        tool_calls=True,
        input_kinds=frozenset({ContentKind.TEXT, ContentKind.JSON}),
        output_kinds=frozenset({ContentKind.TEXT, ContentKind.JSON}),
        maximum_budget=ModelBudget(1_000, 1_000),
        usage_reporting=True,
        contract_version="1",
    )


def response(
    actual_selector: ModelSelector,
    *,
    structured: object | None = None,
    finish: ModelFinishReason = ModelFinishReason.STOP,
    tools: tuple[ToolCallProposal, ...] = (),
) -> ModelResponse:
    return ModelResponse(
        output=(ContentBlock.text("answer"),),
        structured_output=structured,
        tool_requests=tools,
        finish_reason=finish,
        usage=ModelUsage(3, 2),
        provider_id=actual_selector.provider_id,
        model_id=actual_selector.model_id,
        warnings=("fake warning",),
        provenance=("fake-model",),
    )


def request(
    actual_selector: ModelSelector,
    *,
    output_schema: SchemaRef | None = None,
    tools: tuple[ResourceRef, ...] = (),
    budget: ModelBudget | None = None,
) -> ModelRequest:
    return ModelRequest(
        selector=actual_selector,
        input=(ContentBlock.text("question"),),
        output_schema=output_schema,
        tools=tools,
        policy=ModelRequestPolicy(allow_tool_requests=bool(tools)),
        budget=budget or ModelBudget(100, 100),
    )


def gateway(
    providers: tuple[tuple[ProviderId, FakeModelProvider], ...],
    *,
    policy: RecordingPolicy | None = None,
    schemas: SchemaRegistry | None = None,
    events: ProviderEventRecorder | None = None,
) -> ModelGateway:
    registry = ModelProviderRegistry()
    for provider_id, provider in providers:
        registry.register(provider_id, provider)
    return ModelGateway(
        providers=registry,
        schemas=schemas or SchemaRegistry(),
        dispatcher=authorized_dispatcher(policy or RecordingPolicy()),
        clock=FixedClock(),
        events=events,
    )


def test_model_value_models_and_capability_binding() -> None:
    primary = selector()
    fallback = selector("backup", "model-b")
    binding = ModelBinding(
        ModelBindingRef("binding-1"),
        primary,
        required=ModelCapabilityRequirements(
            streaming=True,
            structured_output=True,
            input_kinds=frozenset({ContentKind.TEXT}),
        ),
        fallbacks=(fallback,),
    )
    assert binding.selectors == (primary, fallback)
    assert ModelBinding.from_data(binding.to_data()) == binding
    assert ModelSelector.from_data(primary.to_data()) == primary
    model_capabilities = capabilities(primary)
    assert (
        ModelCapabilities.from_data(model_capabilities.to_data()) == model_capabilities
    )
    assert model_capabilities.satisfies(binding.required)
    model_response = response(primary)
    assert ModelResponse.from_data(model_response.to_data()) == model_response
    assert ModelUsage(2, 3).total_units == 5
    with pytest.raises(ValueError):
        ModelBudget(max_output_units=0)
    with pytest.raises(ValueError):
        ModelUsage(-1, 0)
    with pytest.raises(ValueError):
        replace(binding, fallbacks=(primary,))
    with pytest.raises(ValueError):
        ModelRequest(primary, ())
    with pytest.raises(ValueError):
        ModelResponse(
            (),
            ModelFinishReason.STOP,
            ModelUsage(0, 0),
            primary.provider_id,
            primary.model_id,
        )

    assert ModelEvent.start().type is ModelEventType.START
    assert ModelEvent.completion(response(primary)).type.terminal
    failure = ModelEvent.failure(ErrorDetail(ErrorCategory.UNAVAILABLE, "x", "x"))
    assert failure.error is not None
    assert ModelEvent.from_data(failure.to_data()) == failure
    delta = ModelEvent(ModelEventType.STRUCTURED_DELTA, structured_delta={"x": 1})
    assert ModelEvent.from_data(delta.to_data()) == delta
    with pytest.raises(ValueError):
        ModelEvent(ModelEventType.CONTENT_DELTA)
    with pytest.raises(ValueError):
        ModelEvent(ModelEventType.START, content=ContentBlock.text("unexpected"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_type", [FakeModelProvider, AlternateFakeModelProvider]
)
async def test_model_provider_generate_capabilities_and_stream_contract(
    provider_type: type[FakeModelProvider],
) -> None:
    provider_name = provider_type.__name__.lower()
    actual_selector = selector(provider_name)
    final = response(actual_selector)
    provider = provider_type(
        capabilities(actual_selector),
        final,
        (
            ModelEvent.start(),
            ModelEvent(ModelEventType.CONTENT_DELTA, content=ContentBlock.text("a")),
            ModelEvent(ModelEventType.USAGE_UPDATE, usage=ModelUsage(2, 1)),
            ModelEvent.completion(final),
            ModelEvent(ModelEventType.CONTENT_DELTA, content=ContentBlock.text("late")),
        ),
    )
    events = ProviderEventRecorder()
    model_gateway = gateway(((actual_selector.provider_id, provider),), events=events)

    discovered = await model_gateway.capabilities(actual_selector, call_context())
    generated = await model_gateway.generate(request(actual_selector), call_context())
    streamed = [
        event
        async for event in model_gateway.stream(
            request(actual_selector), call_context()
        )
    ]

    assert discovered.selector == actual_selector
    assert generated == final
    assert [event.type for event in streamed] == [
        ModelEventType.START,
        ModelEventType.CONTENT_DELTA,
        ModelEventType.USAGE_UPDATE,
        ModelEventType.COMPLETION,
    ]
    assert provider.stream_closed
    assert sum(event.type.terminal for event in streamed) == 1
    assert [event_type for event_type, _ in events.events].count(
        "core.model.invocation_completed"
    ) == 2


@pytest.mark.asyncio
async def test_model_authorization_grant_narrowing_and_no_bypass() -> None:
    actual_selector = selector("secure")
    tool_a = ResourceRef("core", "tool", ResourceId("a"))
    tool_b = ResourceRef("core", "tool", ResourceId("b"))
    proposal = ToolCallProposal("call-1", tool_a, {"value": 1})
    provider = FakeModelProvider(
        capabilities(actual_selector),
        response(actual_selector, tools=(proposal,)),
        (),
    )
    denied_policy = RecordingPolicy(denied_actions={MODEL_CAPABILITIES_ACTION.name})
    with pytest.raises(CoreError) as denied:
        await gateway(
            ((actual_selector.provider_id, provider),), policy=denied_policy
        ).capabilities(actual_selector, call_context())
    assert denied.value.detail.category is ErrorCategory.DENIED
    assert provider.capability_calls == 0

    unknown_provider = FakeModelProvider(
        capabilities(actual_selector), response(actual_selector), ()
    )
    unknown = ModelGateway(
        providers=_model_registry(actual_selector.provider_id, unknown_provider),
        schemas=SchemaRegistry(),
        dispatcher=authorized_dispatcher(RecordingPolicy(), known_actions=False),
        clock=FixedClock(),
    )
    with pytest.raises(CoreError) as rejected:
        await unknown.generate(request(actual_selector), call_context())
    assert rejected.value.detail.code == "unknown_action"
    assert not unknown_provider.generate_calls

    invalid_capability_provider = FakeModelProvider(
        capabilities(actual_selector), response(actual_selector), ()
    )
    with pytest.raises(CoreError) as invalid_capability_grant:
        await gateway(
            ((actual_selector.provider_id, invalid_capability_provider),),
            policy=RecordingPolicy(constraints={"unknown": True}),
        ).capabilities(actual_selector, call_context())
    assert invalid_capability_grant.value.detail.code == "invalid_grant"
    assert invalid_capability_provider.capability_calls == 0

    constrained = RecordingPolicy(
        constraints={
            "model": actual_selector.model_id.value,
            "tools": ["core:tool:a"],
            "max_output_units": 10,
        }
    )
    generated = await gateway(
        ((actual_selector.provider_id, provider),), policy=constrained
    ).generate(
        request(
            actual_selector,
            tools=(tool_a, tool_b),
            budget=ModelBudget(100, 20),
        ),
        call_context(),
    )
    assert generated.tool_requests == (proposal,)
    constrained_request = provider.generate_calls[-1]
    assert constrained_request.tools == (tool_a,)
    assert constrained_request.budget.max_output_units == 10
    assert constrained.requests[-1].action == MODEL_GENERATE_ACTION

    broad = RecordingPolicy(constraints={"tools": ["core:tool:not-requested"]})
    with pytest.raises(CoreError) as invalid_grant:
        await gateway(
            ((actual_selector.provider_id, provider),), policy=broad
        ).generate(request(actual_selector, tools=(tool_a,)), call_context())
    assert invalid_grant.value.detail.code == "invalid_grant"


@pytest.mark.asyncio
async def test_structured_output_budget_partial_and_provider_failures() -> None:
    actual_selector = selector()
    schemas = SchemaRegistry()
    schemas.register(OUTPUT_SCHEMA, StringObjectValidator())
    valid = FakeModelProvider(
        capabilities(actual_selector),
        response(actual_selector, structured={"value": "valid"}),
        (),
    )
    generated = await gateway(
        ((actual_selector.provider_id, valid),), schemas=schemas
    ).generate(request(actual_selector, output_schema=OUTPUT_SCHEMA), call_context())
    assert generated.structured_output == {"value": "valid"}

    invalid = FakeModelProvider(
        capabilities(actual_selector),
        response(actual_selector, structured={"value": 1}),
        (),
    )
    with pytest.raises(CoreError) as schema_error:
        await gateway(
            ((actual_selector.provider_id, invalid),), schemas=schemas
        ).generate(
            request(actual_selector, output_schema=OUTPUT_SCHEMA), call_context()
        )
    assert schema_error.value.detail.code == "schema_validation_failed"

    partial = FakeModelProvider(
        capabilities(actual_selector),
        response(actual_selector, finish=ModelFinishReason.PARTIAL),
        (),
    )
    with pytest.raises(CoreError) as partial_error:
        await gateway(((actual_selector.provider_id, partial),)).generate(
            request(actual_selector), call_context()
        )
    assert partial_error.value.detail.category is ErrorCategory.PARTIAL_RESULT

    excessive = FakeModelProvider(
        capabilities(actual_selector),
        replace(response(actual_selector), usage=ModelUsage(3, 20)),
        (),
    )
    with pytest.raises(CoreError) as budget_error:
        await gateway(((actual_selector.provider_id, excessive),)).generate(
            request(actual_selector, budget=ModelBudget(5, 5)), call_context()
        )
    assert budget_error.value.detail.code == "model_budget_exceeded"

    failed = FakeModelProvider(
        capabilities(actual_selector), RuntimeError("vendor failure"), ()
    )
    with pytest.raises(CoreError) as normalized:
        await gateway(((actual_selector.provider_id, failed),)).generate(
            request(actual_selector), call_context()
        )
    assert normalized.value.detail.code == "model_provider_failure"


@pytest.mark.asyncio
async def test_model_stream_normalizes_terminal_failures_and_cancellation() -> None:
    actual_selector = selector()
    missing_terminal = FakeModelProvider(
        capabilities(actual_selector), response(actual_selector), (ModelEvent.start(),)
    )
    events = [
        item
        async for item in gateway(
            ((actual_selector.provider_id, missing_terminal),)
        ).stream(request(actual_selector), call_context())
    ]
    assert [item.type for item in events] == [
        ModelEventType.START,
        ModelEventType.FAILURE,
    ]
    assert events[-1].error is not None
    assert events[-1].error.code == "model_stream_missing_terminal"

    bad_start = FakeModelProvider(
        capabilities(actual_selector),
        response(actual_selector),
        (ModelEvent(ModelEventType.CONTENT_DELTA, content=ContentBlock.text("x")),),
    )
    bad_events = [
        item
        async for item in gateway(((actual_selector.provider_id, bad_start),)).stream(
            request(actual_selector), call_context()
        )
    ]
    assert bad_events[0].type is ModelEventType.FAILURE
    assert bad_events[0].error is not None
    assert bad_events[0].error.code == "model_stream_missing_start"

    started = asyncio.Event()
    closed = asyncio.Event()

    class SlowStreamProvider(FakeModelProvider):
        async def stream(self, request: ModelRequest, context: object):
            del context
            self.stream_calls.append(request)
            try:
                yield ModelEvent.start()
                started.set()
                await asyncio.Future[None]()
            finally:
                closed.set()

    slow = SlowStreamProvider(
        capabilities(actual_selector), response(actual_selector), ()
    )
    context = call_context()
    collected: list[ModelEvent] = []

    async def consume() -> None:
        async for item in gateway(((actual_selector.provider_id, slow),)).stream(
            request(actual_selector), context
        ):
            collected.append(item)

    task = asyncio.create_task(consume())
    await started.wait()
    context.cancellation.cancel()
    await task
    assert collected[-1].type is ModelEventType.FAILURE
    assert collected[-1].error is not None
    assert collected[-1].error.category is ErrorCategory.CANCELLED
    await closed.wait()

    expired = replace(call_context(), deadline=Deadline(NOW - timedelta(seconds=1)))
    never_called = FakeModelProvider(
        capabilities(actual_selector), response(actual_selector), ()
    )
    with pytest.raises(CoreError) as timeout:
        await gateway(((actual_selector.provider_id, never_called),)).generate(
            request(actual_selector), expired
        )
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT
    assert not never_called.generate_calls


def _model_registry(
    provider_id: ProviderId, provider: FakeModelProvider
) -> ModelProviderRegistry:
    registry = ModelProviderRegistry()
    registry.register(provider_id, provider)
    return registry
