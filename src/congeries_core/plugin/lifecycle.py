"""Linearizable Plugin lifecycle state and execution leases."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import ErrorCategory
from congeries_core.runtime.ids import RunId

from .errors import plugin_error
from .model import PluginHookName, PluginLifecycleState, PluginManifest, PluginRef
from .registry import CapabilityKey, RegistrationReceipt


@dataclass(frozen=True, slots=True)
class PluginStateRecord:
    manifest: PluginManifest
    state: PluginLifecycleState
    state_version: int
    registration_receipt: RegistrationReceipt | None = None
    activation_epoch: int = 0
    successful_hooks: frozenset[PluginHookName] = frozenset()

    def __post_init__(self) -> None:
        if self.state_version < 0 or self.activation_epoch < 0:
            raise ValueError("Plugin state versions must be non-negative")

    @property
    def ref(self) -> PluginRef:
        return self.manifest.ref


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    lease_id: str
    plugin: PluginRef
    activation_epoch: int
    capability_key: CapabilityKey
    run_id: RunId
    invocation_identity: str
    acquired_at: datetime


@dataclass(frozen=True, slots=True)
class _InvocationReservation:
    plugin_id: str
    capability_key: CapabilityKey
    run_id: RunId
    invocation_identity: str


_ALLOWED_TRANSITIONS: dict[PluginLifecycleState, frozenset[PluginLifecycleState]] = {
    PluginLifecycleState.DISCOVERED: frozenset({PluginLifecycleState.VALIDATED}),
    PluginLifecycleState.VALIDATED: frozenset({PluginLifecycleState.LOADED}),
    PluginLifecycleState.LOADED: frozenset({PluginLifecycleState.REGISTERED}),
    PluginLifecycleState.REGISTERED: frozenset(
        {PluginLifecycleState.ACTIVE, PluginLifecycleState.UNREGISTERED}
    ),
    PluginLifecycleState.ACTIVE: frozenset({PluginLifecycleState.DRAINING}),
    PluginLifecycleState.DRAINING: frozenset(
        {PluginLifecycleState.ACTIVE, PluginLifecycleState.UNREGISTERED}
    ),
    PluginLifecycleState.UNREGISTERED: frozenset({PluginLifecycleState.UNLOADED}),
    PluginLifecycleState.UNLOADED: frozenset(),
}


class PluginLifecycleController:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[str, PluginStateRecord] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._leases: dict[str, dict[str, ExecutionLease]] = {}
        self._released: dict[str, set[str]] = {}
        self._invocations: dict[str, dict[str, _InvocationReservation]] = {}

    async def discover(self, manifest: PluginManifest) -> PluginStateRecord:
        condition = self._conditions.setdefault(manifest.name, asyncio.Condition())
        async with condition:
            existing = self._records.get(manifest.name)
            if existing is not None:
                if existing.manifest == manifest:
                    return existing
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "registration_identity_conflict",
                    "Plugin identity is already associated with another manifest",
                    plugin=manifest.name,
                )
            record = PluginStateRecord(
                manifest,
                PluginLifecycleState.DISCOVERED,
                0,
            )
            self._records[manifest.name] = record
            self._leases[manifest.name] = {}
            self._released[manifest.name] = set()
            self._invocations[manifest.name] = {}
            return record

    async def get(self, plugin_id: str) -> PluginStateRecord:
        condition = self._condition(plugin_id)
        async with condition:
            return self._record(plugin_id)

    async def transition(
        self,
        plugin_id: str,
        target: PluginLifecycleState,
        *,
        expected_version: int | None = None,
        receipt: RegistrationReceipt | None = None,
        clear_receipt: bool = False,
    ) -> PluginStateRecord:
        condition = self._condition(plugin_id)
        async with condition:
            # The per-Plugin condition is the FSM's linearization boundary.
            # expected_version rejects stale coordinators, while activation_epoch
            # separates leases created by different ACTIVE periods.
            current = self._record(plugin_id)
            if (
                expected_version is not None
                and current.state_version != expected_version
            ):
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "lifecycle_state_conflict",
                    "Plugin lifecycle state version is stale",
                    plugin=plugin_id,
                )
            if current.state is target:
                return current
            if target not in _ALLOWED_TRANSITIONS[current.state]:
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "invalid_lifecycle_transition",
                    f"Cannot transition Plugin from {current.state} to {target}",
                    plugin=plugin_id,
                )
            next_receipt = current.registration_receipt
            if receipt is not None:
                next_receipt = receipt
            if clear_receipt:
                next_receipt = None
            activation_epoch = current.activation_epoch
            if target is PluginLifecycleState.ACTIVE:
                activation_epoch += 1
            updated = replace(
                current,
                state=target,
                state_version=current.state_version + 1,
                registration_receipt=next_receipt,
                activation_epoch=activation_epoch,
            )
            self._records[plugin_id] = updated
            condition.notify_all()
            return updated

    async def mark_hook(
        self, plugin_id: str, hook: PluginHookName
    ) -> PluginStateRecord:
        condition = self._condition(plugin_id)
        async with condition:
            current = self._record(plugin_id)
            if hook in current.successful_hooks:
                return current
            updated = replace(
                current,
                state_version=current.state_version + 1,
                successful_hooks=current.successful_hooks | {hook},
            )
            self._records[plugin_id] = updated
            return updated

    async def acquire(
        self,
        plugin_id: str,
        capability_key: CapabilityKey,
        context: RuntimeCallContext,
    ) -> ExecutionLease:
        invocation = context.idempotency_key
        if invocation is None:
            raise plugin_error(
                ErrorCategory.INVALID_REQUEST,
                "missing_invocation_identity",
                "Plugin capability invocation requires an idempotency key",
                plugin=plugin_id,
                capability=capability_key[1],
            )
        condition = self._condition(plugin_id)
        async with condition:
            current = self._record(plugin_id)
            # An in-flight retry receives the original lease only when Run and
            # capability also match. Released IDs are retained to prevent an ABA
            # replay from recreating a lease after its invocation already ended.
            active = self._leases[plugin_id].get(invocation.value)
            if active is not None:
                if (
                    active.capability_key == capability_key
                    and active.run_id == context.run_id
                ):
                    return active
                raise self._lease_conflict(plugin_id, capability_key[1])
            if current.state is PluginLifecycleState.DRAINING:
                raise plugin_error(
                    ErrorCategory.UNAVAILABLE,
                    "plugin_draining",
                    "Plugin is draining and rejects new execution leases",
                    retryable=True,
                    plugin=plugin_id,
                    capability=capability_key[1],
                )
            if current.state is not PluginLifecycleState.ACTIVE:
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "invalid_lifecycle_transition",
                    "Execution leases are available only while Plugin is ACTIVE",
                    plugin=plugin_id,
                    capability=capability_key[1],
                )
            lease_id = _lease_id(
                current.ref,
                current.activation_epoch,
                capability_key,
                context.run_id,
                invocation.value,
            )
            if lease_id in self._released[plugin_id]:
                raise self._lease_conflict(plugin_id, capability_key[1])
            lease = ExecutionLease(
                lease_id=lease_id,
                plugin=current.ref,
                activation_epoch=current.activation_epoch,
                capability_key=capability_key,
                run_id=context.run_id,
                invocation_identity=invocation.value,
                acquired_at=self._clock.now(),
            )
            self._leases[plugin_id][invocation.value] = lease
            return lease

    async def reserve_invocation(
        self,
        plugin_id: str,
        capability_key: CapabilityKey,
        context: RuntimeCallContext,
    ) -> _InvocationReservation:
        # A reservation and an execution lease solve different races. Reserve the
        # idempotency identity before authorization so concurrent duplicates
        # cannot both reach a side effect; acquire the lease later to protect the
        # implementation itself from drain/unload.
        invocation = context.idempotency_key
        if invocation is None:
            raise plugin_error(
                ErrorCategory.INVALID_REQUEST,
                "missing_invocation_identity",
                "Plugin capability invocation requires an idempotency key",
                plugin=plugin_id,
                capability=capability_key[1],
            )
        condition = self._condition(plugin_id)
        async with condition:
            if invocation.value in self._invocations[plugin_id]:
                raise self._lease_conflict(plugin_id, capability_key[1])
            reservation = _InvocationReservation(
                plugin_id,
                capability_key,
                context.run_id,
                invocation.value,
            )
            self._invocations[plugin_id][invocation.value] = reservation
            return reservation

    async def release_invocation(self, reservation: _InvocationReservation) -> None:
        condition = self._condition(reservation.plugin_id)
        async with condition:
            existing = self._invocations[reservation.plugin_id].get(
                reservation.invocation_identity
            )
            if existing != reservation:
                raise self._lease_conflict(
                    reservation.plugin_id, reservation.capability_key[1]
                )
            del self._invocations[reservation.plugin_id][
                reservation.invocation_identity
            ]

    async def release(self, lease: ExecutionLease) -> None:
        plugin_id = lease.plugin.name
        condition = self._condition(plugin_id)
        async with condition:
            # Release is idempotent by lease ID, but only the exact recorded lease
            # may remove the invocation entry. notify_all wakes drain waiters only
            # after the active set has been updated.
            if lease.lease_id in self._released[plugin_id]:
                return
            existing = self._leases[plugin_id].get(lease.invocation_identity)
            if existing != lease:
                raise self._lease_conflict(plugin_id, lease.capability_key[1])
            del self._leases[plugin_id][lease.invocation_identity]
            self._released[plugin_id].add(lease.lease_id)
            condition.notify_all()

    async def begin_draining(
        self, plugin_id: str, *, expected_version: int | None = None
    ) -> PluginStateRecord:
        return await self.transition(
            plugin_id,
            PluginLifecycleState.DRAINING,
            expected_version=expected_version,
        )

    async def wait_for_zero(self, plugin_id: str, context: RuntimeCallContext) -> None:
        condition = self._condition(plugin_id)
        async with condition:
            # Recheck the predicate under the same condition used by release so a
            # wakeup cannot be lost. await_provider adds deadline/cancellation
            # control without weakening the lease-count invariant.
            while self._leases[plugin_id]:
                await await_provider(condition.wait(), context, self._clock)

    async def active_lease_count(self, plugin_id: str) -> int:
        condition = self._condition(plugin_id)
        async with condition:
            return len(self._leases[plugin_id])

    async def active_plugin_refs(self) -> tuple[PluginRef, ...]:
        refs: list[PluginRef] = []
        for plugin_id in sorted(self._records):
            record = await self.get(plugin_id)
            if record.state in {
                PluginLifecycleState.REGISTERED,
                PluginLifecycleState.ACTIVE,
                PluginLifecycleState.DRAINING,
            }:
                refs.append(record.ref)
        return tuple(refs)

    def _record(self, plugin_id: str) -> PluginStateRecord:
        record = self._records.get(plugin_id)
        if record is None:
            raise plugin_error(
                ErrorCategory.UNAVAILABLE,
                "plugin_not_loaded",
                "Plugin is not known to the lifecycle controller",
                retryable=True,
                plugin=plugin_id,
            )
        return record

    def _condition(self, plugin_id: str) -> asyncio.Condition:
        condition = self._conditions.get(plugin_id)
        if condition is None:
            self._record(plugin_id)
            raise AssertionError("known Plugin must have a lifecycle condition")
        return condition

    def _lease_conflict(self, plugin_id: str, capability_id: str) -> Exception:
        return plugin_error(
            ErrorCategory.CONFLICT,
            "lease_identity_conflict",
            "Execution lease identity conflicts with existing state",
            plugin=plugin_id,
            capability=capability_id,
        )


def _lease_id(
    plugin: PluginRef,
    activation_epoch: int,
    capability_key: CapabilityKey,
    run_id: RunId,
    invocation: str,
) -> str:
    encoded = "\x1f".join(
        (
            plugin.name,
            str(plugin.version),
            str(activation_epoch),
            *capability_key,
            run_id.value,
            invocation,
        )
    ).encode()
    return "plugin-lease:" + hashlib.sha256(encoded).hexdigest()
