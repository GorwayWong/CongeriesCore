"""Authorized capability invocation with Plugin execution leases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import ErrorCategory
from congeries_core.runtime.json_types import JsonValue

from .errors import plugin_error
from .lifecycle import PluginLifecycleController
from .registry import CapabilityKey, CapabilityRegistration, CapabilityRegistry

type CapabilityOperation[ResultT] = Callable[
    [CapabilityRegistration, AuthorizedCall], Awaitable[ResultT]
]
type CapabilityResourceValidator = Callable[[CapabilityRegistration, ResourceRef], bool]


class PluginCapabilityInvoker:
    """The only boundary that hands Plugin implementation objects to callers."""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        lifecycle: PluginLifecycleController,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
    ) -> None:
        self._registry = registry
        self._lifecycle = lifecycle
        self._dispatcher = dispatcher
        self._clock = clock

    async def invoke[ResultT](
        self,
        *,
        plugin_id: str,
        capability_key: CapabilityKey,
        action: ActionRef,
        resource: ResourceRef,
        context: RuntimeCallContext,
        principal: RuntimePrincipal,
        operation: CapabilityOperation[ResultT],
        constraints: Mapping[str, JsonValue] | None = None,
        resource_validator: CapabilityResourceValidator | None = None,
    ) -> ResultT:
        registration = self._registry.get(capability_key)
        if registration.owner.name != plugin_id:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "registration_identity_conflict",
                "Capability is owned by another Plugin",
                plugin=plugin_id,
                capability=capability_key[1],
            )
        if resource.owning_extension != plugin_id:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "registration_identity_conflict",
                "Invocation resource does not identify the owning Plugin",
                plugin=plugin_id,
                capability=capability_key[1],
            )
        # Most capabilities authorize their own core ResourceRef. Skill resource
        # reads authorize a narrower child resource instead, so they supply a
        # validator that must bind that child back to this exact registration.
        if resource_validator is None:
            resource_matches = (
                resource.namespace == "core"
                and resource.kind == registration.declaration.type.value
                and resource.id.value == registration.declaration.capability_id
            )
        else:
            resource_matches = resource_validator(registration, resource)
        if not resource_matches:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "invocation_resource_mismatch",
                "Invocation resource does not match the registered capability",
                plugin=plugin_id,
                capability=capability_key[1],
            )
        permission = next(
            (
                item
                for item in registration.declaration.permissions
                if item.action.key == action.key
            ),
            None,
        )
        if permission is None:
            raise plugin_error(
                ErrorCategory.DENIED,
                "permission_denied",
                "Capability invocation action is not declared",
                plugin=plugin_id,
                capability=capability_key[1],
            )

        async def authorized(call: AuthorizedCall) -> object:
            if not permission.permits(call.context.scope):
                raise plugin_error(
                    ErrorCategory.DENIED,
                    "permission_denied",
                    "Capability invocation Scope is outside its declaration",
                    plugin=plugin_id,
                    capability=capability_key[1],
                )
            # Authorization and declared-Scope checks precede the lease. The
            # callback below is the sole place the opaque implementation is
            # usable, and finally keeps drain/unload safe on every exit path.
            lease = await self._lifecycle.acquire(
                plugin_id, capability_key, call.context
            )
            try:
                return await await_provider(
                    operation(registration, call), call.context, self._clock
                )
            finally:
                await self._lifecycle.release(lease)

        reservation = await self._lifecycle.reserve_invocation(
            plugin_id, capability_key, context
        )
        try:
            result = await await_provider(
                self._dispatcher.dispatch(
                    AccessRequest(
                        principal=principal,
                        action=action,
                        resource=resource,
                        scope=context.scope,
                        context=context,
                        constraints=constraints or {},
                    ),
                    authorized,
                ),
                context,
                self._clock,
            )
            return cast(ResultT, result)
        finally:
            await self._lifecycle.release_invocation(reservation)
