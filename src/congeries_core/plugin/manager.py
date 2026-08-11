"""Authorized Plugin loading, invocation, drain, and unload coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import cast

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, Clock
from congeries_core.runtime.errors import CoreError, ErrorCategory
from congeries_core.runtime.ids import PrincipalId, ResourceId

from .dependency import (
    CapabilityCatalogSnapshot,
    CatalogCapability,
    DependencyResolver,
)
from .errors import plugin_error
from .events import NullPluginEventPublisher, PluginEventPublisher
from .invocation import PluginCapabilityInvoker
from .lifecycle import PluginLifecycleController, PluginStateRecord
from .loader import CompositeCapabilityImplementation, PluginLoader, PreparedPlugin
from .manifest import ManifestValidator, PluginPreflight
from .model import (
    CapabilityType,
    PluginHookName,
    PluginLifecycleState,
    PluginManifest,
    PluginPermission,
    PluginRef,
)
from .registry import (
    CapabilityKey,
    CapabilityRegistration,
    CapabilityRegistrationPlan,
    CapabilityRegistry,
)

PLUGIN_LOAD_ACTION = ActionRef("core", "plugin.load", "1")
PLUGIN_ACTIVATE_ACTION = ActionRef("core", "plugin.activate", "1")
PLUGIN_DRAIN_ACTION = ActionRef("core", "plugin.drain", "1")
PLUGIN_CANCEL_DRAIN_ACTION = ActionRef("core", "plugin.cancel_drain", "1")
PLUGIN_UNLOAD_ACTION = ActionRef("core", "plugin.unload", "1")


def plugin_actions() -> tuple[ActionRef, ...]:
    return (
        PLUGIN_LOAD_ACTION,
        PLUGIN_ACTIVATE_ACTION,
        PLUGIN_DRAIN_ACTION,
        PLUGIN_CANCEL_DRAIN_ACTION,
        PLUGIN_UNLOAD_ACTION,
    )


type CapabilityOperation[ResultT] = Callable[
    [object, RuntimeCallContext], Awaitable[ResultT]
]


class PluginManager:
    def __init__(
        self,
        *,
        validator: ManifestValidator,
        preflight: PluginPreflight,
        resolver: DependencyResolver,
        registry: CapabilityRegistry,
        lifecycle: PluginLifecycleController,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
        events: PluginEventPublisher | None = None,
    ) -> None:
        self._validator = validator
        self._preflight = preflight
        self._resolver = resolver
        self._registry = registry
        self._lifecycle = lifecycle
        self._dispatcher = dispatcher
        self._clock = clock
        self._events = events or NullPluginEventPublisher()
        self._invoker = PluginCapabilityInvoker(
            registry=registry,
            lifecycle=lifecycle,
            dispatcher=dispatcher,
            clock=clock,
        )
        self._prepared: dict[str, tuple[PreparedPlugin, PluginLoader]] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}

    def validate(self, data: Mapping[str, object]) -> PluginManifest:
        return self._validator.validate(data)

    async def load(
        self,
        data: Mapping[str, object],
        loader: PluginLoader,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> PluginStateRecord:
        manifest = self.validate(data)
        lock = self._operation_locks.setdefault(manifest.name, asyncio.Lock())
        async with lock:
            result = await self._authorized(
                PLUGIN_LOAD_ACTION,
                manifest.name,
                context,
                principal,
                lambda call: self._load_effect(manifest, loader, call.context),
            )
            return cast(PluginStateRecord, result)

    async def activate(
        self,
        plugin_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> PluginStateRecord:
        async with self._operation_lock(plugin_id):
            result = await self._authorized(
                PLUGIN_ACTIVATE_ACTION,
                plugin_id,
                context,
                principal,
                lambda call: self._activate_effect(plugin_id, call.context),
            )
            return cast(PluginStateRecord, result)

    async def drain(
        self,
        plugin_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> PluginStateRecord:
        async with self._operation_lock(plugin_id):
            result = await self._authorized(
                PLUGIN_DRAIN_ACTION,
                plugin_id,
                context,
                principal,
                lambda call: self._drain_effect(plugin_id, call.context),
            )
            return cast(PluginStateRecord, result)

    async def cancel_drain(
        self,
        plugin_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> PluginStateRecord:
        async with self._operation_lock(plugin_id):
            result = await self._authorized(
                PLUGIN_CANCEL_DRAIN_ACTION,
                plugin_id,
                context,
                principal,
                lambda call: self._cancel_drain_effect(plugin_id, call.context),
            )
            return cast(PluginStateRecord, result)

    async def unload(
        self,
        plugin_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
    ) -> PluginStateRecord:
        async with self._operation_lock(plugin_id):
            result = await self._authorized(
                PLUGIN_UNLOAD_ACTION,
                plugin_id,
                context,
                principal,
                lambda call: self._unload_effect(plugin_id, call.context),
            )
            return cast(PluginStateRecord, result)

    async def invoke[ResultT](
        self,
        plugin_id: str,
        capability_key: CapabilityKey,
        action: ActionRef,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
        operation: CapabilityOperation[ResultT],
    ) -> ResultT:
        async def adapted(
            registration: CapabilityRegistration, call: AuthorizedCall
        ) -> ResultT:
            return await operation(registration.implementation, call.context)

        return await self._invoker.invoke(
            plugin_id=plugin_id,
            capability_key=capability_key,
            action=action,
            resource=ResourceRef(
                "core",
                capability_key[0],
                ResourceId(capability_key[1]),
                owning_extension=plugin_id,
            ),
            context=context,
            principal=principal,
            operation=adapted,
        )

    async def _load_effect(
        self,
        manifest: PluginManifest,
        loader: PluginLoader,
        context: RuntimeCallContext,
    ) -> PluginStateRecord:
        operation_id = _operation_id(context, PLUGIN_LOAD_ACTION, manifest.name)
        existing = self._prepared.get(manifest.name)
        if existing is not None:
            record = await self._lifecycle.get(manifest.name)
            if record.manifest != manifest:
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "registration_identity_conflict",
                    "Plugin identity is already loaded with another manifest",
                    plugin=manifest.name,
                )
            return record
        self._preflight.check_local(manifest)
        available = await self._catalog_snapshot()
        self._resolver.resolve((manifest,), available)
        registry_snapshot = self._registry.snapshot()
        for declaration in manifest.provides:
            if declaration.key in registry_snapshot.registrations:
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "registration_conflict",
                    "Capability is already registered",
                    plugin=manifest.name,
                    capability=declaration.capability_id,
                )
        await self._events.transition_requested(
            manifest.ref,
            "absent",
            PluginLifecycleState.LOADED,
            operation_id,
            0,
            context,
        )
        record = await self._lifecycle.discover(manifest)
        await self._emit_changed(
            manifest.ref,
            "absent",
            record.state,
            operation_id,
            context,
        )
        if record.state is PluginLifecycleState.DISCOVERED:
            previous = record.state
            record = await self._lifecycle.transition(
                manifest.name,
                PluginLifecycleState.VALIDATED,
                expected_version=record.state_version,
            )
            await self._emit_changed(
                manifest.ref, previous.value, record.state, operation_id, context
            )
        if record.state is not PluginLifecycleState.VALIDATED:
            return record
        prepared: PreparedPlugin | None = None
        try:
            # Loader output remains staged and invisible until its manifest,
            # capabilities, and declared hooks match exactly. Any failure below
            # cleans these handles without publishing a registry entry.
            prepared = await await_provider(
                loader.prepare(manifest, context), context, self._clock
            )
            self._validate_prepared(prepared, manifest)
            await self._run_hook(
                prepared,
                PluginHookName.ON_LOAD,
                context,
            )
            if PluginHookName.ON_LOAD in manifest.lifecycle:
                record = await self._lifecycle.mark_hook(
                    manifest.name,
                    PluginHookName.ON_LOAD,
                )
            self._prepared[manifest.name] = prepared, loader
            previous = record.state
            record = await self._lifecycle.transition(
                manifest.name,
                PluginLifecycleState.LOADED,
                expected_version=record.state_version,
            )
            await self._emit_changed(
                manifest.ref, previous.value, record.state, operation_id, context
            )
            return record
        except CoreError as error:
            if prepared is not None:
                await self._cleanup_staged(loader, prepared, context)
            await self._emit_failed(
                manifest.ref,
                PluginLifecycleState.VALIDATED.value,
                PluginLifecycleState.LOADED,
                operation_id,
                error,
                context,
            )
            raise
        except Exception as error:
            if prepared is not None:
                await self._cleanup_staged(loader, prepared, context)
            mapped = plugin_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "plugin_load_failed",
                "Plugin loader failed",
                retryable=True,
                cause_id=type(error).__name__,
                plugin=manifest.name,
            )
            await self._emit_failed(
                manifest.ref,
                PluginLifecycleState.VALIDATED.value,
                PluginLifecycleState.LOADED,
                operation_id,
                mapped,
                context,
            )
            raise mapped from error

    async def _activate_effect(
        self, plugin_id: str, context: RuntimeCallContext
    ) -> PluginStateRecord:
        record = await self._lifecycle.get(plugin_id)
        operation_id = _operation_id(context, PLUGIN_ACTIVATE_ACTION, plugin_id)
        await self._events.transition_requested(
            record.ref,
            record.state.value,
            PluginLifecycleState.ACTIVE,
            operation_id,
            await self._lifecycle.active_lease_count(plugin_id),
            context,
        )
        if record.state is PluginLifecycleState.ACTIVE:
            return record
        prepared, _ = self._prepared_plugin(plugin_id)
        try:
            if record.state is PluginLifecycleState.LOADED:
                snapshot = self._registry.snapshot()
                # Capability publication happens before the REGISTERED state
                # commit. If that CAS loses a race, roll back this exact receipt so
                # observers never keep capabilities for a non-registered Plugin.
                receipt = self._registry.commit(
                    CapabilityRegistrationPlan(record.ref, prepared.capabilities),
                    expected_version=snapshot.version,
                )
                previous = record.state
                try:
                    record = await self._lifecycle.transition(
                        plugin_id,
                        PluginLifecycleState.REGISTERED,
                        expected_version=record.state_version,
                        receipt=receipt,
                    )
                except Exception:
                    self._registry.rollback(receipt)
                    raise
                await self._emit_changed(
                    record.ref, previous.value, record.state, operation_id, context
                )
            if record.state is not PluginLifecycleState.REGISTERED:
                raise self._invalid_state(
                    plugin_id, "activation requires LOADED or REGISTERED"
                )
            await self._authorize_activation_permissions(record.manifest, context)
            if PluginHookName.ON_ACTIVATE not in record.successful_hooks:
                await self._run_hook(
                    prepared,
                    PluginHookName.ON_ACTIVATE,
                    context,
                )
                if PluginHookName.ON_ACTIVATE in record.manifest.lifecycle:
                    record = await self._lifecycle.mark_hook(
                        plugin_id,
                        PluginHookName.ON_ACTIVATE,
                    )
            previous = record.state
            record = await self._lifecycle.transition(
                plugin_id,
                PluginLifecycleState.ACTIVE,
                expected_version=record.state_version,
            )
            await self._emit_changed(
                record.ref, previous.value, record.state, operation_id, context
            )
            return record
        except CoreError as error:
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.ACTIVE,
                operation_id,
                error,
                context,
            )
            raise
        except Exception as error:
            mapped = self._hook_failure(
                plugin_id,
                "lifecycle_hook_failed",
                "Plugin activation failed",
                error,
            )
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.ACTIVE,
                operation_id,
                mapped,
                context,
            )
            raise mapped from error

    async def _drain_effect(
        self, plugin_id: str, context: RuntimeCallContext
    ) -> PluginStateRecord:
        record = await self._lifecycle.get(plugin_id)
        operation_id = _operation_id(context, PLUGIN_DRAIN_ACTION, plugin_id)
        await self._events.transition_requested(
            record.ref,
            record.state.value,
            PluginLifecycleState.DRAINING,
            operation_id,
            await self._lifecycle.active_lease_count(plugin_id),
            context,
        )
        try:
            record = await self._ensure_draining(record, operation_id, context)
            await self._lifecycle.wait_for_zero(plugin_id, context)
            return await self._lifecycle.get(plugin_id)
        except CoreError as error:
            error = self._normalize_drain_error(plugin_id, error)
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.DRAINING,
                operation_id,
                error,
                context,
            )
            raise error
        except Exception as error:
            mapped = self._hook_failure(
                plugin_id,
                "lifecycle_hook_failed",
                "Plugin drain hook failed",
                error,
            )
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.DRAINING,
                operation_id,
                mapped,
                context,
            )
            raise mapped from error

    async def _cancel_drain_effect(
        self, plugin_id: str, context: RuntimeCallContext
    ) -> PluginStateRecord:
        record = await self._lifecycle.get(plugin_id)
        operation_id = _operation_id(context, PLUGIN_CANCEL_DRAIN_ACTION, plugin_id)
        await self._events.transition_requested(
            record.ref,
            record.state.value,
            PluginLifecycleState.ACTIVE,
            operation_id,
            await self._lifecycle.active_lease_count(plugin_id),
            context,
        )
        if record.state is not PluginLifecycleState.DRAINING:
            raise self._invalid_state(plugin_id, "cancel-drain requires DRAINING")
        if record.registration_receipt is None:
            raise self._invalid_state(
                plugin_id, "cannot reactivate unregistered Plugin"
            )
        previous = record.state
        record = await self._lifecycle.transition(
            plugin_id,
            PluginLifecycleState.ACTIVE,
            expected_version=record.state_version,
        )
        await self._emit_changed(
            record.ref, previous.value, record.state, operation_id, context
        )
        return record

    async def _unload_effect(
        self, plugin_id: str, context: RuntimeCallContext
    ) -> PluginStateRecord:
        record = await self._lifecycle.get(plugin_id)
        operation_id = _operation_id(context, PLUGIN_UNLOAD_ACTION, plugin_id)
        await self._events.transition_requested(
            record.ref,
            record.state.value,
            PluginLifecycleState.UNLOADED,
            operation_id,
            await self._lifecycle.active_lease_count(plugin_id),
            context,
        )
        if record.state is PluginLifecycleState.UNLOADED:
            return record
        prepared, loader = self._prepared_plugin(plugin_id)
        try:
            if record.state in {
                PluginLifecycleState.ACTIVE,
                PluginLifecycleState.DRAINING,
            }:
                record = await self._ensure_draining(record, operation_id, context)
                await self._lifecycle.wait_for_zero(plugin_id, context)
                record = await self._lifecycle.get(plugin_id)
            if record.state in {
                PluginLifecycleState.DRAINING,
                PluginLifecycleState.REGISTERED,
            }:
                receipt = record.registration_receipt
                if receipt is None:
                    raise self._invalid_state(
                        plugin_id, "registered Plugin lacks receipt"
                    )
                self._registry.unregister(receipt)
                previous = record.state
                record = await self._lifecycle.transition(
                    plugin_id,
                    PluginLifecycleState.UNREGISTERED,
                    expected_version=record.state_version,
                    clear_receipt=True,
                )
                await self._emit_changed(
                    record.ref, previous.value, record.state, operation_id, context
                )
            if record.state is not PluginLifecycleState.UNREGISTERED:
                raise self._invalid_state(
                    plugin_id,
                    "unload requires ACTIVE, DRAINING, REGISTERED, or UNREGISTERED",
                )
            if PluginHookName.ON_UNLOAD not in record.successful_hooks:
                await self._run_hook(prepared, PluginHookName.ON_UNLOAD, context)
                if PluginHookName.ON_UNLOAD in record.manifest.lifecycle:
                    record = await self._lifecycle.mark_hook(
                        plugin_id,
                        PluginHookName.ON_UNLOAD,
                    )
            await await_provider(
                loader.cleanup(prepared, context), context, self._clock
            )
            previous = record.state
            record = await self._lifecycle.transition(
                plugin_id,
                PluginLifecycleState.UNLOADED,
                expected_version=record.state_version,
            )
            self._prepared.pop(plugin_id, None)
            await self._emit_changed(
                record.ref, previous.value, record.state, operation_id, context
            )
            return record
        except CoreError as error:
            if (
                await self._lifecycle.get(plugin_id)
            ).state is PluginLifecycleState.DRAINING:
                error = self._normalize_drain_error(plugin_id, error)
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.UNLOADED,
                operation_id,
                error,
                context,
            )
            raise error
        except Exception as error:
            mapped = plugin_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "unload_failed",
                "Plugin unload cleanup failed",
                retryable=True,
                cause_id=type(error).__name__,
                plugin=plugin_id,
            )
            current = await self._lifecycle.get(plugin_id)
            await self._emit_failed(
                current.ref,
                current.state.value,
                PluginLifecycleState.UNLOADED,
                operation_id,
                mapped,
                context,
            )
            raise mapped from error

    async def _ensure_draining(
        self,
        record: PluginStateRecord,
        operation_id: str,
        context: RuntimeCallContext,
    ) -> PluginStateRecord:
        if record.state is PluginLifecycleState.ACTIVE:
            previous = record.state
            # Close lease admission before invoking Plugin code. Once DRAINING is
            # visible, concurrent callers are rejected while existing leases may
            # finish; successful_hooks records retry progress after partial work.
            record = await self._lifecycle.begin_draining(
                record.ref.name,
                expected_version=record.state_version,
            )
            await self._emit_changed(
                record.ref, previous.value, record.state, operation_id, context
            )
        if record.state is not PluginLifecycleState.DRAINING:
            raise self._invalid_state(
                record.ref.name, "drain requires ACTIVE or DRAINING"
            )
        prepared, _ = self._prepared_plugin(record.ref.name)
        if PluginHookName.ON_DRAIN not in record.successful_hooks:
            await self._run_hook(prepared, PluginHookName.ON_DRAIN, context)
            if PluginHookName.ON_DRAIN in record.manifest.lifecycle:
                record = await self._lifecycle.mark_hook(
                    record.ref.name,
                    PluginHookName.ON_DRAIN,
                )
        return record

    async def _run_hook(
        self,
        prepared: PreparedPlugin,
        hook_name: PluginHookName,
        context: RuntimeCallContext,
    ) -> None:
        hook = getattr(prepared.hooks, hook_name.value)
        if hook is None:
            return
        await await_provider(hook(context), context, self._clock)

    def _validate_prepared(
        self, prepared: PreparedPlugin, manifest: PluginManifest
    ) -> None:
        if prepared.manifest != manifest:
            raise plugin_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "plugin_load_failed",
                "Loader returned a different Plugin manifest",
                plugin=manifest.name,
            )
        declared = {item.key for item in manifest.provides}
        loaded = {item.declaration.key for item in prepared.capabilities}
        if loaded != declared or len(loaded) != len(prepared.capabilities):
            raise plugin_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "plugin_load_failed",
                "Loader capabilities do not exactly match the manifest",
                plugin=manifest.name,
            )
        for capability in prepared.capabilities:
            if capability.declaration.type is not CapabilityType.MCP_ADAPTER:
                continue
            implementation = capability.implementation
            if not isinstance(implementation, CompositeCapabilityImplementation):
                raise plugin_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "plugin_load_failed",
                    "MCP Adapter cannot validate its atomic capability composition",
                    plugin=manifest.name,
                    capability=capability.declaration.capability_id,
                )
            try:
                implementation.validate_composition(
                    manifest.name, prepared.capabilities
                )
            except (TypeError, ValueError) as error:
                raise plugin_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "plugin_load_failed",
                    "MCP Adapter capability composition is invalid",
                    cause_id=type(error).__name__,
                    plugin=manifest.name,
                    capability=capability.declaration.capability_id,
                ) from error
        for hook_name in PluginHookName:
            present = getattr(prepared.hooks, hook_name.value) is not None
            declared_hook = hook_name in manifest.lifecycle
            if present != declared_hook:
                raise plugin_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "plugin_load_failed",
                    "Loader hooks do not exactly match the manifest",
                    plugin=manifest.name,
                )

    async def _cleanup_staged(
        self,
        loader: PluginLoader,
        prepared: PreparedPlugin,
        context: RuntimeCallContext,
    ) -> None:
        # Best-effort staged cleanup must not replace the original load error. It
        # uses a recovery context because the initiating call may already have
        # timed out or been cancelled.
        with suppress(Exception):
            await loader.cleanup(prepared, _recovery_context(context))

    async def _catalog_snapshot(self) -> CapabilityCatalogSnapshot:
        registry = self._registry.snapshot()
        return CapabilityCatalogSnapshot(
            plugins=await self._lifecycle.active_plugin_refs(),
            capabilities=tuple(
                CatalogCapability(item.owner, item.declaration)
                for _, item in sorted(registry.registrations.items())
            ),
        )

    async def _authorize_activation_permissions(
        self,
        manifest: PluginManifest,
        context: RuntimeCallContext,
    ) -> None:
        # Activation checks permissions as the Plugin principal, not as the user
        # who requested activation. The runtime caller is authorized separately
        # on each capability invocation.
        principal = RuntimePrincipal.core(
            CorePrincipalKind.PLUGIN,
            PrincipalId(manifest.name),
        )
        requests: list[tuple[PluginPermission, ResourceRef, str | None]] = [
            (
                permission,
                ResourceRef(
                    "core",
                    "plugin",
                    ResourceId(manifest.name),
                    owning_extension=manifest.name,
                ),
                None,
            )
            for permission in manifest.permissions
        ]
        for declaration in manifest.provides:
            requests.extend(
                (
                    permission,
                    ResourceRef(
                        "core",
                        declaration.type.value,
                        ResourceId(declaration.capability_id),
                        owning_extension=manifest.name,
                    ),
                    declaration.capability_id,
                )
                for permission in declaration.permissions
            )

        for permission, resource, capability_id in requests:
            try:
                await self._dispatcher.dispatch(
                    AccessRequest(
                        principal=principal,
                        action=permission.action,
                        resource=resource,
                        scope=context.scope,
                        context=context,
                    ),
                    lambda call, requested=permission, capability=capability_id: (
                        self._allow_declared_permission(
                            requested,
                            manifest.name,
                            capability,
                            call,
                        )
                    ),
                )
            except CoreError as error:
                if error.detail.category is not ErrorCategory.DENIED:
                    raise
                raise plugin_error(
                    ErrorCategory.DENIED,
                    "permission_denied",
                    "Plugin permission request was denied",
                    cause_id=error.detail.code,
                    plugin=manifest.name,
                    capability=capability_id,
                ) from error

    async def _allow_declared_permission(
        self,
        permission: PluginPermission,
        plugin_id: str,
        capability_id: str | None,
        call: AuthorizedCall,
    ) -> object:
        if not permission.permits(call.context.scope):
            raise plugin_error(
                ErrorCategory.DENIED,
                "permission_denied",
                "Requested Plugin permission is outside its declared Scope",
                plugin=plugin_id,
                capability=capability_id,
            )
        return None

    async def _emit_changed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        context: RuntimeCallContext,
    ) -> None:
        with suppress(Exception):
            await self._events.lifecycle_changed(
                plugin,
                from_state,
                to_state,
                operation_id,
                await self._lifecycle.active_lease_count(plugin.name),
                _recovery_context(context),
            )

    async def _emit_failed(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        error: CoreError,
        context: RuntimeCallContext,
    ) -> None:
        await self._events.lifecycle_failed(
            plugin,
            from_state,
            to_state,
            operation_id,
            await self._lifecycle.active_lease_count(plugin.name),
            error.detail,
            _recovery_context(context),
        )

    async def _authorized(
        self,
        action: ActionRef,
        plugin_id: str,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
        operation: Callable[[AuthorizedCall], Awaitable[object]],
    ) -> object:
        return await self._dispatcher.dispatch(
            AccessRequest(
                principal=principal,
                action=action,
                resource=ResourceRef(
                    "core",
                    "plugin",
                    ResourceId(plugin_id),
                    owning_extension=plugin_id,
                ),
                scope=context.scope,
                context=context,
            ),
            operation,
        )

    def _prepared_plugin(self, plugin_id: str) -> tuple[PreparedPlugin, PluginLoader]:
        prepared = self._prepared.get(plugin_id)
        if prepared is None:
            raise plugin_error(
                ErrorCategory.UNAVAILABLE,
                "plugin_not_loaded",
                "Plugin has no prepared loader state",
                retryable=True,
                plugin=plugin_id,
            )
        return prepared

    def _operation_lock(self, plugin_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(plugin_id, asyncio.Lock())

    def _invalid_state(self, plugin_id: str, message: str) -> CoreError:
        return plugin_error(
            ErrorCategory.CONFLICT,
            "invalid_lifecycle_transition",
            message,
            plugin=plugin_id,
        )

    def _hook_failure(
        self,
        plugin_id: str,
        code: str,
        message: str,
        error: Exception,
    ) -> CoreError:
        return plugin_error(
            ErrorCategory.PROTOCOL_FAILURE,
            code,
            message,
            retryable=True,
            cause_id=type(error).__name__,
            plugin=plugin_id,
        )

    def _normalize_drain_error(self, plugin_id: str, error: CoreError) -> CoreError:
        if error.detail.category is not ErrorCategory.TIMEOUT:
            return error
        return plugin_error(
            ErrorCategory.TIMEOUT,
            "drain_timeout",
            "Plugin drain deadline expired before active leases reached zero",
            retryable=True,
            cause_id=error.detail.code,
            plugin=plugin_id,
        )


def _operation_id(
    context: RuntimeCallContext,
    action: ActionRef,
    plugin_id: str,
) -> str:
    if context.idempotency_key is None:
        raise plugin_error(
            ErrorCategory.INVALID_REQUEST,
            "missing_invocation_identity",
            "Plugin lifecycle operation requires an idempotency key",
            plugin=plugin_id,
        )
    return f"{action.name}:{plugin_id}:{context.idempotency_key.value}"


def _recovery_context(context: RuntimeCallContext) -> RuntimeCallContext:
    # Cleanup and failure reporting must survive the request that triggered them.
    # Preserve Scope, trace, Run, and idempotency identity, but remove transient
    # deadline/cancellation controls that have already fired.
    return replace(
        context,
        deadline=None,
        cancellation=CancellationToken(),
    )
