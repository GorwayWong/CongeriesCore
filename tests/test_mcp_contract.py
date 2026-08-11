"""MCP Adapter v1 transport-neutral and existing-boundary contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from congeries_core.mcp import (
    MCP_DISCOVERY_COMPLETED,
    MCP_DISCOVERY_FAILED,
    MCP_DISCOVERY_STARTED,
    MCP_INVOCATION_COMPLETED,
    MCP_INVOCATION_FAILED,
    MCP_INVOCATION_STARTED,
    MCP_PROTOCOL_VERSION,
    McpAdapterDescriptor,
    McpAdapterImplementation,
    McpContextBinding,
    McpContextProviderFacade,
    McpContextProviderImplementation,
    McpDiscoverySnapshot,
    McpRemoteResource,
    McpRemoteTool,
    McpResourceRequest,
    McpResourceResponse,
    McpServerInfo,
    McpToolBinding,
    McpToolExecutor,
    McpToolRequest,
    McpToolResponse,
    canonical_schema_digest,
    validate_discovery,
)
from congeries_core.plugin import (
    CapabilityRegistrationPlan,
    CapabilityRegistry,
    LoadedCapability,
    ManifestValidator,
    PluginCapabilityInvoker,
    PluginLifecycleController,
    PluginLifecycleState,
    PluginManifest,
    PreparedPlugin,
)
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRegistry,
    AuthorizedDispatcher,
    Grant,
    PolicyDecision,
)
from congeries_core.provider.context import (
    CONTEXT_CAPABILITIES_ACTION,
    CONTEXT_PROVIDE_ACTION,
    ContextBinding,
    ContextKey,
    ContextMergeRegistry,
    ContextProviderRegistry,
    ContextRequirement,
    ContextResolver,
    context_actions,
)
from congeries_core.runtime import CapabilityRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import CoreError, ErrorCategory
from congeries_core.runtime.ids import (
    DefinitionId,
    IdempotencyKey,
    ResourceId,
)
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.tool import (
    TOOL_EXECUTE_ACTION,
    ToolCall,
    ToolDescriptor,
    ToolExecutionPolicy,
    ToolGateway,
    ToolIdempotencyMode,
    ToolImplementation,
    ToolRegistry,
    ToolSideEffect,
    tool_actions,
)

from .plugin_support import plugin_runtime as plugin_manager_runtime
from .provider_support import (
    AuditRecorder,
    FailureRecorder,
    ProviderEventRecorder,
    RecordingPolicy,
)
from .support import NOW, FixedClock, call_context, child_scope, root_scope

OWNER = "test.mcp"
ADAPTER_REF = CapabilityRef(
    "core", "mcp_adapter", ResourceId("test.mcp.adapter"), OWNER, "1"
)
TOOL_REF = CapabilityRef("core", "tool", ResourceId("test.mcp.tool"), OWNER, "1")
CONTEXT_REF = CapabilityRef(
    "core", "context_provider", ResourceId("test.mcp.context"), OWNER, "1"
)
INPUT_SCHEMA = SchemaRef("test", "mcp.input", "1")
OUTPUT_SCHEMA = SchemaRef("test", "mcp.output", "1")
CONTEXT_SCHEMA = SchemaRef("test", "mcp.profile", "1")
CONTEXT_REQUIREMENT = ContextRequirement(ContextKey("test", "profile"), CONTEXT_SCHEMA)

REMOTE_INPUT_SCHEMA: JsonValue = {
    "type": "object",
    "required": ["value"],
}
REMOTE_OUTPUT_SCHEMA: JsonValue = {
    "type": "object",
    "required": ["ok"],
}
REMOTE_RESOURCE_SCHEMA: JsonValue = {
    "type": "object",
    "required": ["test:profile"],
}


class InputValidator:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), str):
            raise ValueError("value is required")


class OutputValidator:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("ok"), str):
            raise ValueError("ok is required")


class ProfileValidator:
    def validate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ValueError("name is required")


def discovery_snapshot() -> McpDiscoverySnapshot:
    return McpDiscoverySnapshot(
        protocol_version=MCP_PROTOCOL_VERSION,
        server=McpServerInfo("remote.service", "Remote Test", "1.0.0"),
        discovery_identity="discovery-1",
        tools=(
            McpRemoteTool("remote.echo", REMOTE_INPUT_SCHEMA, REMOTE_OUTPUT_SCHEMA),
            McpRemoteTool("remote.unknown", {}, {}),
        ),
        resources=(
            McpRemoteResource(
                "mcp://remote/profile", "application/json", REMOTE_RESOURCE_SCHEMA
            ),
            McpRemoteResource("mcp://remote/unknown", "application/json", {}),
        ),
    )


@dataclass(slots=True)
class FakeStdioTransport:
    snapshot: object = field(default_factory=discovery_snapshot)
    failures: int = 0
    malformed_tool: bool = False
    invalid_tool_output: bool = False
    invalid_context: bool = False
    block_tool: bool = False
    cleanup_failure: bool = False
    discovery_calls: int = 0
    tool_calls: list[McpToolRequest] = field(default_factory=list)
    resource_calls: list[McpResourceRequest] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: int = 0

    @property
    def kind(self) -> str:
        return "stdio"

    async def discover(self, context: RuntimeCallContext) -> McpDiscoverySnapshot:
        context.check_active(FixedClock())
        self.discovery_calls += 1
        return cast(McpDiscoverySnapshot, self.snapshot)

    async def call_tool(
        self, request: McpToolRequest, context: RuntimeCallContext
    ) -> McpToolResponse:
        del context
        self.tool_calls.append(request)
        self.entered.set()
        try:
            if self.block_tool:
                await self.release.wait()
            if self.failures:
                self.failures -= 1
                raise ConnectionError("private stdio failure")
            if self.malformed_tool:
                return cast(McpToolResponse, object())
            assert isinstance(request.arguments, dict)
            if self.invalid_tool_output:
                return McpToolResponse({"invalid": True})
            return McpToolResponse({"ok": request.arguments["value"]})
        finally:
            self.cleaned += 1
            if self.cleanup_failure:
                raise RuntimeError("private stdio cleanup failure")

    async def read_resource(
        self, request: McpResourceRequest, context: RuntimeCallContext
    ) -> McpResourceResponse:
        del context
        self.resource_calls.append(request)
        profile: JsonValue = (
            {"invalid": True} if self.invalid_context else {"name": "Ada"}
        )
        return McpResourceResponse(
            request.uri,
            "application/json",
            {"test:profile": profile},
        )


@dataclass(slots=True)
class FakeStreamableHttpTransport:
    snapshot: object = field(default_factory=discovery_snapshot)
    failures: int = 0
    malformed_tool: bool = False
    invalid_tool_output: bool = False
    invalid_context: bool = False
    block_tool: bool = False
    cleanup_failure: bool = False
    discovery_calls: int = 0
    tool_calls: list[McpToolRequest] = field(default_factory=list)
    resource_calls: list[McpResourceRequest] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: int = 0

    @property
    def kind(self) -> str:
        return "streamable_http"

    async def discover(self, context: RuntimeCallContext) -> McpDiscoverySnapshot:
        if context.cancellation.cancelled:
            context.cancellation.raise_if_cancelled()
        self.discovery_calls = self.discovery_calls + 1
        return cast(McpDiscoverySnapshot, self.snapshot)

    async def call_tool(
        self, request: McpToolRequest, context: RuntimeCallContext
    ) -> McpToolResponse:
        del context
        self.tool_calls += [request]
        self.entered.set()
        try:
            if self.block_tool:
                await self.release.wait()
            if self.failures > 0:
                self.failures = self.failures - 1
                raise OSError("private HTTP failure")
            if self.malformed_tool:
                return cast(McpToolResponse, object())
            assert isinstance(request.arguments, dict)
            output: JsonValue = (
                {"invalid": True}
                if self.invalid_tool_output
                else {"ok": request.arguments["value"]}
            )
            return McpToolResponse(output)
        finally:
            self.cleaned = self.cleaned + 1
            if self.cleanup_failure:
                raise RuntimeError("private HTTP cleanup failure")

    async def read_resource(
        self, request: McpResourceRequest, context: RuntimeCallContext
    ) -> McpResourceResponse:
        context.check_active(FixedClock())
        self.resource_calls += [request]
        value: JsonValue = (
            {"invalid": True} if self.invalid_context else {"name": "Ada"}
        )
        return McpResourceResponse(
            uri=request.uri,
            media_type="application/json",
            contents={"test:profile": value},
        )


type FakeTransport = FakeStdioTransport | FakeStreamableHttpTransport


@dataclass(slots=True)
class BroadeningPolicy(RecordingPolicy):
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=root_scope(),
                constraints={},
                issued_at=NOW,
                expires_at=None,
                policy_version="broadening-test",
                audit_correlation="broadening-audit",
            )
        )


@dataclass(slots=True)
class InvalidMcpLoader:
    adapter: McpAdapterImplementation
    cleanup_calls: int = 0

    async def prepare(
        self, manifest: PluginManifest, context: RuntimeCallContext
    ) -> PreparedPlugin:
        del context
        return PreparedPlugin(
            manifest, (LoadedCapability(manifest.provides[0], self.adapter),)
        )

    async def cleanup(
        self, prepared: PreparedPlugin, context: RuntimeCallContext
    ) -> None:
        del prepared, context
        self.cleanup_calls += 1


@dataclass(slots=True)
class McpRuntime:
    transport: FakeTransport
    tool_gateway: ToolGateway
    context_resolver: ContextResolver
    context_binding: ContextBinding
    lifecycle: PluginLifecycleController
    events: ProviderEventRecorder


async def mcp_runtime(
    transport_type: type[FakeTransport],
    *,
    policy: RecordingPolicy | None = None,
    default_deny: bool = False,
    timeout_ms: int = 1_000,
) -> McpRuntime:
    clock = FixedClock()
    schemas = SchemaRegistry()
    schemas.register(INPUT_SCHEMA, InputValidator())
    schemas.register(OUTPUT_SCHEMA, OutputValidator())
    schemas.register(CONTEXT_SCHEMA, ProfileValidator())
    tool_binding = McpToolBinding(
        TOOL_REF,
        "remote.echo",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        canonical_schema_digest(REMOTE_INPUT_SCHEMA),
        canonical_schema_digest(REMOTE_OUTPUT_SCHEMA),
    )
    context_mapping = McpContextBinding(
        CONTEXT_REF,
        "mcp://remote/profile",
        (CONTEXT_REQUIREMENT,),
        canonical_schema_digest(REMOTE_RESOURCE_SCHEMA),
    )
    adapter_descriptor = McpAdapterDescriptor(
        ADAPTER_REF,
        "remote.service",
        tool_bindings=(tool_binding,),
        context_bindings=(context_mapping,),
    )
    transport = transport_type()
    adapter = McpAdapterImplementation(adapter_descriptor, transport)
    events = ProviderEventRecorder()
    tool_descriptor = ToolDescriptor(
        TOOL_REF,
        "Remote echo",
        "Explicit MCP Tool binding",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        TOOL_EXECUTE_ACTION,
        ToolExecutionPolicy(timeout_ms, 2),
        ToolSideEffect.EXTERNAL,
        ToolIdempotencyMode.CALLER_KEY,
    )
    tool = ToolImplementation(
        tool_descriptor,
        McpToolExecutor(tool_descriptor, adapter, clock, events),
    )
    context_implementation = McpContextProviderImplementation(
        adapter, context_mapping, schemas, clock, events
    )
    manifest = ManifestValidator().validate(
        {
            "contract_version": "1",
            "name": OWNER,
            "version": "1.0.0",
            "core_api": ">=0.2.0,<0.3.0",
            "entrypoint": "test.mcp:plugin",
            "provides": [
                {
                    "type": "mcp_adapter",
                    "capability_id": ADAPTER_REF.id.value,
                    "contract_version": "1.0.0",
                    "entry": "adapter",
                    "permissions": [],
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
                {
                    "type": "context_provider",
                    "capability_id": CONTEXT_REF.id.value,
                    "contract_version": "1.0.0",
                    "entry": "context",
                    "permissions": [
                        {
                            "action": CONTEXT_CAPABILITIES_ACTION.to_data(),
                            "scope_pattern": "core:workspace:*",
                        },
                        {
                            "action": CONTEXT_PROVIDE_ACTION.to_data(),
                            "scope_pattern": "core:workspace:*",
                        },
                    ],
                },
            ],
            "requires": [],
            "permissions": [
                {
                    "action": TOOL_EXECUTE_ACTION.to_data(),
                    "scope_pattern": "core:workspace:*",
                },
                {
                    "action": CONTEXT_CAPABILITIES_ACTION.to_data(),
                    "scope_pattern": "core:workspace:*",
                },
                {
                    "action": CONTEXT_PROVIDE_ACTION.to_data(),
                    "scope_pattern": "core:workspace:*",
                },
            ],
            "lifecycle": [],
        }
    )
    registry = CapabilityRegistry()
    lifecycle = PluginLifecycleController(clock)
    record = await lifecycle.discover(manifest)
    for state in (PluginLifecycleState.VALIDATED, PluginLifecycleState.LOADED):
        record = await lifecycle.transition(
            OWNER, state, expected_version=record.state_version
        )
    capabilities = (
        LoadedCapability(manifest.provides[0], adapter),
        LoadedCapability(manifest.provides[1], tool),
        LoadedCapability(manifest.provides[2], context_implementation),
    )
    adapter.validate_composition(OWNER, capabilities)
    receipt = registry.commit(
        CapabilityRegistrationPlan(manifest.ref, capabilities),
        expected_version=0,
    )
    record = await lifecycle.transition(
        OWNER,
        PluginLifecycleState.REGISTERED,
        expected_version=record.state_version,
        receipt=receipt,
    )
    await lifecycle.transition(
        OWNER, PluginLifecycleState.ACTIVE, expected_version=record.state_version
    )
    actions = ActionRegistry((*tool_actions(), *context_actions()))
    dispatcher: AuthorizedDispatcher[object] = AuthorizedDispatcher(
        action_registry=actions,
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        policy=None if default_deny else (policy or RecordingPolicy()),
    )
    invoker = PluginCapabilityInvoker(
        registry=registry, lifecycle=lifecycle, dispatcher=dispatcher, clock=clock
    )
    tools = ToolRegistry(registry, schemas, actions)
    tool_gateway = ToolGateway(
        tools=tools, invoker=invoker, schemas=schemas, clock=clock, events=events
    )
    providers = ContextProviderRegistry()
    providers.register(
        context_mapping.provider_id, McpContextProviderFacade(context_mapping, invoker)
    )
    context_resolver = ContextResolver(
        providers=providers,
        schemas=schemas,
        merges=ContextMergeRegistry(),
        dispatcher=dispatcher,
        clock=clock,
        events=events,
    )
    binding = ContextBinding(
        (context_mapping.provider_id,), context_mapping.requirements
    )
    return McpRuntime(
        transport, tool_gateway, context_resolver, binding, lifecycle, events
    )


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_shared_transport_contract_tool_context_and_redacted_events(
    transport_type: type[FakeTransport],
) -> None:
    runtime = await mcp_runtime(transport_type)
    context = call_context()
    result = await runtime.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "secret-input"}), context
    )
    assert result.output == {"ok": "secret-input"}
    assert runtime.transport.tool_calls[0].attempt == 1
    assert runtime.transport.tool_calls[0].operation_identity == "idempotency-1"

    resolved = await runtime.context_resolver.resolve(
        runtime.context_binding.request(DefinitionId("agent-1"), context),
        runtime.context_binding,
    )
    assert resolved.entries[0].value == {"name": "Ada"}
    assert runtime.transport.resource_calls[0].uri == "mcp://remote/profile"
    event_types = {item[0] for item in runtime.events.events}
    assert {
        MCP_DISCOVERY_STARTED,
        MCP_DISCOVERY_COMPLETED,
        MCP_INVOCATION_STARTED,
        MCP_INVOCATION_COMPLETED,
    }.issubset(event_types)
    event_text = repr(runtime.events.events)
    assert "secret-input" not in event_text
    assert "Ada" not in event_text
    assert "arguments" not in event_text
    assert "contents" not in event_text


def test_models_round_trip_and_discovery_filters_unknown_capabilities() -> None:
    tool_binding = McpToolBinding(
        TOOL_REF,
        "remote.echo",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        canonical_schema_digest(REMOTE_INPUT_SCHEMA),
        canonical_schema_digest(REMOTE_OUTPUT_SCHEMA),
    )
    context_binding = McpContextBinding(
        CONTEXT_REF,
        "mcp://remote/profile",
        (CONTEXT_REQUIREMENT,),
        canonical_schema_digest(REMOTE_RESOURCE_SCHEMA),
    )
    descriptor = McpAdapterDescriptor(
        ADAPTER_REF,
        "remote.service",
        tool_bindings=(tool_binding,),
        context_bindings=(context_binding,),
    )
    assert McpAdapterDescriptor.from_data(descriptor.to_data()) == descriptor
    snapshot = discovery_snapshot()
    assert McpDiscoverySnapshot.from_data(snapshot.to_data()) == snapshot
    mapped = validate_discovery(descriptor, snapshot)
    assert [item.name for item in mapped.tools] == ["remote.echo"]
    assert [item.uri for item in mapped.resources] == ["mcp://remote/profile"]
    with pytest.raises(ValueError, match="unsupported"):
        replace(descriptor, protocol_version="2025-11-25")
    with pytest.raises(ValueError, match="templates"):
        replace(context_binding, resource_uri="mcp://remote/{id}")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(tool_binding, input_schema_digest="invalid")


def test_atomic_composition_rejects_missing_mapped_capability() -> None:
    transport = FakeStdioTransport()
    binding = McpToolBinding(
        TOOL_REF,
        "remote.echo",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        canonical_schema_digest(REMOTE_INPUT_SCHEMA),
        canonical_schema_digest(REMOTE_OUTPUT_SCHEMA),
    )
    adapter = McpAdapterImplementation(
        McpAdapterDescriptor(ADAPTER_REF, "remote.service", tool_bindings=(binding,)),
        transport,
    )
    declaration = (
        ManifestValidator()
        .validate(
            {
                "contract_version": "1",
                "name": OWNER,
                "version": "1.0.0",
                "core_api": ">=0.2.0,<0.3.0",
                "entrypoint": "test.mcp:plugin",
                "provides": [
                    {
                        "type": "mcp_adapter",
                        "capability_id": ADAPTER_REF.id.value,
                        "contract_version": "1.0.0",
                        "entry": "adapter",
                        "permissions": [],
                    }
                ],
                "requires": [],
                "permissions": [],
                "lifecycle": [],
            }
        )
        .provides[0]
    )
    with pytest.raises(ValueError, match="atomically declared"):
        adapter.validate_composition(OWNER, (LoadedCapability(declaration, adapter),))


@pytest.mark.asyncio
async def test_plugin_manager_rejects_non_atomic_mcp_composition_and_cleans_up() -> (
    None
):
    binding = McpToolBinding(
        TOOL_REF,
        "remote.echo",
        INPUT_SCHEMA,
        OUTPUT_SCHEMA,
        canonical_schema_digest(REMOTE_INPUT_SCHEMA),
        canonical_schema_digest(REMOTE_OUTPUT_SCHEMA),
    )
    adapter = McpAdapterImplementation(
        McpAdapterDescriptor(ADAPTER_REF, "remote.service", tool_bindings=(binding,)),
        FakeStdioTransport(),
    )
    data: dict[str, object] = {
        "contract_version": "1",
        "name": OWNER,
        "version": "1.0.0",
        "core_api": ">=0.2.0,<0.3.0",
        "entrypoint": "test.mcp:plugin",
        "provides": [
            {
                "type": "mcp_adapter",
                "capability_id": ADAPTER_REF.id.value,
                "contract_version": "1.0.0",
                "entry": "adapter",
                "permissions": [],
            }
        ],
        "requires": [],
        "permissions": [],
        "lifecycle": [],
    }
    loader = InvalidMcpLoader(adapter)
    runtime = plugin_manager_runtime()
    with pytest.raises(CoreError) as error:
        await runtime.manager.load(data, loader, call_context(), runtime.principal)
    assert error.value.detail.code == "plugin_load_failed"
    assert loader.cleanup_calls == 1


@pytest.mark.parametrize(
    ("mutation", "category"),
    [
        (
            lambda snapshot: replace(snapshot, protocol_version="2025-11-25"),
            ErrorCategory.VERSION_MISMATCH,
        ),
        (
            lambda snapshot: replace(snapshot, tools=()),
            ErrorCategory.UNSUPPORTED_CAPABILITY,
        ),
        (
            lambda snapshot: replace(
                snapshot,
                tools=(replace(snapshot.tools[0], input_schema={"changed": True}),),
            ),
            ErrorCategory.VERSION_MISMATCH,
        ),
        (lambda snapshot: object(), ErrorCategory.PROTOCOL_FAILURE),
    ],
)
@pytest.mark.asyncio
async def test_discovery_failures_precede_remote_business_effect(
    mutation: Callable[[McpDiscoverySnapshot], object],
    category: ErrorCategory,
) -> None:
    runtime = await mcp_runtime(FakeStdioTransport)
    runtime.transport.snapshot = mutation(discovery_snapshot())
    with pytest.raises(CoreError) as error:
        await runtime.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "blocked"}), call_context()
        )
    assert error.value.detail.category is category
    assert runtime.transport.discovery_calls == 1
    assert not runtime.transport.tool_calls
    assert MCP_DISCOVERY_FAILED in {item[0] for item in runtime.events.events}


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_local_validation_and_default_denial_have_zero_transport_effect(
    transport_type: type[FakeTransport],
) -> None:
    runtime = await mcp_runtime(transport_type)
    with pytest.raises(CoreError) as invalid:
        await runtime.tool_gateway.execute(
            ToolCall(TOOL_REF, {"bad": True}), call_context()
        )
    assert invalid.value.detail.category is ErrorCategory.PROTOCOL_FAILURE
    assert runtime.transport.discovery_calls == 0
    assert not runtime.transport.tool_calls

    denied = await mcp_runtime(transport_type, default_deny=True)
    with pytest.raises(CoreError) as denial:
        await denied.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "blocked"}), call_context()
        )
    assert denial.value.detail.category is ErrorCategory.DENIED
    assert denied.transport.discovery_calls == 0
    assert not denied.transport.tool_calls


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_retry_uses_stable_identity_and_transport_never_retries_implicitly(
    transport_type: type[FakeTransport],
) -> None:
    runtime = await mcp_runtime(transport_type)
    runtime.transport.failures = 1
    result = await runtime.tool_gateway.execute(
        ToolCall(TOOL_REF, {"value": "retry"}), call_context()
    )
    assert result.attempts == 2
    assert [item.attempt for item in runtime.transport.tool_calls] == [1, 2]
    assert {item.operation_identity for item in runtime.transport.tool_calls} == {
        "idempotency-1"
    }
    assert runtime.transport.discovery_calls == 2
    assert runtime.transport.cleaned == 2


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_malformed_and_local_output_failures_are_normalized(
    transport_type: type[FakeTransport],
) -> None:
    malformed = await mcp_runtime(transport_type)
    malformed.transport.malformed_tool = True
    with pytest.raises(CoreError) as malformed_error:
        await malformed.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "x"}), call_context()
        )
    assert malformed_error.value.detail.category is ErrorCategory.PROTOCOL_FAILURE

    invalid = await mcp_runtime(transport_type)
    invalid.transport.invalid_tool_output = True
    with pytest.raises(CoreError) as schema_error:
        await invalid.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "x"}), call_context()
        )
    assert schema_error.value.detail.code == "schema_validation_failed"
    assert await invalid.lifecycle.active_lease_count(OWNER) == 0
    assert MCP_INVOCATION_FAILED in {item[0] for item in malformed.events.events}


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_timeout_cancellation_late_result_cleanup_and_drain(
    transport_type: type[FakeTransport],
) -> None:
    timed = await mcp_runtime(transport_type, timeout_ms=1)
    timed.transport.block_tool = True
    timed.transport.cleanup_failure = True
    with pytest.raises(CoreError) as timeout:
        await timed.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "slow"}), call_context()
        )
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT
    assert timed.transport.cleaned == 1
    assert await timed.lifecycle.active_lease_count(OWNER) == 0

    cancelled = await mcp_runtime(transport_type)
    cancelled.transport.block_tool = True
    context = call_context()
    task = asyncio.create_task(
        cancelled.tool_gateway.execute(ToolCall(TOOL_REF, {"value": "cancel"}), context)
    )
    await cancelled.transport.entered.wait()
    context.cancellation.cancel()
    with pytest.raises(CoreError) as cancellation:
        await task
    assert cancellation.value.detail.category is ErrorCategory.CANCELLED
    assert cancelled.transport.cleaned == 1
    assert await cancelled.lifecycle.active_lease_count(OWNER) == 0

    draining = await mcp_runtime(transport_type)
    draining.transport.block_tool = True
    first_context = call_context()
    first = asyncio.create_task(
        draining.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "first"}), first_context
        )
    )
    await draining.transport.entered.wait()
    record = await draining.lifecycle.get(OWNER)
    await draining.lifecycle.begin_draining(
        OWNER, expected_version=record.state_version
    )
    second_context = replace(
        call_context(), idempotency_key=IdempotencyKey("idempotency-2")
    )
    with pytest.raises(CoreError) as drain_error:
        await draining.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "second"}), second_context
        )
    assert drain_error.value.detail.code == "plugin_draining"
    assert len(draining.transport.tool_calls) == 1
    draining.transport.release.set()
    assert (await first).output == {"ok": "first"}
    assert await draining.lifecycle.active_lease_count(OWNER) == 0


@pytest.mark.parametrize(
    "transport_type", [FakeStdioTransport, FakeStreamableHttpTransport]
)
@pytest.mark.asyncio
async def test_context_schema_failure_releases_provider_lease(
    transport_type: type[FakeTransport],
) -> None:
    runtime = await mcp_runtime(transport_type)
    runtime.transport.invalid_context = True
    with pytest.raises(CoreError) as error:
        await runtime.context_resolver.resolve(
            runtime.context_binding.request(DefinitionId("agent-1"), call_context()),
            runtime.context_binding,
        )
    assert error.value.detail.code == "schema_validation_failed"
    assert await runtime.lifecycle.active_lease_count(OWNER) == 0
    assert runtime.transport.resource_calls


@pytest.mark.asyncio
async def test_invalid_grant_stops_before_transport_acquisition() -> None:
    runtime = await mcp_runtime(
        FakeStdioTransport,
        policy=RecordingPolicy(constraints={"max_attempts": 3}),
    )
    with pytest.raises(CoreError) as error:
        await runtime.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "blocked"}), call_context()
        )
    assert error.value.detail.category is ErrorCategory.DENIED
    assert runtime.transport.discovery_calls == 0
    assert not runtime.transport.tool_calls
    assert MCP_DISCOVERY_FAILED not in {item[0] for item in runtime.events.events}


@pytest.mark.asyncio
async def test_scope_broadening_grant_stops_before_transport_acquisition() -> None:
    runtime = await mcp_runtime(FakeStdioTransport, policy=BroadeningPolicy())
    with pytest.raises(CoreError) as error:
        await runtime.tool_gateway.execute(
            ToolCall(TOOL_REF, {"value": "blocked"}),
            call_context(scope=child_scope()),
        )
    assert error.value.detail.category is ErrorCategory.DENIED
    assert runtime.transport.discovery_calls == 0
    assert not runtime.transport.tool_calls
