from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from congeries_core.provider.context import (
    CONTEXT_CAPABILITIES_ACTION,
    CONTEXT_PROVIDE_ACTION,
    ContextBinding,
    ContextBudget,
    ContextCapabilities,
    ContextCompleteness,
    ContextCompletenessPolicy,
    ContextEntry,
    ContextKey,
    ContextMergeRegistry,
    ContextMergeStrategy,
    ContextProviderRegistry,
    ContextRequest,
    ContextRequirement,
    ContextResolver,
    ContextResult,
    ContextScopePattern,
    ContextUsage,
    ContextWarning,
)
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import DefinitionId, ProviderId
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from .provider_support import (
    FakeContextProvider,
    ProviderEventRecorder,
    RecordingPolicy,
    StringObjectValidator,
    SumMergePolicy,
    authorized_dispatcher,
)
from .support import NOW, FixedClock, call_context

SCHEMA = SchemaRef("test", "context", "1")
KEY = ContextKey("agent", "profile")
REQUIREMENT = ContextRequirement(KEY, SCHEMA)


class AcceptValidator:
    def validate(self, value: object) -> None:
        del value


def capabilities(
    provider_id: ProviderId,
    *requirements: ContextRequirement,
    supports_partial: bool = True,
) -> ContextCapabilities:
    return ContextCapabilities(
        provider_id=provider_id,
        contract_version="1",
        supported=tuple(requirements),
        supports_partial=supports_partial,
        maximum_budget=ContextBudget(10_000, 1_000),
        scope_patterns=(ContextScopePattern("core", "workspace"),),
    )


def result(
    provider_id: ProviderId,
    value: object | None = None,
    *,
    partial: bool = False,
) -> ContextResult:
    entries = () if partial else (ContextEntry(KEY, SCHEMA, value, ("fake",)),)
    return ContextResult(
        provider_id=provider_id,
        contract_version="1",
        entries=entries,
        completeness=(
            ContextCompleteness.PARTIAL if partial else ContextCompleteness.COMPLETE
        ),
        missing_keys=(KEY,) if partial else (),
        warnings=(ContextWarning("fake_warning", "fake warning", KEY),),
        usage=ContextUsage(8, 2),
    )


def resolver(
    providers: tuple[tuple[ProviderId, FakeContextProvider], ...],
    *,
    policy: RecordingPolicy | None = None,
    schemas: SchemaRegistry | None = None,
    merges: ContextMergeRegistry | None = None,
    events: ProviderEventRecorder | None = None,
    clock: FixedClock | None = None,
) -> ContextResolver:
    registry = ContextProviderRegistry()
    for provider_id, provider in providers:
        registry.register(provider_id, provider)
    actual_schemas = schemas or SchemaRegistry()
    if not actual_schemas.contains(SCHEMA):
        actual_schemas.register(SCHEMA, AcceptValidator())
    return ContextResolver(
        providers=registry,
        schemas=actual_schemas,
        merges=merges or ContextMergeRegistry(),
        dispatcher=authorized_dispatcher(policy or RecordingPolicy()),
        clock=clock or FixedClock(),
        events=events,
    )


def binding(
    provider_ids: tuple[ProviderId, ...], strategy: ContextMergeStrategy
) -> ContextBinding:
    return ContextBinding(
        provider_ids,
        (REQUIREMENT,),
        merge_strategy=strategy,
        completeness_policy=ContextCompletenessPolicy.ALLOW_PARTIAL,
    )


def request(actual_binding: ContextBinding, *, context=None) -> ContextRequest:
    return actual_binding.request(
        DefinitionId("definition-1"), context or call_context()
    )


def test_shared_content_schema_and_context_value_models() -> None:
    blocks = (
        ContentBlock.text("hello", name="prompt"),
        ContentBlock.json({"value": "ok"}),
        ContentBlock.reference("artifact:1", media_type="application/test"),
    )
    assert tuple(ContentBlock.from_data(item.to_data()) for item in blocks) == blocks
    assert {item.kind for item in blocks} == {
        ContentKind.TEXT,
        ContentKind.JSON,
        ContentKind.REFERENCE,
    }
    with pytest.raises(ValueError):
        ContentBlock(ContentKind.TEXT, {"not": "text"})
    with pytest.raises(ValueError):
        ContentBlock.text("ok", name=" bad")

    assert SchemaRef.from_data(SCHEMA.to_data()) == SCHEMA
    for invalid in (("Bad", "name", "1"), ("test", "Bad", "1"), ("test", "x", " ")):
        with pytest.raises(ValueError):
            SchemaRef(*invalid)
    schemas = SchemaRegistry()
    schemas.register(SCHEMA, StringObjectValidator())
    assert schemas.contains(SCHEMA)
    schemas.validate(SCHEMA, {"value": "yes"})
    with pytest.raises(CoreError) as invalid_value:
        schemas.validate(SCHEMA, {"value": 1})
    assert invalid_value.value.detail.code == "schema_validation_failed"
    with pytest.raises(CoreError):
        schemas.register(SCHEMA, StringObjectValidator())
    with pytest.raises(CoreError) as missing:
        SchemaRegistry().validate(SCHEMA, {})
    assert missing.value.detail.category is ErrorCategory.VERSION_MISMATCH

    assert ContextKey.from_data(KEY.to_data()) == KEY
    assert ContextBudget.from_data(ContextBudget(10, 5).to_data()) == ContextBudget(
        10, 5
    )
    context_result = result(ProviderId("p"), {"value": "x"})
    assert ContextResult.from_data(context_result.to_data()) == context_result
    context_capabilities = capabilities(ProviderId("p"), REQUIREMENT)
    assert (
        ContextCapabilities.from_data(context_capabilities.to_data())
        == context_capabilities
    )
    context_binding = binding((ProviderId("p"),), ContextMergeStrategy.SINGLE)
    assert ContextBinding.from_data(context_binding.to_data()) == context_binding
    timed_entry = ContextEntry(
        KEY,
        SCHEMA,
        {"value": "fresh"},
        ("source",),
        fresh_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    assert ContextEntry.from_data(timed_entry.to_data()) == timed_entry
    with pytest.raises(ValueError):
        ContextBudget(0)
    with pytest.raises(ValueError):
        ContextResult(
            ProviderId("p"),
            "1",
            (),
            ContextCompleteness.COMPLETE,
            missing_keys=(KEY,),
        )
    with pytest.raises(ValueError):
        ContextWarning("", "message")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "expected_values", "expected_calls"),
    [
        (ContextMergeStrategy.SINGLE, (1,), (1, 0)),
        (ContextMergeStrategy.FIRST_SUCCESS, (2,), (1, 1)),
        (ContextMergeStrategy.MERGE, (3,), (1, 1)),
        (ContextMergeStrategy.ALL, (1, 2), (1, 1)),
    ],
)
async def test_context_resolution_strategies(
    strategy: ContextMergeStrategy,
    expected_values: tuple[int, ...],
    expected_calls: tuple[int, int],
) -> None:
    first_id, second_id = ProviderId("first"), ProviderId("second")
    first_result: ContextResult | Exception = result(first_id, 1)
    provider_ids = (first_id, second_id)
    if strategy is ContextMergeStrategy.SINGLE:
        provider_ids = (first_id,)
    elif strategy is ContextMergeStrategy.FIRST_SUCCESS:
        first_result = core_error(
            ErrorCategory.UNAVAILABLE, "first_unavailable", "first unavailable"
        )
    first = FakeContextProvider(capabilities(first_id, REQUIREMENT), first_result)
    second = FakeContextProvider(
        capabilities(second_id, REQUIREMENT), result(second_id, 2)
    )
    merges = ContextMergeRegistry()
    merges.register(SCHEMA, SumMergePolicy())
    events = ProviderEventRecorder()
    actual_binding = binding(provider_ids, strategy)

    resolved = await resolver(
        ((first_id, first), (second_id, second)), merges=merges, events=events
    ).resolve(request(actual_binding), actual_binding)

    assert tuple(entry.value for entry in resolved.entries) == expected_values
    assert (len(first.provide_calls), len(second.provide_calls)) == expected_calls
    assert resolved.completeness is ContextCompleteness.COMPLETE
    assert events.events[0][0].endswith("resolution_started")
    assert events.events[-1][0].endswith("resolution_completed")


@pytest.mark.asyncio
async def test_context_authorization_constraints_and_no_bypass() -> None:
    provider_id = ProviderId("secure")
    provider = FakeContextProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, {"value": "ok"})
    )
    actual_binding = binding((provider_id,), ContextMergeStrategy.SINGLE)

    denied_policy = RecordingPolicy(denied_actions={CONTEXT_CAPABILITIES_ACTION.name})
    with pytest.raises(CoreError) as denied:
        await resolver(((provider_id, provider),), policy=denied_policy).resolve(
            request(actual_binding), actual_binding
        )
    assert denied.value.detail.category is ErrorCategory.DENIED
    assert provider.capability_calls == 0
    assert not provider.provide_calls

    unknown_provider = FakeContextProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, {"value": "ok"})
    )
    registry = ContextProviderRegistry()
    registry.register(provider_id, unknown_provider)
    schemas = SchemaRegistry()
    schemas.register(SCHEMA, AcceptValidator())
    unknown = ContextResolver(
        providers=registry,
        schemas=schemas,
        merges=ContextMergeRegistry(),
        dispatcher=authorized_dispatcher(RecordingPolicy(), known_actions=False),
        clock=FixedClock(),
    )
    with pytest.raises(CoreError) as rejected:
        await unknown.resolve(request(actual_binding), actual_binding)
    assert rejected.value.detail.code == "unknown_action"
    assert unknown_provider.capability_calls == 0

    invalid_capability_provider = FakeContextProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, {"value": "ok"})
    )
    with pytest.raises(CoreError) as invalid_capability_grant:
        await resolver(
            ((provider_id, invalid_capability_provider),),
            policy=RecordingPolicy(constraints={"unknown": True}),
        ).resolve(request(actual_binding), actual_binding)
    assert invalid_capability_grant.value.detail.code == "invalid_grant"
    assert invalid_capability_provider.capability_calls == 0

    constrained_provider = FakeContextProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, {"value": "ok"})
    )
    constrained = RecordingPolicy(
        constraints={"keys": [KEY.wire_name], "max_bytes": 100}
    )
    resolved = await resolver(
        ((provider_id, constrained_provider),), policy=constrained
    ).resolve(request(actual_binding), actual_binding)
    assert resolved.entries[0].value == {"value": "ok"}
    assert constrained_provider.provide_calls[0].budget.max_bytes == 100
    assert [item.action for item in constrained.requests] == [
        CONTEXT_CAPABILITIES_ACTION,
        CONTEXT_PROVIDE_ACTION,
    ]


@pytest.mark.asyncio
async def test_context_partial_conflict_budget_schema_and_identity_failures() -> None:
    first_id, second_id = ProviderId("first"), ProviderId("second")
    first = FakeContextProvider(
        capabilities(first_id, REQUIREMENT), result(first_id, 1)
    )
    second = FakeContextProvider(
        capabilities(second_id, REQUIREMENT), result(second_id, 2)
    )
    events = ProviderEventRecorder(fail=True)
    merge_binding = binding((first_id, second_id), ContextMergeStrategy.MERGE)
    with pytest.raises(CoreError) as conflict:
        await resolver(((first_id, first), (second_id, second)), events=events).resolve(
            request(merge_binding), merge_binding
        )
    assert conflict.value.detail.code == "context_merge_conflict"

    partial_provider = FakeContextProvider(
        capabilities(first_id, REQUIREMENT), result(first_id, partial=True)
    )
    partial_binding = replace(
        binding((first_id,), ContextMergeStrategy.SINGLE),
        completeness_policy=ContextCompletenessPolicy.REQUIRE_COMPLETE,
    )
    partial = await resolver(((first_id, partial_provider),)).resolve(
        request(partial_binding), partial_binding
    )
    assert partial.completeness is ContextCompleteness.PARTIAL

    wrong = FakeContextProvider(
        capabilities(first_id, REQUIREMENT), result(second_id, {"value": "wrong"})
    )
    with pytest.raises(CoreError) as identity:
        await resolver(((first_id, wrong),)).resolve(
            request(binding((first_id,), ContextMergeStrategy.SINGLE)),
            binding((first_id,), ContextMergeStrategy.SINGLE),
        )
    assert identity.value.detail.code == "context_provider_identity_mismatch"

    budget_binding = replace(
        binding((first_id,), ContextMergeStrategy.SINGLE),
        budget=ContextBudget(max_bytes=1),
    )
    budget_provider = FakeContextProvider(
        capabilities(first_id, REQUIREMENT), result(first_id, "too large")
    )
    with pytest.raises(CoreError) as budget_error:
        await resolver(((first_id, budget_provider),)).resolve(
            request(budget_binding), budget_binding
        )
    assert budget_error.value.detail.code == "context_byte_budget_exceeded"


@pytest.mark.asyncio
async def test_context_deadline_and_active_cancellation_stop_provider_work() -> None:
    provider_id = ProviderId("slow")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowProvider(FakeContextProvider):
        async def provide(self, request: ContextRequest) -> ContextResult:
            self.provide_calls.append(request)
            started.set()
            try:
                await asyncio.Future[None]()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    slow = SlowProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, "late")
    )
    actual_binding = binding((provider_id,), ContextMergeStrategy.SINGLE)
    context = call_context()
    task = asyncio.create_task(
        resolver(((provider_id, slow),)).resolve(
            request(actual_binding, context=context), actual_binding
        )
    )
    await started.wait()
    context.cancellation.cancel()
    with pytest.raises(CoreError) as cancellation:
        await task
    assert cancellation.value.detail.category is ErrorCategory.CANCELLED
    await cancelled.wait()

    expired_context = replace(
        call_context(), deadline=Deadline(NOW - timedelta(seconds=1))
    )
    never_called = FakeContextProvider(
        capabilities(provider_id, REQUIREMENT), result(provider_id, "late")
    )
    with pytest.raises(CoreError) as timeout:
        await resolver(((provider_id, never_called),)).resolve(
            request(actual_binding, context=expired_context), actual_binding
        )
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT
    assert never_called.capability_calls == 0
