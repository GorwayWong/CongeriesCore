from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from congeries_core.harness import (
    AgentExecutionResult,
    AgentRegistry,
    AgentRuntime,
    AgentSpec,
)
from congeries_core.policy.authorization import ActionRegistry, AuthorizedDispatcher
from congeries_core.provider import provider_actions
from congeries_core.provider.context import (
    CONTEXT_CAPABILITIES_ACTION,
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
    ContextRequirement,
    ContextResolver,
    ContextResult,
    ContextUsage,
)
from congeries_core.provider.model import (
    MODEL_GENERATE_ACTION,
    ModelBinding,
    ModelBudget,
    ModelCapabilities,
    ModelEvent,
    ModelFinishReason,
    ModelGateway,
    ModelOperation,
    ModelProviderRegistry,
    ModelRequest,
    ModelResponse,
    ModelSelector,
    ModelUsage,
)
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, ErrorDetail
from congeries_core.runtime.ids import ModelId, ProviderId, RunId
from congeries_core.runtime.run import (
    AgentRun,
    AuditFailureMode,
    RunControlPolicy,
    RunStatus,
    RunTransition,
)
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.state.repository import InMemoryRunRepository
from congeries_core.state.service import RunService

from ..provider_support import (
    AuditRecorder,
    FakeContextProvider,
    FakeModelProvider,
    ProviderEventRecorder,
    RecordingPolicy,
    authorized_dispatcher,
)
from ..support import NOW, FixedClock, agent_run, call_context

CONTEXT_PROVIDER_ID = ProviderId("context-provider")
MODEL_PROVIDER_ID = ProviderId("model-provider")
MODEL_SELECTOR = ModelSelector(MODEL_PROVIDER_ID, ModelId("model-a"))
FALLBACK_SELECTOR = ModelSelector(ProviderId("fallback-provider"), ModelId("model-b"))
CONTEXT_SCHEMA = SchemaRef("test", "profile", "1")
CONTEXT_KEY = ContextKey("agent", "profile")
CONTEXT_REQUIREMENT = ContextRequirement(CONTEXT_KEY, CONTEXT_SCHEMA)


class AcceptValidator:
    def validate(self, value: object) -> None:
        del value


@dataclass(slots=True)
class TransitionRecorder:
    transitions: list[RunTransition] = field(default_factory=list)

    async def run_state_changed(self, transition: RunTransition) -> None:
        self.transitions.append(transition)


@dataclass(slots=True)
class RunAuditFailureHandler:
    runs: RunService

    async def handle(self, run_id: RunId, error: ErrorDetail) -> None:
        await self.runs.handle_audit_failure(run_id, error)


@dataclass(slots=True)
class RuntimeFixture:
    runtime: AgentRuntime
    runs: RunService
    run: AgentRun
    context_provider: FakeContextProvider
    model_provider: FakeModelProvider
    policy: RecordingPolicy
    transitions: TransitionRecorder
    events: ProviderEventRecorder


async def runtime_fixture(
    *,
    policy: RecordingPolicy | None = None,
    context_result: ContextResult | Exception | None = None,
    model_response: ModelResponse | Exception | None = None,
    model_provider: FakeModelProvider | None = None,
    fallback_provider: FakeModelProvider | None = None,
    events: ProviderEventRecorder | None = None,
    audit_failure_mode: AuditFailureMode = AuditFailureMode.PAUSE,
    fail_audit: bool = False,
) -> RuntimeFixture:
    run = replace(agent_run(), control_policy=RunControlPolicy(audit_failure_mode))
    transitions = TransitionRecorder()
    runs = RunService(InMemoryRunRepository(), FixedClock(), transitions)
    created = await runs.create(run)
    assert isinstance(created, AgentRun)

    actual_context_result = context_result or ContextResult(
        CONTEXT_PROVIDER_ID,
        "1",
        (
            ContextEntry(
                CONTEXT_KEY,
                CONTEXT_SCHEMA,
                {"name": "Ada"},
                ("fake-context",),
            ),
        ),
        ContextCompleteness.COMPLETE,
        usage=ContextUsage(16, 4),
    )
    context_provider = FakeContextProvider(
        ContextCapabilities(
            CONTEXT_PROVIDER_ID,
            "1",
            (CONTEXT_REQUIREMENT,),
            True,
            ContextBudget(1_000, 100),
        ),
        actual_context_result,
    )
    context_registry = ContextProviderRegistry()
    context_registry.register(CONTEXT_PROVIDER_ID, context_provider)

    actual_model_response = model_response or ModelResponse(
        (ContentBlock.text("hello"),),
        ModelFinishReason.STOP,
        ModelUsage(5, 2),
        MODEL_PROVIDER_ID,
        MODEL_SELECTOR.model_id,
        provenance=("fake-model",),
    )
    actual_model_provider = model_provider or FakeModelProvider(
        ModelCapabilities(
            MODEL_SELECTOR,
            frozenset({ModelOperation.GENERATE, ModelOperation.STREAM}),
            False,
            False,
            frozenset({ContentKind.TEXT}),
            frozenset({ContentKind.TEXT}),
            ModelBudget(1_000, 1_000),
            True,
            "1",
        ),
        actual_model_response,
        (ModelEvent.start(), ModelEvent.completion(actual_model_response)),
    )
    model_registry = ModelProviderRegistry()
    model_registry.register(MODEL_PROVIDER_ID, actual_model_provider)
    if fallback_provider is not None:
        model_registry.register(FALLBACK_SELECTOR.provider_id, fallback_provider)

    schemas = SchemaRegistry()
    schemas.register(CONTEXT_SCHEMA, AcceptValidator())
    actual_policy = policy or RecordingPolicy()
    actual_events = events or ProviderEventRecorder()
    dispatcher: AuthorizedDispatcher[object]
    if fail_audit:
        dispatcher = AuthorizedDispatcher(
            action_registry=ActionRegistry(provider_actions()),
            audit_publisher=AuditRecorder(fail=True),
            audit_failure_handler=RunAuditFailureHandler(runs),
            clock=FixedClock(),
            policy=actual_policy,
        )
    else:
        dispatcher = authorized_dispatcher(actual_policy)
    contexts = ContextResolver(
        providers=context_registry,
        schemas=schemas,
        merges=ContextMergeRegistry(),
        dispatcher=dispatcher,
        clock=FixedClock(),
        events=actual_events,
    )
    models = ModelGateway(
        providers=model_registry,
        schemas=schemas,
        dispatcher=dispatcher,
        clock=FixedClock(),
        events=actual_events,
    )
    context_binding = ContextBinding(
        (CONTEXT_PROVIDER_ID,),
        (CONTEXT_REQUIREMENT,),
        ContextMergeStrategy.SINGLE,
        ContextCompletenessPolicy.REQUIRE_COMPLETE,
    )
    model_binding = ModelBinding(
        run.model_binding_ref,
        MODEL_SELECTOR,
        fallbacks=(FALLBACK_SELECTOR,) if fallback_provider else (),
    )
    spec = AgentSpec(
        run.agent_id,
        run.definition_id,
        (ContentBlock.text("Be helpful"),),
        context_binding,
        model_binding,
    )
    agents = AgentRegistry()
    agents.register(spec)
    return RuntimeFixture(
        AgentRuntime(
            agents=agents,
            contexts=contexts,
            models=models,
            runs=runs,
            clock=FixedClock(),
        ),
        runs,
        run,
        context_provider,
        actual_model_provider,
        actual_policy,
        transitions,
        actual_events,
    )


@pytest.mark.asyncio
async def test_no_plugin_agent_end_to_end_succeeds() -> None:
    fixture = await runtime_fixture()
    context = call_context(run_id=fixture.run.run_id, scope=fixture.run.scope)

    result = await fixture.runtime.execute(
        fixture.run.run_id, (ContentBlock.text("Hi"),), context
    )

    assert result.run.status is RunStatus.SUCCEEDED
    assert result.run.state_version == 4
    assert result.response is not None
    assert AgentExecutionResult.from_data(result.to_data()) == result
    assert [
        transition.current.status for transition in fixture.transitions.transitions
    ] == [
        RunStatus.STARTING,
        RunStatus.CONTEXT_LOADING,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
    ]
    assert fixture.context_provider.capability_calls == 1
    assert len(fixture.context_provider.provide_calls) == 1
    assert fixture.model_provider.capability_calls == 1
    assert len(fixture.model_provider.generate_calls) == 1
    assert [request.action.name for request in fixture.policy.requests] == [
        "context.capabilities",
        "context.provide",
        "model.capabilities",
        "model.generate",
    ]
    event_names = [name for name, _ in fixture.events.events]
    assert event_names == [
        "core.context.resolution_started",
        "core.context.provider_selected",
        "core.context.resolution_completed",
        "core.model.invocation_started",
        "core.model.invocation_completed",
    ]
    assert all(
        "prompt" not in payload and "output" not in payload and "value" not in payload
        for _, payload in fixture.events.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("denied_action", "expected_status"),
    [
        (CONTEXT_CAPABILITIES_ACTION.name, RunStatus.FAILED),
        (MODEL_GENERATE_ACTION.name, RunStatus.FAILED),
    ],
)
async def test_agent_denial_preserves_failed_run(
    denied_action: str, expected_status: RunStatus
) -> None:
    fixture = await runtime_fixture(
        policy=RecordingPolicy(denied_actions={denied_action})
    )
    result = await fixture.runtime.execute(
        fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=fixture.run.run_id, scope=fixture.run.scope),
    )
    assert result.run.status is expected_status
    assert result.error is not None
    assert result.error.category is ErrorCategory.DENIED
    if denied_action == CONTEXT_CAPABILITIES_ACTION.name:
        assert fixture.context_provider.capability_calls == 0
        assert not fixture.model_provider.generate_calls
    else:
        assert not fixture.model_provider.generate_calls


@pytest.mark.asyncio
async def test_agent_rejects_partial_context_and_provider_failure() -> None:
    partial = ContextResult(
        CONTEXT_PROVIDER_ID,
        "1",
        (),
        ContextCompleteness.PARTIAL,
        missing_keys=(CONTEXT_KEY,),
    )
    partial_fixture = await runtime_fixture(context_result=partial)
    partial_result = await partial_fixture.runtime.execute(
        partial_fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(
            run_id=partial_fixture.run.run_id, scope=partial_fixture.run.scope
        ),
    )
    assert partial_result.run.status is RunStatus.FAILED
    assert partial_result.error is not None
    assert partial_result.error.code == "partial_context_rejected"
    assert not partial_fixture.model_provider.generate_calls

    failed_fixture = await runtime_fixture(model_response=RuntimeError("vendor down"))
    failed_result = await failed_fixture.runtime.execute(
        failed_fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=failed_fixture.run.run_id, scope=failed_fixture.run.scope),
    )
    assert failed_result.run.status is RunStatus.FAILED
    assert failed_result.error is not None
    assert failed_result.error.code == "model_provider_failure"


@pytest.mark.asyncio
async def test_agent_uses_fallback_only_for_unsupported_capability() -> None:
    primary = FakeModelProvider(
        ModelCapabilities(
            MODEL_SELECTOR,
            frozenset(),
            False,
            False,
            frozenset({ContentKind.TEXT}),
            frozenset({ContentKind.TEXT}),
            ModelBudget(),
            True,
            "1",
        ),
        RuntimeError("primary must not generate"),
        (),
    )
    fallback_response = ModelResponse(
        (ContentBlock.text("fallback"),),
        ModelFinishReason.STOP,
        ModelUsage(2, 1),
        FALLBACK_SELECTOR.provider_id,
        FALLBACK_SELECTOR.model_id,
    )
    fallback = FakeModelProvider(
        ModelCapabilities(
            FALLBACK_SELECTOR,
            frozenset({ModelOperation.GENERATE}),
            False,
            False,
            frozenset({ContentKind.TEXT}),
            frozenset({ContentKind.TEXT}),
            ModelBudget(),
            True,
            "1",
        ),
        fallback_response,
        (),
    )
    fixture = await runtime_fixture(model_provider=primary, fallback_provider=fallback)
    result = await fixture.runtime.execute(
        fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=fixture.run.run_id, scope=fixture.run.scope),
    )
    assert result.run.status is RunStatus.SUCCEEDED
    assert result.response == fallback_response
    assert not primary.generate_calls
    assert len(fallback.generate_calls) == 1


@pytest.mark.asyncio
async def test_agent_cancellation_and_completion_race_have_one_terminal_state() -> None:
    started = asyncio.Event()

    class SlowModel(FakeModelProvider):
        async def generate(
            self, request: ModelRequest, context: object
        ) -> ModelResponse:
            del context
            self.generate_calls.append(request)
            started.set()
            await asyncio.Future[None]()
            raise AssertionError("unreachable")

    slow = SlowModel(
        ModelCapabilities(
            MODEL_SELECTOR,
            frozenset({ModelOperation.GENERATE}),
            False,
            False,
            frozenset({ContentKind.TEXT}),
            frozenset({ContentKind.TEXT}),
            ModelBudget(),
            True,
            "1",
        ),
        RuntimeError("unused"),
        (),
    )
    fixture = await runtime_fixture(model_provider=slow)
    context = call_context(run_id=fixture.run.run_id, scope=fixture.run.scope)
    task = asyncio.create_task(
        fixture.runtime.execute(fixture.run.run_id, (ContentBlock.text("Hi"),), context)
    )
    await started.wait()
    context.cancellation.cancel()
    cancelled = await task
    assert cancelled.run.status is RunStatus.CANCELLED
    assert cancelled.error is not None
    assert cancelled.error.category is ErrorCategory.CANCELLED

    race_fixture = await runtime_fixture()

    class CancellingModel(FakeModelProvider):
        async def generate(
            self, request: ModelRequest, context: object
        ) -> ModelResponse:
            del context
            self.generate_calls.append(request)
            current = await race_fixture.runs.get(race_fixture.run.run_id)
            await race_fixture.runs.cancel(
                race_fixture.run.run_id, current.state_version
            )
            assert not isinstance(self.response, Exception)
            return self.response

    cancelling = CancellingModel(
        race_fixture.model_provider.declared_capabilities,
        race_fixture.model_provider.response,
        (),
    )
    race_fixture = await runtime_fixture(model_provider=cancelling)
    raced = await race_fixture.runtime.execute(
        race_fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=race_fixture.run.run_id, scope=race_fixture.run.scope),
    )
    assert raced.run.status is RunStatus.CANCELLED
    assert [
        transition.current.status
        for transition in race_fixture.transitions.transitions
        if transition.current.status.terminal
    ] == [RunStatus.CANCELLED]


@pytest.mark.asyncio
async def test_agent_deadline_maps_to_failed_without_provider_invocation() -> None:
    fixture = await runtime_fixture()
    context = replace(
        call_context(run_id=fixture.run.run_id, scope=fixture.run.scope),
        deadline=Deadline(NOW - timedelta(seconds=1)),
    )
    result = await fixture.runtime.execute(
        fixture.run.run_id, (ContentBlock.text("Hi"),), context
    )
    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.category is ErrorCategory.TIMEOUT
    assert fixture.context_provider.capability_calls == 0


@pytest.mark.asyncio
async def test_observability_failure_does_not_change_agent_outcome() -> None:
    fixture = await runtime_fixture(events=ProviderEventRecorder(fail=True))
    result = await fixture.runtime.execute(
        fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=fixture.run.run_id, scope=fixture.run.scope),
    )
    assert result == AgentExecutionResult(result.run, response=result.response)
    assert result.run.status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AuditFailureMode.PAUSE, RunStatus.PAUSED),
        (AuditFailureMode.FAIL, RunStatus.FAILED),
    ],
)
async def test_reliable_audit_failure_preserves_policy_run_state(
    mode: AuditFailureMode, expected: RunStatus
) -> None:
    fixture = await runtime_fixture(
        policy=RecordingPolicy(denied_actions={CONTEXT_CAPABILITIES_ACTION.name}),
        audit_failure_mode=mode,
        fail_audit=True,
    )
    result = await fixture.runtime.execute(
        fixture.run.run_id,
        (ContentBlock.text("Hi"),),
        call_context(run_id=fixture.run.run_id, scope=fixture.run.scope),
    )
    assert result.run.status is expected
    assert result.error is not None
    assert result.error.code == "audit_failed"


def test_agent_registry_and_execution_value_validation() -> None:
    run = agent_run()
    context_binding = ContextBinding(
        (CONTEXT_PROVIDER_ID,), (CONTEXT_REQUIREMENT,), ContextMergeStrategy.SINGLE
    )
    model_binding = ModelBinding(run.model_binding_ref, MODEL_SELECTOR)
    spec = AgentSpec(
        run.agent_id,
        run.definition_id,
        (ContentBlock.text("instruction"),),
        context_binding,
        model_binding,
    )
    registry = AgentRegistry()
    registry.register(spec)
    assert registry.get(run.agent_id, run.definition_id) == spec
    assert AgentSpec.from_data(spec.to_data()) == spec
    with pytest.raises(CoreError):
        registry.register(spec)
    with pytest.raises(ValueError):
        replace(spec, instructions=())
    with pytest.raises(ValueError):
        AgentExecutionResult(run)
    with pytest.raises(CoreError):
        registry.get(run.agent_id, replace(run.definition_id, value="missing"))
