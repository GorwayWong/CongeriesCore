"""Redacted Plugin lifecycle observability and reliable audit events."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol

from congeries_core.event.dispatcher import EventDispatcher
from congeries_core.event.model import (
    CoreEventType,
    DeliveryClass,
    PayloadField,
    Sensitivity,
)
from congeries_core.policy.authorization import (
    CorePrincipalKind,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorDetail
from congeries_core.runtime.ids import EventId, PrincipalId
from congeries_core.runtime.json_types import JsonValue

from .model import PluginLifecycleState, PluginRef


class PluginEventPublisher(Protocol):
    async def transition_requested(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        context: RuntimeCallContext,
    ) -> None: ...

    async def lifecycle_changed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        context: RuntimeCallContext,
    ) -> None: ...

    async def lifecycle_failed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        error: ErrorDetail,
        context: RuntimeCallContext,
    ) -> None: ...


class NullPluginEventPublisher:
    async def transition_requested(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def lifecycle_changed(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def lifecycle_failed(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


class RuntimePluginEventPublisher:
    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def transition_requested(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        context: RuntimeCallContext,
    ) -> None:
        await self._publish(
            CoreEventType.PLUGIN_LIFECYCLE_TRANSITION_REQUESTED,
            DeliveryClass.AUDIT,
            plugin,
            from_state,
            to_state,
            operation_id,
            active_lease_count,
            "requested",
            context,
        )

    async def lifecycle_changed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        context: RuntimeCallContext,
    ) -> None:
        await self._publish(
            CoreEventType.PLUGIN_LIFECYCLE_CHANGED,
            DeliveryClass.OBSERVABILITY,
            plugin,
            from_state,
            to_state,
            operation_id,
            active_lease_count,
            "changed",
            context,
        )

    async def lifecycle_failed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        error: ErrorDetail,
        context: RuntimeCallContext,
    ) -> None:
        await self._publish(
            CoreEventType.PLUGIN_LIFECYCLE_FAILED,
            DeliveryClass.AUDIT,
            plugin,
            from_state,
            to_state,
            operation_id,
            active_lease_count,
            "failed",
            context,
            error_code=error.code,
        )

    async def _publish(
        self,
        event_type: CoreEventType,
        delivery_class: DeliveryClass,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        outcome: str,
        context: RuntimeCallContext,
        *,
        error_code: str | None = None,
    ) -> None:
        values: dict[str, JsonValue] = {
            "plugin_id": plugin.name,
            "plugin_version": str(plugin.version),
            "from_state": from_state,
            "to_state": to_state.value,
            "operation_id": operation_id,
            "active_lease_count": active_lease_count,
            "outcome": outcome,
        }
        if error_code is not None:
            values["error_code"] = error_code
        event = await self._dispatcher.create_event(
            event_type=event_type.value,
            schema_version="1",
            run_id=context.run_id,
            root_run_id=context.root_run_id,
            parent_run_id=context.parent_run_id,
            scope=context.scope,
            context=context,
            sensitivity=Sensitivity.INTERNAL,
            delivery_class=delivery_class,
            payload=_payload(values),
            event_id=(
                # Reliable AUDIT retries reuse one logical identity for outbox
                # deduplication. Observability emissions intentionally receive a
                # fresh identity so distinct committed transitions are not folded.
                EventId(_audit_event_id(event_type, plugin, operation_id, outcome))
                if delivery_class is DeliveryClass.AUDIT
                else None
            ),
        )
        principal = RuntimePrincipal.core(
            CorePrincipalKind.RUN,
            PrincipalId(context.run_id.value),
        )
        await self._dispatcher.publish(event, context, principal)


def _payload(values: Mapping[str, JsonValue]) -> dict[str, PayloadField]:
    return {key: PayloadField(value) for key, value in values.items()}


def _audit_event_id(
    event_type: CoreEventType,
    plugin: PluginRef,
    operation_id: str,
    outcome: str,
) -> str:
    digest = hashlib.sha256(
        "\x1f".join(
            (event_type.value, plugin.name, str(plugin.version), operation_id, outcome)
        ).encode()
    ).hexdigest()
    return "plugin-audit:" + digest
