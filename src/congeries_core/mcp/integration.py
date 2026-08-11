"""MCP facades that compose existing Tool and Context boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Never, cast

from congeries_core.plugin.invocation import PluginCapabilityInvoker
from congeries_core.plugin.registry import CapabilityRegistration
from congeries_core.policy.authorization import (
    ActionRef,
    AuthorizedCall,
    CorePrincipalKind,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.provider.context import (
    CONTEXT_CAPABILITIES_ACTION,
    CONTEXT_PROVIDE_ACTION,
    ContextBudget,
    ContextCapabilities,
    ContextCompleteness,
    ContextEntry,
    ContextRequest,
    ContextResult,
    ContextUsage,
)
from congeries_core.provider.events import (
    NullProviderEventPublisher,
    ProviderEventPublisher,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import IdempotencyKey, PrincipalId
from congeries_core.runtime.json_types import JsonValue, as_json_value, as_object
from congeries_core.runtime.schema import SchemaRegistry
from congeries_core.tool.model import TOOL_EXECUTE_ACTION, ToolCall, ToolDescriptor

from .mapping import (
    validate_context_binding,
    validate_discovery,
    validate_tool_binding,
)
from .model import (
    McpAdapterImplementation,
    McpContextBinding,
    McpDiscoverySnapshot,
    McpResourceRequest,
    McpResourceResponse,
    McpToolBinding,
    McpToolRequest,
    McpToolResponse,
)

MCP_DISCOVERY_STARTED = "core.mcp.discovery_started"
MCP_DISCOVERY_COMPLETED = "core.mcp.discovery_completed"
MCP_DISCOVERY_FAILED = "core.mcp.discovery_failed"
MCP_INVOCATION_STARTED = "core.mcp.invocation_started"
MCP_INVOCATION_COMPLETED = "core.mcp.invocation_completed"
MCP_INVOCATION_FAILED = "core.mcp.invocation_failed"


def mcp_actions() -> tuple[ActionRef, ActionRef, ActionRef]:
    """Return the existing Actions reused by MCP-backed facades."""

    return (
        TOOL_EXECUTE_ACTION,
        CONTEXT_CAPABILITIES_ACTION,
        CONTEXT_PROVIDE_ACTION,
    )


class _McpClient:
    """Private bridge from authorized local facades to an untrusted transport.

    It owns discovery validation, task control, error normalization, and redacted
    MCP observability. Public callers cannot obtain this client; they must enter
    through ToolGateway or ContextResolver first.
    """

    def __init__(
        self,
        implementation: McpAdapterImplementation,
        clock: Clock,
        events: ProviderEventPublisher,
    ) -> None:
        self._implementation = implementation
        self._clock = clock
        self._events = events

    async def discover(self, context: RuntimeCallContext) -> McpDiscoverySnapshot:
        descriptor = self._implementation.descriptor
        started_at = self._clock.now()
        await self._emit(
            MCP_DISCOVERY_STARTED,
            context,
            self._base_payload("discovery", 0, 0),
        )
        try:
            raw = cast(
                object,
                await self._transport_call(
                    self._implementation.transport.discover(context), context
                ),
            )
            if not isinstance(raw, McpDiscoverySnapshot):
                raise _protocol_failure(
                    "mcp_malformed_discovery", "MCP discovery response is malformed"
                )
            snapshot = validate_discovery(descriptor, raw)
            await self._emit(
                MCP_DISCOVERY_COMPLETED,
                context,
                self._base_payload(
                    "discovery",
                    len(snapshot.tools),
                    len(snapshot.resources),
                    latency=self._elapsed_ms(started_at),
                ),
            )
            return snapshot
        except CoreError as error:
            await self._emit_failure(
                MCP_DISCOVERY_FAILED, "discovery", context, started_at, error
            )
            raise

    async def call_tool(
        self,
        binding: McpToolBinding,
        arguments: JsonValue,
        context: RuntimeCallContext,
        attempt: int,
    ) -> JsonValue:
        # Revalidate discovery before every attempt. This is intentionally not a
        # transport retry: ToolGateway alone decides whether another attempt may
        # happen, and it passes the stable operation identity and attempt number.
        await self.discover(context)
        identity = _operation_identity(context)
        started_at = self._clock.now()
        await self._emit(
            MCP_INVOCATION_STARTED,
            context,
            self._base_payload("tool", 1, 0, attempt=attempt),
        )
        try:
            raw = cast(
                object,
                await self._transport_call(
                    self._implementation.transport.call_tool(
                        McpToolRequest(
                            service_id=self._implementation.descriptor.service_id,
                            name=binding.remote_name,
                            arguments=arguments,
                            operation_identity=identity,
                            attempt=attempt,
                        ),
                        context,
                    ),
                    context,
                ),
            )
            if not isinstance(raw, McpToolResponse):
                raise _protocol_failure(
                    "mcp_malformed_tool_response", "MCP Tool response is malformed"
                )
            await self._emit(
                MCP_INVOCATION_COMPLETED,
                context,
                self._base_payload(
                    "tool", 1, 0, attempt=attempt, latency=self._elapsed_ms(started_at)
                ),
            )
            return raw.output
        except CoreError as error:
            await self._emit_failure(
                MCP_INVOCATION_FAILED,
                "tool",
                context,
                started_at,
                error,
                attempt=attempt,
            )
            raise

    async def read_resource(
        self,
        binding: McpContextBinding,
        keys: tuple[str, ...],
        context: RuntimeCallContext,
    ) -> McpResourceResponse:
        snapshot = await self.discover(context)
        expected = next(
            item for item in snapshot.resources if item.uri == binding.resource_uri
        )
        started_at = self._clock.now()
        await self._emit(
            MCP_INVOCATION_STARTED,
            context,
            self._base_payload("resource", 0, 1),
        )
        try:
            raw = cast(
                object,
                await self._transport_call(
                    self._implementation.transport.read_resource(
                        McpResourceRequest(
                            service_id=self._implementation.descriptor.service_id,
                            uri=binding.resource_uri,
                            keys=keys,
                            operation_identity=_operation_identity(context),
                        ),
                        context,
                    ),
                    context,
                ),
            )
            if not isinstance(raw, McpResourceResponse):
                raise _protocol_failure(
                    "mcp_malformed_resource_response",
                    "MCP resource response is malformed",
                )
            if raw.uri != binding.resource_uri or raw.media_type != expected.media_type:
                raise _protocol_failure(
                    "mcp_resource_identity_mismatch",
                    "MCP resource response identity is invalid",
                )
            await self._emit(
                MCP_INVOCATION_COMPLETED,
                context,
                self._base_payload(
                    "resource", 0, 1, latency=self._elapsed_ms(started_at)
                ),
            )
            return raw
        except CoreError as error:
            await self._emit_failure(
                MCP_INVOCATION_FAILED, "resource", context, started_at, error
            )
            raise

    async def _transport_call[T](
        self, operation: Awaitable[T], context: RuntimeCallContext
    ) -> T:
        # await_provider cancels and awaits the concrete transport Task on
        # timeout/cancellation. Normalize anything transport-specific only after
        # that cleanup, without exposing exception text, frames, or credentials.
        try:
            return await await_provider(operation, context, self._clock)
        except CoreError:
            raise
        except asyncio.CancelledError as error:
            raise core_error(
                ErrorCategory.CANCELLED,
                "mcp_transport_cancelled",
                "MCP transport operation was cancelled",
            ) from error
        except Exception as error:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "mcp_transport_unavailable",
                "MCP transport is unavailable",
                retryable=True,
                cause_id=type(error).__name__,
            ) from error

    def _base_payload(
        self,
        operation: str,
        tool_count: int,
        resource_count: int,
        *,
        attempt: int = 0,
        latency: int = 0,
    ) -> dict[str, JsonValue]:
        descriptor = self._implementation.descriptor
        return {
            "adapter_id": descriptor.ref.id.value,
            "service_id": descriptor.service_id,
            "transport_kind": self._implementation.transport.kind,
            "operation": operation,
            "tool_count": tool_count,
            "resource_count": resource_count,
            "attempt": attempt,
            "latency_ms": latency,
        }

    async def _emit_failure(
        self,
        event_type: str,
        operation: str,
        context: RuntimeCallContext,
        started_at: datetime,
        error: CoreError,
        *,
        attempt: int = 0,
    ) -> None:
        payload = self._base_payload(
            operation, 0, 0, attempt=attempt, latency=self._elapsed_ms(started_at)
        )
        payload.update(
            {"category": error.detail.category.value, "error_code": error.detail.code}
        )
        await self._emit(event_type, context, payload)

    async def _emit(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with suppress(Exception):
            await self._events.provider_event(event_type, context, payload)

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((self._clock.now() - started_at).total_seconds() * 1_000))


@dataclass(frozen=True, slots=True)
class McpToolExecutor:
    """Internal executor for one ordinary local Tool bound to MCP."""

    descriptor: ToolDescriptor
    adapter: McpAdapterImplementation
    clock: Clock
    events: ProviderEventPublisher | None = None

    def __post_init__(self) -> None:
        binding = self.adapter.descriptor.tool_binding(self.descriptor.ref)
        validate_tool_binding(binding, self.descriptor)

    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> JsonValue:
        return await self.execute_attempt(call, context, 1)

    async def execute_attempt(
        self, call: ToolCall, context: RuntimeCallContext, attempt: int
    ) -> JsonValue:
        if call.tool != self.descriptor.ref:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "mcp_tool_binding_mismatch",
                "MCP Tool call does not match its local binding",
            )
        binding = self.adapter.descriptor.tool_binding(call.tool)
        return await self._client().call_tool(binding, call.input, context, attempt)

    def _client(self) -> _McpClient:
        return _McpClient(
            self.adapter,
            self.clock,
            self.events or NullProviderEventPublisher(),
        )


@dataclass(frozen=True, slots=True)
class McpContextProviderImplementation:
    """Lease-protected Plugin implementation behind the Context facade.

    This object may touch the transport, so it stays opaque until
    PluginCapabilityInvoker has authorized the operation and acquired a lease.
    """

    adapter: McpAdapterImplementation
    binding: McpContextBinding
    schemas: SchemaRegistry
    clock: Clock
    events: ProviderEventPublisher | None = None

    def __post_init__(self) -> None:
        declared = self.adapter.descriptor.context_binding(self.binding.provider_id)
        if declared != self.binding:
            raise ValueError("MCP Context implementation binding is not declared")
        validate_context_binding(self.binding, self.schemas)

    async def capabilities(self, context: RuntimeCallContext) -> ContextCapabilities:
        await self._client().discover(context)
        return ContextCapabilities(
            provider_id=self.binding.provider_id,
            contract_version="1",
            supported=self.binding.requirements,
            supports_partial=False,
        )

    async def provide(self, request: ContextRequest) -> ContextResult:
        allowed = {item.key: item for item in self.binding.requirements}
        if any(allowed.get(item.key) != item for item in request.requirements):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "mcp_context_binding_mismatch",
                "MCP Context request is outside its explicit binding",
            )
        keys = tuple(item.key.wire_name for item in request.requirements)
        response = await self._client().read_resource(
            self.binding, keys, request.context
        )
        try:
            contents = as_object(response.contents, "MCP resource contents")
        except ValueError as error:
            raise _protocol_failure(
                "mcp_malformed_resource_contents",
                "MCP resource contents must be an object",
            ) from error
        if set(contents) != set(keys):
            raise _protocol_failure(
                "mcp_resource_keys_mismatch",
                "MCP resource contents do not match requested Context keys",
            )
        entries: list[ContextEntry] = []
        for requirement in request.requirements:
            value = as_json_value(
                contents[requirement.key.wire_name], "MCP Context value"
            )
            self.schemas.validate(requirement.schema, value)
            entries.append(
                ContextEntry(
                    key=requirement.key,
                    schema=requirement.schema,
                    value=value,
                    provenance=(self.binding.resource_uri,),
                )
            )
        encoded = json.dumps(
            [item.value for item in entries],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        usage = ContextUsage(byte_count=len(encoded))
        _enforce_budget(request.budget, usage)
        return ContextResult(
            provider_id=self.binding.provider_id,
            contract_version="1",
            entries=tuple(entries),
            completeness=ContextCompleteness.COMPLETE,
            usage=usage,
        )

    def _client(self) -> _McpClient:
        return _McpClient(
            self.adapter,
            self.clock,
            self.events or NullProviderEventPublisher(),
        )


@dataclass(frozen=True, slots=True)
class McpContextProviderFacade:
    """Public ContextProvider shape that re-enters the Plugin safety boundary.

    ContextResolver owns the outer Context policy and merge semantics. This
    facade adds the mapped Plugin permission, grant-narrowing, and execution
    lease without exposing a direct MCP resource-read gateway.
    """

    binding: McpContextBinding
    invoker: PluginCapabilityInvoker

    async def capabilities(self, context: RuntimeCallContext) -> ContextCapabilities:
        invocation_context = _child_invocation_context(context, "capabilities")

        async def operation(
            registration: CapabilityRegistration, authorized: AuthorizedCall
        ) -> ContextCapabilities:
            implementation = _context_implementation(registration, self.binding)
            return await implementation.capabilities(authorized.context)

        result = cast(
            object,
            await self.invoker.invoke(
                plugin_id=self.binding.provider.owning_extension,
                capability_key=self.binding.provider.registration_key,
                action=CONTEXT_CAPABILITIES_ACTION,
                resource=self.binding.provider.resource,
                context=invocation_context,
                principal=_principal(invocation_context),
                constraints={
                    "keys": [item.key.wire_name for item in self.binding.requirements]
                },
                operation=operation,
            ),
        )
        if not isinstance(result, ContextCapabilities):
            raise _protocol_failure(
                "invalid_mcp_context_capabilities",
                "MCP Context implementation returned invalid capabilities",
            )
        return result

    async def provide(self, request: ContextRequest) -> ContextResult:
        invocation_context = _child_invocation_context(request.context, "provide")

        async def operation(
            registration: CapabilityRegistration, authorized: AuthorizedCall
        ) -> ContextResult:
            implementation = _context_implementation(registration, self.binding)
            constrained = _constrain_context_request(request, authorized)
            return await implementation.provide(constrained)

        result = cast(
            object,
            await self.invoker.invoke(
                plugin_id=self.binding.provider.owning_extension,
                capability_key=self.binding.provider.registration_key,
                action=CONTEXT_PROVIDE_ACTION,
                resource=self.binding.provider.resource,
                context=invocation_context,
                principal=_principal(invocation_context),
                constraints={
                    "keys": [item.key.wire_name for item in request.requirements],
                    "max_bytes": request.budget.max_bytes,
                    "max_tokens": request.budget.max_tokens,
                },
                operation=operation,
            ),
        )
        if not isinstance(result, ContextResult):
            raise _protocol_failure(
                "invalid_mcp_context_result",
                "MCP Context implementation returned an invalid result",
            )
        return result


def _context_implementation(
    registration: CapabilityRegistration, binding: McpContextBinding
) -> McpContextProviderImplementation:
    implementation = registration.implementation
    if (
        not isinstance(implementation, McpContextProviderImplementation)
        or implementation.binding != binding
    ):
        raise _protocol_failure(
            "invalid_mcp_context_implementation",
            "MCP Context implementation does not match its registration",
        )
    return implementation


def _constrain_context_request(
    request: ContextRequest, call: AuthorizedCall
) -> ContextRequest:
    constraints = call.grant.constraints
    allowed = {"keys", "max_bytes", "max_tokens"}
    if set(constraints).difference(allowed):
        _invalid_grant("MCP Context grant contains unknown constraints")
    requirements = request.requirements
    raw_keys = constraints.get("keys")
    if raw_keys is not None:
        if not isinstance(raw_keys, list) or not all(
            isinstance(item, str) for item in raw_keys
        ):
            _invalid_grant("MCP Context key constraint is invalid")
        keys = cast(list[str], raw_keys)
        requested = {item.key.wire_name for item in requirements}
        if not set(keys).issubset(requested):
            _invalid_grant("MCP Context grant broadens requested keys")
        requirements = tuple(
            item for item in requirements if item.key.wire_name in keys
        )
        if not requirements:
            _invalid_grant("MCP Context grant permits no requested keys")
    budget = ContextBudget(
        max_bytes=_narrow_limit(
            "max_bytes", request.budget.max_bytes, constraints.get("max_bytes")
        ),
        max_tokens=_narrow_limit(
            "max_tokens", request.budget.max_tokens, constraints.get("max_tokens")
        ),
    )
    return replace(
        request, context=call.context, requirements=requirements, budget=budget
    )


def _narrow_limit(
    name: str, requested: int | None, raw: JsonValue | None
) -> int | None:
    if raw is None:
        return requested
    if isinstance(raw, bool) or not isinstance(raw, int):
        _invalid_grant(f"MCP Context {name} constraint is invalid")
    if raw < 1 or (requested is not None and raw > requested):
        _invalid_grant(f"MCP Context {name} grant broadens the request")
    return raw


def _enforce_budget(budget: ContextBudget, usage: ContextUsage) -> None:
    if budget.max_bytes is not None and usage.byte_count > budget.max_bytes:
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "context_byte_budget_exceeded",
            "MCP Context result exceeds its byte budget",
        )
    if budget.max_tokens is not None:
        raise core_error(
            ErrorCategory.UNSUPPORTED_CAPABILITY,
            "context_token_usage_unavailable",
            "MCP Context does not report token usage",
        )


def _operation_identity(context: RuntimeCallContext) -> str:
    if context.idempotency_key is None:
        raise core_error(
            ErrorCategory.INVALID_REQUEST,
            "missing_invocation_identity",
            "MCP operation requires an invocation identity",
        )
    return context.idempotency_key.value


def _child_invocation_context(
    context: RuntimeCallContext, operation: str
) -> RuntimeCallContext:
    identity = _operation_identity(context)
    # ContextResolver has already used the caller identity for its outer provider
    # operation. Derive a stable, operation-specific child identity so the nested
    # Plugin reservation neither collides with that call nor changes on recovery.
    return replace(
        context,
        idempotency_key=IdempotencyKey(f"{identity}:mcp-context:{operation}"),
    )


def _principal(context: RuntimeCallContext) -> RuntimePrincipal:
    return RuntimePrincipal.core(
        CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
    )


def _protocol_failure(code: str, message: str) -> CoreError:
    return core_error(ErrorCategory.PROTOCOL_FAILURE, code, message)


def _invalid_grant(message: str) -> Never:
    raise core_error(ErrorCategory.DENIED, "invalid_grant", message)
