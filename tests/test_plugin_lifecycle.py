from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from congeries_core.plugin import (
    CapabilityRegistrationPlan,
    CapabilityRegistry,
    ExecutionLease,
    LoadedCapability,
    PluginHookName,
    PluginLifecycleController,
    PluginLifecycleState,
)
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import IdempotencyKey, RunId

from .plugin_support import manifest
from .support import NOW, FixedClock, call_context


async def active_controller() -> tuple[
    PluginLifecycleController,
    CapabilityRegistry,
    tuple[str, str, str],
]:
    value = manifest()
    registry = CapabilityRegistry()
    receipt = registry.commit(
        CapabilityRegistrationPlan(
            value.ref,
            (LoadedCapability(value.provides[0], object()),),
        ),
        expected_version=0,
    )
    controller = PluginLifecycleController(FixedClock())
    await controller.discover(value)
    await controller.transition(value.name, PluginLifecycleState.VALIDATED)
    await controller.transition(value.name, PluginLifecycleState.LOADED)
    await controller.transition(
        value.name,
        PluginLifecycleState.REGISTERED,
        receipt=receipt,
    )
    await controller.transition(value.name, PluginLifecycleState.ACTIVE)
    return controller, registry, value.provides[0].key


@pytest.mark.asyncio
async def test_lifecycle_transitions_hooks_and_discovery_are_idempotent() -> None:
    value = manifest()
    controller = PluginLifecycleController(FixedClock())

    discovered = await controller.discover(value)
    assert await controller.discover(value) is discovered
    validated = await controller.transition(value.name, PluginLifecycleState.VALIDATED)
    marked = await controller.mark_hook(value.name, PluginHookName.ON_LOAD)
    assert marked.state_version == validated.state_version + 1
    assert await controller.mark_hook(value.name, PluginHookName.ON_LOAD) is marked
    with pytest.raises(CoreError) as stale:
        await controller.transition(
            value.name,
            PluginLifecycleState.LOADED,
            expected_version=validated.state_version,
        )
    assert stale.value.detail.code == "lifecycle_state_conflict"

    with pytest.raises(CoreError) as conflict:
        await controller.discover(manifest(version="1.2.4"))
    assert conflict.value.detail.code == "registration_identity_conflict"
    with pytest.raises(CoreError) as invalid:
        await controller.transition(value.name, PluginLifecycleState.ACTIVE)
    assert invalid.value.detail.code == "invalid_lifecycle_transition"
    with pytest.raises(CoreError) as missing:
        await controller.get("test.missing")
    assert missing.value.detail.code == "plugin_not_loaded"


@pytest.mark.asyncio
async def test_execution_lease_is_active_only_and_idempotent() -> None:
    controller, _, capability_key = await active_controller()
    context = call_context()

    lease = await controller.acquire("test.echo", capability_key, context)
    assert isinstance(lease, ExecutionLease)
    assert await controller.acquire("test.echo", capability_key, context) is lease
    assert await controller.active_lease_count("test.echo") == 1

    mismatched = replace(context, run_id=RunId("run-2"))
    with pytest.raises(CoreError) as identity:
        await controller.acquire("test.echo", capability_key, mismatched)
    assert identity.value.detail.code == "lease_identity_conflict"

    await controller.release(lease)
    await controller.release(lease)
    assert await controller.active_lease_count("test.echo") == 0
    with pytest.raises(CoreError) as replay:
        await controller.acquire("test.echo", capability_key, context)
    assert replay.value.detail.code == "lease_identity_conflict"


@pytest.mark.asyncio
async def test_execution_lease_requires_identity_and_rejects_draining() -> None:
    controller, _, capability_key = await active_controller()
    context = replace(call_context(), idempotency_key=None)
    with pytest.raises(CoreError) as missing:
        await controller.acquire("test.echo", capability_key, context)
    assert missing.value.detail.code == "missing_invocation_identity"

    await controller.begin_draining("test.echo")
    with pytest.raises(CoreError) as draining:
        await controller.acquire(
            "test.echo",
            capability_key,
            replace(call_context(), idempotency_key=IdempotencyKey("new-call")),
        )
    assert draining.value.detail.code == "plugin_draining"
    assert draining.value.detail.retryable


@pytest.mark.asyncio
async def test_drain_waits_for_release_and_then_unregistration_is_legal() -> None:
    controller, registry, capability_key = await active_controller()
    record = await controller.get("test.echo")
    receipt = record.registration_receipt
    assert receipt is not None
    lease = await controller.acquire("test.echo", capability_key, call_context())
    await controller.begin_draining("test.echo")

    waiting = asyncio.create_task(controller.wait_for_zero("test.echo", call_context()))
    await asyncio.sleep(0)
    assert not waiting.done()
    await controller.release(lease)
    await waiting
    registry.unregister(receipt)
    unregistered = await controller.transition(
        "test.echo",
        PluginLifecycleState.UNREGISTERED,
        clear_receipt=True,
    )
    unloaded = await controller.transition(
        "test.echo",
        PluginLifecycleState.UNLOADED,
    )
    assert unregistered.registration_receipt is None
    assert unloaded.state is PluginLifecycleState.UNLOADED


@pytest.mark.asyncio
async def test_drain_wait_honors_cancellation_and_deadline_without_releasing() -> None:
    controller, _, capability_key = await active_controller()
    lease = await controller.acquire("test.echo", capability_key, call_context())
    await controller.begin_draining("test.echo")

    cancelled = call_context()
    waiting = asyncio.create_task(controller.wait_for_zero("test.echo", cancelled))
    await asyncio.sleep(0)
    cancelled.cancellation.cancel()
    with pytest.raises(CoreError) as cancellation:
        await waiting
    assert cancellation.value.detail.code == "call_cancelled"

    deadline_context = replace(
        call_context(),
        deadline=Deadline(NOW + timedelta(milliseconds=1)),
    )
    with pytest.raises(CoreError) as timeout:
        await controller.wait_for_zero("test.echo", deadline_context)
    assert timeout.value.detail.code == "deadline_exceeded"
    assert await controller.active_lease_count("test.echo") == 1
    await controller.release(lease)


@pytest.mark.asyncio
async def test_acquire_and_drain_have_only_linearized_outcomes() -> None:
    controller, _, capability_key = await active_controller()

    lease = await controller.acquire("test.echo", capability_key, call_context())
    draining = await controller.begin_draining("test.echo")
    assert draining.state is PluginLifecycleState.DRAINING
    assert await controller.active_lease_count("test.echo") == 1
    await controller.release(lease)

    reactivated = await controller.transition(
        "test.echo",
        PluginLifecycleState.ACTIVE,
    )
    assert reactivated.activation_epoch == 2
    await controller.begin_draining("test.echo")
    with pytest.raises(CoreError) as rejected:
        await controller.acquire(
            "test.echo",
            capability_key,
            replace(call_context(), idempotency_key=IdempotencyKey("after-drain")),
        )
    assert rejected.value.detail.code == "plugin_draining"


@pytest.mark.asyncio
async def test_release_rejects_unknown_or_mismatched_lease() -> None:
    controller, _, capability_key = await active_controller()
    lease = await controller.acquire("test.echo", capability_key, call_context())
    wrong = replace(lease, lease_id="wrong")
    with pytest.raises(CoreError) as conflict:
        await controller.release(wrong)
    assert conflict.value.detail.code == "lease_identity_conflict"
    await controller.release(lease)


@pytest.mark.asyncio
async def test_active_plugin_refs_include_registered_active_and_draining() -> None:
    controller, _, _ = await active_controller()
    assert [item.name for item in await controller.active_plugin_refs()] == [
        "test.echo"
    ]
    await controller.begin_draining("test.echo")
    assert [item.name for item in await controller.active_plugin_refs()] == [
        "test.echo"
    ]
