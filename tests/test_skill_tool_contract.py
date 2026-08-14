"""Skill/Tool v1 model, registry, authorization, retry, and lease contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import cast

import pytest

from congeries_core.plugin import (
    CapabilityRegistrationPlan,
    CapabilityRegistry,
    LoadedCapability,
    ManifestValidator,
    PluginCapabilityInvoker,
    PluginLifecycleController,
    PluginLifecycleState,
    RegistrationReceipt,
)
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    PolicyDecision,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime import CapabilityRef
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import IdempotencyKey, PrincipalId, ResourceId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.skill import (
    SKILL_RESOURCE_READ_ACTION,
    SkillDescriptor,
    SkillImplementation,
    SkillRegistry,
    SkillResourceDescriptor,
    SkillResourceGateway,
    SkillResourceKind,
    SkillResourceRequest,
    SkillToolResolver,
    skill_actions,
)
from congeries_core.tool import (
    TOOL_EXECUTE_ACTION,
    ToolCall,
    ToolDescriptor,
    ToolExecutionPolicy,
    ToolGateway,
    ToolIdempotencyMode,
    ToolImplementation,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
    tool_actions,
)

from .provider_support import (
    AuditRecorder,
    FailureRecorder,
    ProviderEventRecorder,
    RecordingPolicy,
)
from .support import NOW, FixedClock, call_context

INPUT_SCHEMA = SchemaRef("test", "tool.input", "1")
OUTPUT_SCHEMA = SchemaRef("test", "tool.output", "1")
SKILL_REF = CapabilityRef(
    "core", "skill", ResourceId("test.capabilities.skill"), "test.capabilities", "1"
)
TOOL_REF = CapabilityRef(
    "core", "tool", ResourceId("test.capabilities.tool"), "test.capabilities", "1"
)
RESOURCE = SkillResourceDescriptor(
    ResourceId("guide"),
    SkillResourceKind.INSTRUCTION,
    "guides/main.md",
    "text/plain",
    64,
)


class ObjectWithValue:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), str):
            raise ValueError("value is required")


class ObjectWithOk:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("ok"), str):
            raise ValueError("ok is required")


@dataclass(slots=True)
class FakeSkillLoader:
    calls: list[str] = field(default_factory=list[str])
    result: ContentBlock = field(default_factory=lambda: ContentBlock.text("guide"))

    async def load_resource(
        self, descriptor: SkillResourceDescriptor, context: RuntimeCallContext
    ) -> ContentBlock:
        del context
        self.calls.append(descriptor.resource_id.value)
        return self.result


@dataclass(slots=True)
class AlternateSkillLoader:
    calls: list[str] = field(default_factory=list[str])
    result: ContentBlock = field(default_factory=lambda: ContentBlock.text("guide"))

    async def load_resource(
        self, descriptor: SkillResourceDescriptor, context: RuntimeCallContext
    ) -> ContentBlock:
        context.check_active(FixedClock())
        self.calls.extend([descriptor.resource_id.value])
        return self.result


@dataclass(slots=True)
class BlockingSkillLoader(FakeSkillLoader):
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def load_resource(
        self, descriptor: SkillResourceDescriptor, context: RuntimeCallContext
    ) -> ContentBlock:
        del context
        self.calls.append(descriptor.resource_id.value)
        self.entered.set()
        await self.release.wait()
        return self.result


@dataclass(slots=True)
class FakeToolExecutor:
    failures: int = 1
    ordinary_failures: int = 0
    retryable: bool = True
    valid_output: bool = True
    calls: list[str] = field(default_factory=list[str])

    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> JsonValue:
        assert context.idempotency_key is not None
        identity = context.idempotency_key.value
        self.calls.append(identity)
        if self.ordinary_failures:
            self.ordinary_failures -= 1
            raise RuntimeError("private executor failure")
        if self.failures:
            self.failures -= 1
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "temporary_tool_failure",
                "temporary Tool failure",
                retryable=self.retryable,
            )
        assert isinstance(call.input, dict)
        if not self.valid_output:
            return {"not_ok": True}
        return {"ok": call.input["value"]}


@dataclass(slots=True)
class AlternateToolExecutor:
    failures: int = 1
    retryable: bool = True
    valid_output: bool = True
    calls: list[str] = field(default_factory=list[str])

    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> JsonValue:
        assert context.idempotency_key is not None
        self.calls += [context.idempotency_key.value]
        if self.failures > 0:
            self.failures -= 1
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "temporary_tool_failure",
                "temporary Tool failure",
                retryable=self.retryable,
            )
        assert isinstance(call.input, dict)
        return {"ok": call.input["value"]} if self.valid_output else {"not_ok": True}


class BlockingToolExecutor(FakeToolExecutor):
    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> JsonValue:
        del call, context
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@dataclass(slots=True)
class GateToolExecutor(FakeToolExecutor):
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> JsonValue:
        assert context.idempotency_key is not None
        self.calls.append(context.idempotency_key.value)
        self.entered.set()
        await self.release.wait()
        assert isinstance(call.input, dict)
        return {"ok": call.input["value"]}


@dataclass(slots=True)
class BlockingPolicy(RecordingPolicy):
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


type TestSkillLoader = FakeSkillLoader | AlternateSkillLoader | BlockingSkillLoader
type TestToolExecutor = (
    FakeToolExecutor | AlternateToolExecutor | BlockingToolExecutor | GateToolExecutor
)


@dataclass(slots=True)
class ContractRuntime:
    skills: SkillRegistry
    tools: ToolRegistry
    skill_gateway: SkillResourceGateway
    tool_gateway: ToolGateway
    invoker: PluginCapabilityInvoker
    lifecycle: PluginLifecycleController
    registry: CapabilityRegistry
    receipt: RegistrationReceipt
    policy: RecordingPolicy
    events: ProviderEventRecorder
    loader: TestSkillLoader
    executor: TestToolExecutor


@dataclass(slots=True)
class RecordingExecutionGuard:
    calls: list[str] = field(default_factory=list[str])
    failure: CoreError | None = None

    async def before_execute(
        self,
        call: ToolCall,
        descriptor: ToolDescriptor,
        context: RuntimeCallContext,
    ) -> None:
        del call, descriptor
        assert context.idempotency_key is not None
        self.calls.append(context.idempotency_key.value)
        if self.failure is not None:
            raise self.failure


async def runtime(
    loader_type: type[TestSkillLoader] = FakeSkillLoader,
    executor_type: type[TestToolExecutor] = FakeToolExecutor,
    *,
    policy: RecordingPolicy | None = None,
    tool_timeout_ms: int | None = 1_000,
    side_effect: ToolSideEffect = ToolSideEffect.EXTERNAL,
) -> ContractRuntime:
    clock = FixedClock()
    registry = CapabilityRegistry()
    lifecycle = PluginLifecycleController(clock)
    schemas = SchemaRegistry()
    schemas.register(INPUT_SCHEMA, ObjectWithValue())
    schemas.register(OUTPUT_SCHEMA, ObjectWithOk())
    actions = ActionRegistry((*skill_actions(), *tool_actions()))
    policy = policy or RecordingPolicy()
    dispatcher: AuthorizedDispatcher[object] = AuthorizedDispatcher(
        action_registry=actions,
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        policy=policy,
    )
    manifest = ManifestValidator().validate(
        {
            "contract_version": "1",
            "name": "test.capabilities",
            "version": "1.0.0",
            "core_api": ">=0.2.0,<0.3.0",
            "entrypoint": "test.capabilities:plugin",
            "provides": [
                {
                    "type": "skill",
                    "capability_id": SKILL_REF.id.value,
                    "contract_version": "1.0.0",
                    "entry": "skill",
                    "permissions": [
                        {
                            "action": SKILL_RESOURCE_READ_ACTION.to_data(),
                            "scope_pattern": "core:workspace:*",
                        }
                    ],
                },
                {
                    "type": "tool",
                    "capability_id": TOOL_REF.id.value,
                    "contract_version": "1.0.0",
                    "entry": "tool",
                    "permissions": [
                        {
                            "action": TOOL_EXECUTE_ACTION.to_data(),
                            "scope_pattern": "core:workspace:*",
                        }
                    ],
                },
            ],
            "requires": [],
            "permissions": [
                {
                    "action": SKILL_RESOURCE_READ_ACTION.to_data(),
                    "scope_pattern": "core:workspace:*",
                },
                {
                    "action": TOOL_EXECUTE_ACTION.to_data(),
                    "scope_pattern": "core:workspace:*",
                },
            ],
            "lifecycle": [],
        }
    )
    loader = loader_type()
    executor = executor_type()
    skill = SkillImplementation(
        SkillDescriptor(
            SKILL_REF, "Test Skill", "Progressive test resources", (RESOURCE,)
        ),
        loader,
    )
    tool = ToolImplementation(
        ToolDescriptor(
            TOOL_REF,
            "Test Tool",
            "Schema-aware test Tool",
            INPUT_SCHEMA,
            OUTPUT_SCHEMA,
            TOOL_EXECUTE_ACTION,
            ToolExecutionPolicy(timeout_ms=tool_timeout_ms, max_attempts=2),
            side_effect,
            ToolIdempotencyMode.CALLER_KEY
            if side_effect is not ToolSideEffect.NONE
            else ToolIdempotencyMode.NOT_APPLICABLE,
        ),
        executor,
    )
    record = await lifecycle.discover(manifest)
    record = await lifecycle.transition(
        manifest.name,
        PluginLifecycleState.VALIDATED,
        expected_version=record.state_version,
    )
    record = await lifecycle.transition(
        manifest.name,
        PluginLifecycleState.LOADED,
        expected_version=record.state_version,
    )
    receipt = registry.commit(
        CapabilityRegistrationPlan(
            manifest.ref,
            (
                LoadedCapability(manifest.provides[0], skill),
                LoadedCapability(manifest.provides[1], tool),
            ),
        ),
        expected_version=0,
    )
    record = await lifecycle.transition(
        manifest.name,
        PluginLifecycleState.REGISTERED,
        expected_version=record.state_version,
        receipt=receipt,
    )
    await lifecycle.transition(
        manifest.name,
        PluginLifecycleState.ACTIVE,
        expected_version=record.state_version,
    )
    skills = SkillRegistry(registry, actions)
    tools = ToolRegistry(registry, schemas, actions)
    invoker = PluginCapabilityInvoker(
        registry=registry, lifecycle=lifecycle, dispatcher=dispatcher, clock=clock
    )
    events = ProviderEventRecorder()
    return ContractRuntime(
        skills,
        tools,
        SkillResourceGateway(
            skills=skills, invoker=invoker, clock=clock, events=events
        ),
        ToolGateway(
            tools=tools, invoker=invoker, schemas=schemas, clock=clock, events=events
        ),
        invoker,
        lifecycle,
        registry,
        receipt,
        policy,
        events,
        loader,
        executor,
    )


def test_models_are_strict_immutable_and_round_trip() -> None:
    skill = SkillDescriptor(SKILL_REF, "Skill", "Summary", (RESOURCE,))
    tool = ToolDescriptor(
        TOOL_REF,
        "Tool",
        "Summary",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        TOOL_EXECUTE_ACTION,
        ToolExecutionPolicy(100, 2),
        ToolSideEffect.EXTERNAL,
        ToolIdempotencyMode.CALLER_KEY,
    )
    assert SkillDescriptor.from_data(skill.to_data()) == skill
    assert ToolDescriptor.from_data(tool.to_data()) == tool
    ref_data: dict[str, object] = {
        key: value for key, value in TOOL_REF.to_data().items()
    }
    assert CapabilityRef.from_data(ref_data) == TOOL_REF
    with pytest.raises(ValueError, match="owning_extension"):
        CapabilityRef.from_data({**ref_data, "owning_extension": None})
    assert TOOL_REF.registration_key == ("tool", "test.capabilities.tool", "1.0.0")
    with pytest.raises(ValueError, match="fields"):
        SkillDescriptor.from_data({**skill.to_data(), "unknown": True})
    with pytest.raises(ValueError, match="traversal"):
        SkillResourceDescriptor(
            ResourceId("bad"),
            SkillResourceKind.SCRIPT,
            "../bad.py",
            "text/x-python",
            10,
        )
    with pytest.raises(ValueError, match="unique"):
        SkillDescriptor(SKILL_REF, "Skill", "Summary", (RESOURCE, RESOURCE))
    with pytest.raises(ValueError, match="caller-key"):
        ToolDescriptor(
            TOOL_REF,
            "Tool",
            "Summary",
            INPUT_SCHEMA,
            OUTPUT_SCHEMA,
            TOOL_EXECUTE_ACTION,
            ToolExecutionPolicy(),
            ToolSideEffect.EXTERNAL,
            ToolIdempotencyMode.NOT_APPLICABLE,
        )
    for invalid_budget in (True, cast(int, 1.5)):
        with pytest.raises(ValueError, match="positive integer"):
            SkillResourceDescriptor(
                ResourceId("invalid-budget"),
                SkillResourceKind.REFERENCE,
                "invalid.txt",
                "text/plain",
                invalid_budget,
            )
        with pytest.raises(ValueError, match="positive integer"):
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), invalid_budget)
        with pytest.raises(ValueError, match="positive integer"):
            ToolExecutionPolicy(timeout_ms=invalid_budget)
        with pytest.raises(ValueError, match="positive integer"):
            ToolExecutionPolicy(max_attempts=invalid_budget)
        with pytest.raises(ValueError, match="positive integer"):
            ToolResult(TOOL_REF, {"ok": "value"}, invalid_budget, "operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("loader_type", [FakeSkillLoader, AlternateSkillLoader])
async def test_skill_loader_contract_is_lazy_authorized_and_leased(
    loader_type: type[TestSkillLoader],
) -> None:
    fixture = await runtime(loader_type=loader_type)
    resolved = fixture.skills.resolve(SKILL_REF)
    assert resolved.descriptor.resources == (RESOURCE,)
    assert resolved.owner.name == "test.capabilities"
    assert fixture.loader.calls == []
    result = await fixture.skill_gateway.load(
        SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), call_context()
    )
    assert result.content == ContentBlock.text("guide")
    assert result.byte_count == 5
    assert fixture.loader.calls == ["guide"]
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0
    payloads = [payload for _, payload in fixture.events.events]
    assert all("content" not in payload for payload in payloads)


@pytest.mark.asyncio
async def test_skill_read_allows_sequential_replay_but_tool_still_conflicts() -> None:
    fixture = await runtime()
    context = call_context()
    request = SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64)

    first = await fixture.skill_gateway.load(request, context)
    replay = await fixture.skill_gateway.load(request, context)

    assert replay == first
    assert fixture.loader.calls == ["guide", "guide"]
    tool_call = ToolCall(TOOL_REF, {"value": "x"})
    await fixture.tool_gateway.execute(tool_call, context)
    with pytest.raises(CoreError) as duplicate_tool:
        await fixture.tool_gateway.execute(tool_call, context)
    assert duplicate_tool.value.detail.code == "lease_identity_conflict"


@pytest.mark.asyncio
async def test_skill_denial_and_invalid_budget_precede_loader_effects() -> None:
    fixture = await runtime()
    fixture.policy.denied_actions.add(SKILL_RESOURCE_READ_ACTION.name)
    with pytest.raises(CoreError) as denied:
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), call_context()
        )
    assert denied.value.detail.category is ErrorCategory.DENIED
    with pytest.raises(CoreError, match="declared byte budget"):
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 65), call_context()
        )
    assert fixture.loader.calls == []


@pytest.mark.asyncio
async def test_skill_and_tool_require_identity_and_skill_failures_release_lease() -> (
    None
):
    fixture = await runtime()
    missing_identity = replace(call_context(), idempotency_key=None)
    with pytest.raises(CoreError) as missing_skill:
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64),
            missing_identity,
        )
    assert missing_skill.value.detail.code == "missing_invocation_identity"
    with pytest.raises(CoreError) as missing_tool:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "x"}), missing_identity
        )
    assert missing_tool.value.detail.code == "missing_invocation_identity"

    with pytest.raises(CoreError) as undeclared:
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("missing"), 64), call_context()
        )
    assert undeclared.value.detail.code == "skill_resource_undeclared"
    assert fixture.loader.calls == []

    fixture.policy.constraints = {"unknown": True}
    with pytest.raises(CoreError) as invalid_grant:
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), call_context()
        )
    assert invalid_grant.value.detail.code == "invalid_grant"
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0

    oversized = await runtime()
    assert isinstance(oversized.loader, FakeSkillLoader)
    oversized.loader.result = ContentBlock.text("x" * 65)
    with pytest.raises(CoreError) as budget:
        await oversized.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), call_context()
        )
    assert budget.value.detail.code == "skill_resource_budget_exceeded"
    assert await oversized.lifecycle.active_lease_count("test.capabilities") == 0

    wrong_media = await runtime()
    assert isinstance(wrong_media.loader, FakeSkillLoader)
    wrong_media.loader.result = ContentBlock(
        ContentKind.TEXT, "guide", media_type="text/markdown"
    )
    with pytest.raises(CoreError) as protocol:
        await wrong_media.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), call_context()
        )
    assert protocol.value.detail.code == "skill_protocol_failure"
    assert await wrong_media.lifecycle.active_lease_count("test.capabilities") == 0


@pytest.mark.asyncio
async def test_invoker_rejects_mismatched_authorization_resource() -> None:
    fixture = await runtime()

    async def must_not_run(registration: object, call: object) -> None:
        del registration, call
        raise AssertionError("mismatched resource reached implementation")

    with pytest.raises(CoreError) as mismatch:
        await fixture.invoker.invoke(
            plugin_id=TOOL_REF.owning_extension,
            capability_key=TOOL_REF.registration_key,
            action=TOOL_EXECUTE_ACTION,
            resource=ResourceRef(
                "core",
                "tool",
                ResourceId("different.tool"),
                owning_extension=TOOL_REF.owning_extension,
            ),
            context=call_context(),
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId("run-1")
            ),
            operation=must_not_run,
        )
    assert mismatch.value.detail.code == "invocation_resource_mismatch"
    assert fixture.policy.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_type", [FakeToolExecutor, AlternateToolExecutor])
async def test_tool_contract_retries_under_one_lease_with_stable_identity(
    executor_type: type[TestToolExecutor],
) -> None:
    fixture = await runtime(executor_type=executor_type)
    result = await fixture.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "done"}), call_context()
    )
    assert result.output == {"ok": "done"}
    assert result.attempts == 2
    assert fixture.executor.calls == ["idempotency-1", "idempotency-1"]
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0
    payloads = [payload for _, payload in fixture.events.events]
    assert all(
        "input" not in payload and "output" not in payload for payload in payloads
    )


@pytest.mark.asyncio
async def test_tool_whole_call_timeout_covers_authorization() -> None:
    policy = BlockingPolicy()
    fixture = await runtime(policy=policy, tool_timeout_ms=1)
    with pytest.raises(CoreError) as timeout:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "wait"}), call_context()
        )
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT
    assert policy.entered.is_set()
    assert fixture.executor.calls == []
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_tool_identity_has_one_effect() -> None:
    fixture = await runtime(executor_type=GateToolExecutor)
    assert isinstance(fixture.executor, GateToolExecutor)
    context = replace(
        call_context(), idempotency_key=IdempotencyKey("concurrent-operation")
    )
    first = asyncio.create_task(
        fixture.tool_gateway.execute(ToolCall(TOOL_REF, {"value": "first"}), context)
    )
    await fixture.executor.entered.wait()
    with pytest.raises(CoreError) as duplicate:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "second"}), context
        )
    assert duplicate.value.detail.code == "lease_identity_conflict"
    fixture.executor.release.set()
    result = await first
    assert result.output == {"ok": "first"}
    assert fixture.executor.calls == ["concurrent-operation"]
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0


@pytest.mark.asyncio
async def test_tool_schema_denial_invalid_grant_and_replay_have_no_extra_effects() -> (
    None
):
    fixture = await runtime()
    with pytest.raises(CoreError) as invalid_input:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"bad": True}), call_context()
        )
    assert invalid_input.value.detail.code == "schema_validation_failed"
    assert fixture.executor.calls == []

    fixture.policy.denied_actions.add(TOOL_EXECUTE_ACTION.name)
    with pytest.raises(CoreError) as denied:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "denied"}), call_context()
        )
    assert denied.value.detail.category is ErrorCategory.DENIED
    assert fixture.executor.calls == []

    fixture.policy.denied_actions.clear()
    fixture.policy.constraints = {"max_attempts": 3}
    with pytest.raises(CoreError) as invalid_grant:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "broad"}), call_context()
        )
    assert invalid_grant.value.detail.code == "invalid_grant"
    assert fixture.executor.calls == []

    fixture.policy.constraints = {}
    fixture.executor.failures = 0
    replay_context = replace(
        call_context(), idempotency_key=IdempotencyKey("completed-operation")
    )
    await fixture.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "once"}), replay_context
    )
    with pytest.raises(CoreError) as replay:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "twice"}), replay_context
        )
    assert replay.value.detail.code == "lease_identity_conflict"
    assert fixture.executor.calls == ["completed-operation"]


@pytest.mark.asyncio
async def test_tool_non_retryable_and_output_schema_failure_release_lease() -> None:
    fixture = await runtime()
    fixture.executor.retryable = False
    with pytest.raises(CoreError) as failure:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "x"}), call_context()
        )
    assert failure.value.detail.code == "temporary_tool_failure"
    assert len(fixture.executor.calls) == 1
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0

    second = await runtime()
    second.executor.failures = 0

    second.executor.valid_output = False
    with pytest.raises(CoreError) as output:
        await second.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "x"}), call_context()
        )
    assert output.value.detail.code == "schema_validation_failed"
    assert await second.lifecycle.active_lease_count("test.capabilities") == 0

    ordinary = await runtime()
    assert isinstance(ordinary.executor, FakeToolExecutor)
    ordinary.executor.failures = 0
    ordinary.executor.ordinary_failures = 1
    normalized = await ordinary.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "retry"}),
        replace(call_context(), idempotency_key=IdempotencyKey("ordinary-retry")),
    )
    assert normalized.attempts == 2
    assert ordinary.executor.calls == ["ordinary-retry", "ordinary-retry"]


@pytest.mark.asyncio
async def test_gateway_drain_race_and_timeout_release_their_leases() -> None:
    fixture = await runtime(loader_type=BlockingSkillLoader)
    assert isinstance(fixture.loader, BlockingSkillLoader)
    active_context = replace(
        call_context(), idempotency_key=IdempotencyKey("active-resource-load")
    )
    loading = asyncio.create_task(
        fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), active_context
        )
    )
    await fixture.loader.entered.wait()
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 1
    await fixture.lifecycle.begin_draining("test.capabilities")
    rejected_context = replace(
        call_context(), idempotency_key=IdempotencyKey("draining-resource-load")
    )
    with pytest.raises(CoreError) as draining:
        await fixture.skill_gateway.load(
            SkillResourceRequest(SKILL_REF, ResourceId("guide"), 64), rejected_context
        )
    assert draining.value.detail.code == "plugin_draining"
    assert fixture.loader.calls == ["guide"]
    fixture.loader.release.set()
    assert (await loading).content == ContentBlock.text("guide")
    assert await fixture.lifecycle.active_lease_count("test.capabilities") == 0
    record = await fixture.lifecycle.get("test.capabilities")
    await fixture.lifecycle.transition(
        "test.capabilities",
        PluginLifecycleState.UNREGISTERED,
        expected_version=record.state_version,
    )
    fixture.registry.unregister(fixture.receipt)
    with pytest.raises(CoreError) as unloaded:
        fixture.skills.resolve(SKILL_REF)
    assert unloaded.value.detail.code == "capability_not_registered"

    timeout_fixture = await runtime(executor_type=BlockingToolExecutor)
    timeout_context = replace(
        call_context(),
        idempotency_key=IdempotencyKey("timed-tool"),
        deadline=Deadline(NOW + timedelta(milliseconds=1)),
    )
    with pytest.raises(CoreError) as timeout:
        await timeout_fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "wait"}), timeout_context
        )
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT
    assert await timeout_fixture.lifecycle.active_lease_count("test.capabilities") == 0

    cancelled_fixture = await runtime(executor_type=BlockingToolExecutor)
    cancelled_context = replace(
        call_context(), idempotency_key=IdempotencyKey("cancelled-tool")
    )
    invocation = asyncio.create_task(
        cancelled_fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "wait"}), cancelled_context
        )
    )
    for _ in range(10):
        if await cancelled_fixture.lifecycle.active_lease_count("test.capabilities"):
            break
        await asyncio.sleep(0)
    assert (
        await cancelled_fixture.lifecycle.active_lease_count("test.capabilities") == 1
    )
    cancelled_context.cancellation.cancel()
    with pytest.raises(CoreError) as cancelled:
        await invocation
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert (
        await cancelled_fixture.lifecycle.active_lease_count("test.capabilities") == 0
    )
    assert cancelled_fixture.events.events[-1][0] == "core.tool.invocation_failed"


def test_shared_resolver_rejects_owner_and_version_mismatch() -> None:
    async def exercise() -> None:
        fixture = await runtime()
        resolver = SkillToolResolver(fixture.skills, fixture.tools)
        resolver.validate_skill(SKILL_REF)
        resolver.validate_tool(TOOL_REF)
        with pytest.raises(CoreError) as owner:
            resolver.validate_tool(
                CapabilityRef("core", "tool", TOOL_REF.id, "other.plugin", "1")
            )
        assert owner.value.detail.code == "tool_owner_mismatch"
        with pytest.raises(CoreError) as version:
            resolver.validate_skill(
                CapabilityRef("core", "skill", SKILL_REF.id, "test.capabilities", "2")
            )
        assert version.value.detail.category is ErrorCategory.VERSION_MISMATCH

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_tool_execution_guard_runs_once_before_all_executor_attempts() -> None:
    fixture = await runtime()
    guard = RecordingExecutionGuard()
    context = call_context()
    result = await fixture.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "guarded"}), context, guard=guard
    )
    assert result.output == {"ok": "guarded"}
    assert context.idempotency_key is not None
    assert guard.calls == [context.idempotency_key.value]
    assert len(fixture.executor.calls) == 2


@pytest.mark.asyncio
async def test_tool_execution_guard_failure_prevents_executor_entry() -> None:
    fixture = await runtime()
    guard = RecordingExecutionGuard(
        failure=core_error(
            ErrorCategory.UNAVAILABLE,
            "operation_log_unavailable",
            "operation guard failed",
        )
    )
    with pytest.raises(CoreError) as error:
        await fixture.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "blocked"}), call_context(), guard=guard
        )
    assert error.value.detail.code == "operation_log_unavailable"
    assert fixture.executor.calls == []


@pytest.mark.asyncio
async def test_side_effect_free_tool_allows_sequential_logical_replay() -> None:
    fixture = await runtime(side_effect=ToolSideEffect.NONE)
    context = call_context()
    call = ToolCall(TOOL_REF, {"value": "repeatable"})
    first = await fixture.tool_gateway.execute(call, context)
    second = await fixture.tool_gateway.execute(call, context)
    assert first.output == second.output == {"ok": "repeatable"}
    assert len(fixture.executor.calls) == 3
