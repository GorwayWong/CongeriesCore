"""Authorized, schema-aware Tool v1 execution."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta

from congeries_core.plugin.invocation import PluginCapabilityInvoker
from congeries_core.plugin.registry import CapabilityRegistration
from congeries_core.policy.authorization import (
    ActionRef,
    AuthorizedCall,
    CorePrincipalKind,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.provider.events import (
    NullProviderEventPublisher,
    ProviderEventPublisher,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock, Deadline
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import PrincipalId
from congeries_core.runtime.json_types import JsonValue, as_json_value
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry

from .model import ToolCall, ToolDescriptor, ToolImplementation, ToolResult
from .registry import ToolRegistry

TOOL_INVOCATION_STARTED = "core.tool.invocation_started"
TOOL_INVOCATION_COMPLETED = "core.tool.invocation_completed"
TOOL_INVOCATION_FAILED = "core.tool.invocation_failed"


class ToolGateway:
    def __init__(
        self,
        *,
        tools: ToolRegistry,
        invoker: PluginCapabilityInvoker,
        schemas: SchemaRegistry,
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._tools = tools
        self._invoker = invoker
        self._schemas = schemas
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def execute(self, call: ToolCall, context: RuntimeCallContext) -> ToolResult:
        started_at = self._clock.now()
        if context.idempotency_key is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "missing_invocation_identity",
                "Tool execution requires an invocation identity",
            )
        operation_identity = context.idempotency_key.value
        resolved = self._tools.resolve(call.tool)
        descriptor = resolved.descriptor
        self._schemas.validate(descriptor.input_schema, call.input)
        await self._emit(TOOL_INVOCATION_STARTED, context, self._payload(call, 0))
        constraints: Mapping[str, JsonValue] = {
            "input_schema": self._schema_identity(descriptor.input_schema),
            "output_schema": self._schema_identity(descriptor.output_schema),
            "action": self._action_identity(descriptor.action),
            "side_effect": descriptor.side_effect.value,
            "idempotency": descriptor.idempotency.value,
            "timeout_ms": descriptor.execution_policy.timeout_ms,
            "max_attempts": descriptor.execution_policy.max_attempts,
        }
        try:

            async def operation(
                registration: CapabilityRegistration, authorized: AuthorizedCall
            ) -> ToolResult:
                # One Plugin lease covers grant validation, every retry, and
                # output-schema validation. Retries therefore reuse the same
                # operation identity instead of becoming new side effects.
                timeout_ms, max_attempts = self._validate_grant(descriptor, authorized)
                implementation = registration.implementation
                if not isinstance(implementation, ToolImplementation):
                    raise self._protocol_failure("Tool implementation is invalid")
                effective_context = self._with_timeout(
                    authorized.context, timeout_ms, started_at
                )
                attempts = 0
                while True:
                    attempts += 1
                    try:
                        output = await self._execute_attempt(
                            implementation, call, effective_context
                        )
                        normalized = as_json_value(output, "Tool output")
                        self._schemas.validate(descriptor.output_schema, normalized)
                        return ToolResult(
                            tool=call.tool,
                            output=normalized,
                            attempts=attempts,
                            operation_identity=operation_identity,
                        )
                    except CoreError as error:
                        if not error.detail.retryable or attempts >= max_attempts:
                            raise
                    except Exception as error:
                        unavailable = core_error(
                            ErrorCategory.UNAVAILABLE,
                            "tool_executor_failure",
                            "Tool executor failed",
                            retryable=True,
                            cause_id=type(error).__name__,
                        )
                        if attempts >= max_attempts:
                            raise unavailable from error

            result = await self._invoker.invoke(
                plugin_id=call.tool.owning_extension,
                capability_key=call.tool.registration_key,
                action=descriptor.action,
                resource=call.tool.resource,
                context=context,
                principal=RuntimePrincipal.core(
                    CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
                ),
                constraints=constraints,
                operation=operation,
            )
            await self._emit(
                TOOL_INVOCATION_COMPLETED,
                context,
                self._payload(
                    call, result.attempts, latency=self._elapsed_ms(started_at)
                ),
            )
            return result
        except CoreError as error:
            await self._emit_failure(call, context, started_at, error.detail)
            raise

    def _validate_grant(
        self, descriptor: ToolDescriptor, call: AuthorizedCall
    ) -> tuple[int | None, int]:
        constraints = call.grant.constraints
        allowed = {
            "input_schema",
            "output_schema",
            "action",
            "side_effect",
            "idempotency",
            "timeout_ms",
            "max_attempts",
        }
        if set(constraints).difference(allowed):
            raise self._invalid_grant("Tool grant contains unknown constraints")
        fixed = {
            "input_schema": self._schema_identity(descriptor.input_schema),
            "output_schema": self._schema_identity(descriptor.output_schema),
            "action": self._action_identity(descriptor.action),
            "side_effect": descriptor.side_effect.value,
            "idempotency": descriptor.idempotency.value,
        }
        for key, value in fixed.items():
            if constraints.get(key, value) != value:
                raise self._invalid_grant(f"Tool grant changes {key}")
        policy = descriptor.execution_policy
        raw_attempts = constraints.get("max_attempts", policy.max_attempts)
        if isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
            raise self._invalid_grant("Tool max_attempts grant is invalid")
        if raw_attempts < 1 or raw_attempts > policy.max_attempts:
            raise self._invalid_grant("Tool grant broadens max_attempts")
        raw_timeout = constraints.get("timeout_ms", policy.timeout_ms)
        if raw_timeout is not None and (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, int)
            or raw_timeout < 1
        ):
            raise self._invalid_grant("Tool timeout_ms grant is invalid")
        if policy.timeout_ms is not None and (
            raw_timeout is None or raw_timeout > policy.timeout_ms
        ):
            raise self._invalid_grant("Tool grant broadens timeout_ms")
        # A descriptor value of None means "no Tool-specific limit", not
        # "timeouts forbidden". A finite grant is therefore a valid narrowing.
        return raw_timeout, raw_attempts

    def _with_timeout(
        self,
        context: RuntimeCallContext,
        timeout_ms: int | None,
        started_at: datetime,
    ) -> RuntimeCallContext:
        if timeout_ms is None:
            return context
        # started_at is captured at execute entry, so resolution, validation,
        # authorization, lease acquisition, retries, and output validation all
        # spend the same whole-invocation timeout budget.
        deadline = Deadline(started_at + timedelta(milliseconds=timeout_ms))
        if context.deadline is not None and context.deadline.at <= deadline.at:
            return context
        return context.narrow(deadline=deadline)

    async def _execute_attempt(
        self,
        implementation: ToolImplementation,
        call: ToolCall,
        context: RuntimeCallContext,
    ) -> JsonValue:
        # Owning the Task explicitly lets timeout/cancellation await its teardown;
        # no late executor completion can escape into a later retry or lease.
        execution = asyncio.create_task(implementation.executor.execute(call, context))
        try:
            return await await_provider(execution, context, self._clock)
        finally:
            if not execution.done():
                execution.cancel()
            with suppress(BaseException):
                await execution

    def _schema_identity(self, schema: SchemaRef) -> str:
        return ":".join(schema.key)

    def _action_identity(self, action: ActionRef) -> str:
        return ":".join(action.key)

    def _payload(
        self, call: ToolCall, attempts: int, *, latency: int = 0
    ) -> Mapping[str, JsonValue]:
        return {
            "tool_id": call.tool.id.value,
            "attempts": attempts,
            "latency_ms": latency,
        }

    async def _emit_failure(
        self,
        call: ToolCall,
        context: RuntimeCallContext,
        started_at: datetime,
        error: ErrorDetail,
    ) -> None:
        payload = dict(self._payload(call, 0, latency=self._elapsed_ms(started_at)))
        payload.update({"category": error.category.value, "error_code": error.code})
        await self._emit(TOOL_INVOCATION_FAILED, context, payload)

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

    def _protocol_failure(self, message: str) -> CoreError:
        return core_error(
            ErrorCategory.PROTOCOL_FAILURE, "tool_protocol_failure", message
        )

    def _invalid_grant(self, message: str) -> CoreError:
        return core_error(ErrorCategory.DENIED, "invalid_grant", message)
