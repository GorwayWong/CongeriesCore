from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from congeries_core.plugin import (
    CapabilityRegistrationPlan,
    LoadedCapability,
    PluginHookName,
    PluginHooks,
    PluginLifecycleState,
    PreparedPlugin,
)
from congeries_core.policy.authorization import ActionRef
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError
from congeries_core.runtime.ids import IdempotencyKey
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef

from ..plugin_support import (
    CAPABILITY_ACTION,
    AlternateFakePluginLoader,
    FakePluginLoader,
    PluginEventRecorder,
    manifest,
    manifest_data,
    plugin_runtime,
)
from ..provider_support import RecordingPolicy
from ..support import NOW, call_context


async def load_active(loader: FakePluginLoader):
    runtime = plugin_runtime()
    data = manifest_data(lifecycle=["on_load", "on_activate", "on_drain", "on_unload"])
    loaded = await runtime.manager.load(
        data,
        loader,
        call_context(),
        runtime.principal,
    )
    active = await runtime.manager.activate(
        loaded.ref.name,
        call_context(),
        runtime.principal,
    )
    return runtime, data, active


@pytest.mark.asyncio
@pytest.mark.parametrize("loader_type", [FakePluginLoader, AlternateFakePluginLoader])
async def test_fake_loaders_share_complete_load_invoke_unload_contract(
    loader_type,
) -> None:
    loader = loader_type()
    runtime, _, active = await load_active(loader)
    capability = active.manifest.provides[0]

    async def invoke(implementation: object, context: object) -> str:
        del context
        assert implementation == {"entry": "echo"}
        return "ok"

    result = await runtime.manager.invoke(
        active.ref.name,
        capability.key,
        CAPABILITY_ACTION,
        call_context(),
        runtime.principal,
        invoke,
    )
    unloaded = await runtime.manager.unload(
        active.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("unload-1")),
        runtime.principal,
    )
    repeated = await runtime.manager.unload(
        active.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("unload-1")),
        runtime.principal,
    )

    assert result == "ok"
    assert unloaded.state is PluginLifecycleState.UNLOADED
    assert repeated is unloaded
    assert not runtime.registry.snapshot().registrations
    assert loader.cleanup_calls == 1
    assert loader.hook_calls == [
        PluginHookName.ON_LOAD,
        PluginHookName.ON_ACTIVATE,
        PluginHookName.ON_DRAIN,
        PluginHookName.ON_UNLOAD,
    ]
    assert await runtime.lifecycle.active_lease_count(active.ref.name) == 0


@pytest.mark.asyncio
async def test_authorization_and_required_request_audit_fail_before_effects() -> None:
    denied_policy = RecordingPolicy(denied_actions={"plugin.load"})
    denied = plugin_runtime(policy=denied_policy)
    loader = FakePluginLoader()

    with pytest.raises(CoreError) as denial:
        await denied.manager.load(
            manifest_data(), loader, call_context(), denied.principal
        )
    assert denial.value.detail.code == "test_denied"
    assert loader.prepare_calls == 0
    with pytest.raises(CoreError):
        await denied.lifecycle.get("test.echo")

    events = PluginEventRecorder(fail_requested=True)
    audited = plugin_runtime(events=events)
    with pytest.raises(CoreError) as audit_failure:
        await audited.manager.load(
            manifest_data(), loader, call_context(), audited.principal
        )
    assert audit_failure.value.detail.code == "audit_failed"
    assert loader.prepare_calls == 0
    with pytest.raises(CoreError):
        await audited.lifecycle.get("test.echo")


@pytest.mark.asyncio
async def test_invalid_manifest_dependency_and_collision_have_no_loader_effects() -> (
    None
):
    runtime = plugin_runtime()
    loader = FakePluginLoader()
    invalid = manifest_data()
    invalid["unknown"] = True
    with pytest.raises(CoreError) as manifest_error:
        await runtime.manager.load(invalid, loader, call_context(), runtime.principal)
    assert manifest_error.value.detail.code == "invalid_manifest"
    assert runtime.policy.requests == []
    assert runtime.events.requested == []
    assert runtime.events.changed == []
    assert runtime.registry.snapshot().version == 0

    missing = manifest_data(
        requires=[
            {
                "kind": "plugin",
                "plugin_id": "test.missing",
                "version_range": ">=1.0.0,<2.0.0",
            }
        ]
    )
    with pytest.raises(CoreError) as dependency:
        await runtime.manager.load(missing, loader, call_context(), runtime.principal)
    assert dependency.value.detail.code == "dependency_unavailable"

    other = manifest(name="test.other")
    declaration = manifest().provides[0]
    runtime.registry.commit(
        CapabilityRegistrationPlan(
            other.ref,
            (LoadedCapability(declaration, object()),),
        ),
        expected_version=0,
    )
    with pytest.raises(CoreError) as collision:
        await runtime.manager.load(
            manifest_data(), loader, call_context(), runtime.principal
        )
    assert collision.value.detail.code == "registration_conflict"
    assert loader.prepare_calls == 0


@pytest.mark.asyncio
async def test_load_and_activation_failures_leave_retryable_stable_states() -> None:
    runtime = plugin_runtime()
    prepare_failure = FakePluginLoader(fail_prepare=True)
    data = manifest_data(lifecycle=["on_load", "on_activate"])
    with pytest.raises(CoreError) as failed_load:
        await runtime.manager.load(
            data, prepare_failure, call_context(), runtime.principal
        )
    assert failed_load.value.detail.code == "plugin_load_failed"
    assert (
        await runtime.lifecycle.get("test.echo")
    ).state is PluginLifecycleState.VALIDATED

    hook_runtime = plugin_runtime()
    hook_failure = FakePluginLoader(fail_hooks_once={PluginHookName.ON_LOAD})
    with pytest.raises(CoreError):
        await hook_runtime.manager.load(
            data, hook_failure, call_context(), hook_runtime.principal
        )
    assert hook_failure.cleanup_calls == 1
    assert (
        await hook_runtime.lifecycle.get("test.echo")
    ).state is PluginLifecycleState.VALIDATED

    retry_runtime = plugin_runtime()
    activation_failure = FakePluginLoader(fail_hooks_once={PluginHookName.ON_ACTIVATE})
    loaded = await retry_runtime.manager.load(
        data, activation_failure, call_context(), retry_runtime.principal
    )
    with pytest.raises(CoreError) as failed_activation:
        await retry_runtime.manager.activate(
            loaded.ref.name, call_context(), retry_runtime.principal
        )
    assert failed_activation.value.detail.code == "lifecycle_hook_failed"
    registered = await retry_runtime.lifecycle.get(loaded.ref.name)
    assert registered.state is PluginLifecycleState.REGISTERED
    assert retry_runtime.registry.snapshot().registrations

    active = await retry_runtime.manager.activate(
        loaded.ref.name, call_context(), retry_runtime.principal
    )
    assert active.state is PluginLifecycleState.ACTIVE
    assert activation_failure.hook_calls.count(PluginHookName.ON_ACTIVATE) == 2


@pytest.mark.asyncio
async def test_unload_drains_existing_work_and_rejects_new_invocations() -> None:
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)
    capability = active.manifest.provides[0]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def blocking(implementation: object, context: object) -> str:
        del implementation, context
        started.set()
        await finish.wait()
        return "completed"

    invocation = asyncio.create_task(
        runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            replace(call_context(), idempotency_key=IdempotencyKey("invoke-1")),
            runtime.principal,
            blocking,
        )
    )
    await started.wait()
    unload = asyncio.create_task(
        runtime.manager.unload(
            active.ref.name,
            replace(call_context(), idempotency_key=IdempotencyKey("unload-race")),
            runtime.principal,
        )
    )
    while (
        await runtime.lifecycle.get(active.ref.name)
    ).state is not PluginLifecycleState.DRAINING:
        await asyncio.sleep(0)

    with pytest.raises(CoreError) as rejected:
        await runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            replace(call_context(), idempotency_key=IdempotencyKey("invoke-2")),
            runtime.principal,
            blocking,
        )
    assert rejected.value.detail.code == "plugin_draining"
    assert not unload.done()

    finish.set()
    assert await invocation == "completed"
    assert (await unload).state is PluginLifecycleState.UNLOADED


@pytest.mark.asyncio
async def test_drain_timeout_cancel_and_retry_preserve_active_resources() -> None:
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)
    capability = active.manifest.provides[0]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def blocking(implementation: object, context: object) -> None:
        del implementation, context
        started.set()
        await finish.wait()

    invocation = asyncio.create_task(
        runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            replace(call_context(), idempotency_key=IdempotencyKey("timeout-call")),
            runtime.principal,
            blocking,
        )
    )
    await started.wait()
    deadline = replace(
        call_context(),
        idempotency_key=IdempotencyKey("timeout-unload"),
        deadline=Deadline(NOW + timedelta(milliseconds=1)),
    )
    with pytest.raises(CoreError) as timeout:
        await runtime.manager.unload(active.ref.name, deadline, runtime.principal)
    assert timeout.value.detail.code == "drain_timeout"
    assert (
        await runtime.lifecycle.get(active.ref.name)
    ).state is PluginLifecycleState.DRAINING
    assert runtime.registry.snapshot().registrations
    assert loader.cleanup_calls == 0

    reactivated = await runtime.manager.cancel_drain(
        active.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("cancel-drain")),
        runtime.principal,
    )
    assert reactivated.state is PluginLifecycleState.ACTIVE
    finish.set()
    await invocation

    unloaded = await runtime.manager.unload(
        active.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("retry-unload")),
        runtime.principal,
    )
    assert unloaded.state is PluginLifecycleState.UNLOADED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hook", "cleanup"])
async def test_unload_failure_stays_unregistered_and_retry_resumes(
    failure: str,
) -> None:
    loader = FakePluginLoader(
        fail_hooks_once={PluginHookName.ON_UNLOAD} if failure == "hook" else set(),
        fail_cleanup_once=failure == "cleanup",
    )
    runtime, _, active = await load_active(loader)
    context = replace(call_context(), idempotency_key=IdempotencyKey("unload-retry"))

    with pytest.raises(CoreError) as failed:
        await runtime.manager.unload(active.ref.name, context, runtime.principal)
    assert failed.value.detail.code == "unload_failed"
    state = await runtime.lifecycle.get(active.ref.name)
    assert state.state is PluginLifecycleState.UNREGISTERED
    assert not runtime.registry.snapshot().registrations

    unloaded = await runtime.manager.unload(active.ref.name, context, runtime.principal)
    assert unloaded.state is PluginLifecycleState.UNLOADED
    assert loader.cleanup_calls == (1 if failure == "hook" else 2)
    expected_hook_calls = 2 if failure == "hook" else 1
    assert loader.hook_calls.count(PluginHookName.ON_UNLOAD) == expected_hook_calls


@pytest.mark.asyncio
async def test_invocation_failure_timeout_and_cancellation_always_release_lease() -> (
    None
):
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)
    capability = active.manifest.provides[0]

    async def failing(implementation: object, context: object) -> None:
        del implementation, context
        raise RuntimeError("capability failed")

    with pytest.raises(RuntimeError):
        await runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            replace(call_context(), idempotency_key=IdempotencyKey("fail-call")),
            runtime.principal,
            failing,
        )
    assert await runtime.lifecycle.active_lease_count(active.ref.name) == 0

    async def blocking(implementation: object, context: object) -> None:
        del implementation, context
        await asyncio.Event().wait()

    timed = replace(
        call_context(),
        idempotency_key=IdempotencyKey("timed-call"),
        deadline=Deadline(NOW + timedelta(milliseconds=1)),
    )
    with pytest.raises(CoreError) as timeout:
        await runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            timed,
            runtime.principal,
            blocking,
        )
    assert timeout.value.detail.code == "deadline_exceeded"
    assert await runtime.lifecycle.active_lease_count(active.ref.name) == 0

    cancelled = replace(
        call_context(), idempotency_key=IdempotencyKey("cancelled-call")
    )
    task = asyncio.create_task(
        runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            cancelled,
            runtime.principal,
            blocking,
        )
    )
    await asyncio.sleep(0)
    cancelled.cancellation.cancel()
    with pytest.raises(CoreError) as cancellation:
        await task
    assert cancellation.value.detail.code == "call_cancelled"
    assert await runtime.lifecycle.active_lease_count(active.ref.name) == 0


@pytest.mark.asyncio
async def test_repeated_load_is_idempotent_and_manifest_identity_is_stable() -> None:
    runtime = plugin_runtime()
    loader = FakePluginLoader()
    data = manifest_data()

    first = await runtime.manager.load(data, loader, call_context(), runtime.principal)
    repeated = await runtime.manager.load(
        data,
        AlternateFakePluginLoader(),
        call_context(),
        runtime.principal,
    )

    assert repeated is first
    assert loader.prepare_calls == 1
    changed = manifest_data(version="1.2.4")
    with pytest.raises(CoreError) as conflict:
        await runtime.manager.load(
            changed,
            AlternateFakePluginLoader(),
            call_context(),
            runtime.principal,
        )
    assert conflict.value.detail.code == "registration_identity_conflict"
    assert (await runtime.lifecycle.get("test.echo")).manifest is first.manifest


class ContractBreakingLoader(FakePluginLoader):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def prepare(self, value, context) -> PreparedPlugin:
        prepared = await super().prepare(value, context)
        if self.mode == "manifest":
            return replace(prepared, manifest=manifest(version="1.2.4"))
        if self.mode == "capabilities":
            return replace(prepared, capabilities=())

        async def undeclared_hook(hook_context) -> None:
            del hook_context

        return replace(prepared, hooks=PluginHooks(on_load=undeclared_hook))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manifest", "capabilities", "hooks"])
async def test_loader_contract_violation_cleans_staged_resources(mode: str) -> None:
    runtime = plugin_runtime()
    loader = ContractBreakingLoader(mode)

    with pytest.raises(CoreError) as failure:
        await runtime.manager.load(
            manifest_data(), loader, call_context(), runtime.principal
        )

    assert failure.value.detail.code == "plugin_load_failed"
    assert loader.cleanup_calls == 1
    assert (
        await runtime.lifecycle.get("test.echo")
    ).state is PluginLifecycleState.VALIDATED


@pytest.mark.asyncio
async def test_activation_registration_collision_rolls_back_to_loaded() -> None:
    runtime = plugin_runtime()
    loader = FakePluginLoader()
    loaded = await runtime.manager.load(
        manifest_data(), loader, call_context(), runtime.principal
    )
    other = manifest(name="test.other")
    runtime.registry.commit(
        CapabilityRegistrationPlan(
            other.ref,
            (LoadedCapability(loaded.manifest.provides[0], object()),),
        ),
        expected_version=0,
    )

    with pytest.raises(CoreError) as collision:
        await runtime.manager.activate(
            loaded.ref.name, call_context(), runtime.principal
        )

    assert collision.value.detail.code == "registration_conflict"
    assert (
        await runtime.lifecycle.get(loaded.ref.name)
    ).state is PluginLifecycleState.LOADED
    registration = runtime.registry.get(loaded.manifest.provides[0].key)
    assert registration.owner == other.ref


@pytest.mark.asyncio
async def test_explicit_drain_is_idempotent_and_cancel_drain_reactivates() -> None:
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)
    drain_context = replace(
        call_context(), idempotency_key=IdempotencyKey("explicit-drain")
    )

    draining = await runtime.manager.drain(
        active.ref.name, drain_context, runtime.principal
    )
    repeated = await runtime.manager.drain(
        active.ref.name, drain_context, runtime.principal
    )

    assert draining.state is PluginLifecycleState.DRAINING
    assert repeated.state is PluginLifecycleState.DRAINING
    assert loader.hook_calls.count(PluginHookName.ON_DRAIN) == 1
    active_again = await runtime.manager.cancel_drain(
        active.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("resume-drain")),
        runtime.principal,
    )
    assert active_again.state is PluginLifecycleState.ACTIVE
    with pytest.raises(CoreError) as invalid_cancel:
        await runtime.manager.cancel_drain(
            active.ref.name,
            replace(call_context(), idempotency_key=IdempotencyKey("resume-again")),
            runtime.principal,
        )
    assert invalid_cancel.value.detail.code == "invalid_lifecycle_transition"


@pytest.mark.asyncio
async def test_wrong_owner_and_missing_operation_identity_fail_closed() -> None:
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)
    capability = active.manifest.provides[0]

    async def never_called(implementation: object, context: object) -> None:
        del implementation, context
        pytest.fail("operation must not be called")

    with pytest.raises(CoreError) as owner:
        await runtime.manager.invoke(
            "test.other",
            capability.key,
            CAPABILITY_ACTION,
            call_context(),
            runtime.principal,
            never_called,
        )
    assert owner.value.detail.code == "registration_identity_conflict"

    missing_identity = replace(call_context(), idempotency_key=None)
    other_runtime = plugin_runtime()
    with pytest.raises(CoreError) as missing:
        await other_runtime.manager.load(
            manifest_data(),
            FakePluginLoader(),
            missing_identity,
            other_runtime.principal,
        )
    assert missing.value.detail.code == "missing_invocation_identity"
    with pytest.raises(CoreError):
        await other_runtime.lifecycle.get("test.echo")


@pytest.mark.asyncio
async def test_invalid_unload_state_and_failed_audit_preserve_safe_state() -> None:
    runtime = plugin_runtime()
    loaded = await runtime.manager.load(
        manifest_data(), FakePluginLoader(), call_context(), runtime.principal
    )
    with pytest.raises(CoreError) as invalid:
        await runtime.manager.unload(
            loaded.ref.name,
            replace(call_context(), idempotency_key=IdempotencyKey("unload-loaded")),
            runtime.principal,
        )
    assert invalid.value.detail.code == "invalid_lifecycle_transition"
    assert (
        await runtime.lifecycle.get(loaded.ref.name)
    ).state is PluginLifecycleState.LOADED

    events = PluginEventRecorder(fail_failed=True)
    audited = plugin_runtime(events=events)
    loader = FakePluginLoader(fail_hooks_once={PluginHookName.ON_ACTIVATE})
    loaded = await audited.manager.load(
        manifest_data(lifecycle=["on_activate"]),
        loader,
        call_context(),
        audited.principal,
    )
    with pytest.raises(CoreError) as audit_failure:
        await audited.manager.activate(
            loaded.ref.name, call_context(), audited.principal
        )
    assert audit_failure.value.detail.code == "audit_failed"
    assert (
        await audited.lifecycle.get(loaded.ref.name)
    ).state is PluginLifecycleState.REGISTERED


@pytest.mark.asyncio
async def test_registered_activation_failure_can_unload_without_drain() -> None:
    runtime = plugin_runtime()
    loader = FakePluginLoader(fail_hooks_once={PluginHookName.ON_ACTIVATE})
    loaded = await runtime.manager.load(
        manifest_data(lifecycle=["on_activate", "on_unload"]),
        loader,
        call_context(),
        runtime.principal,
    )
    with pytest.raises(CoreError):
        await runtime.manager.activate(
            loaded.ref.name, call_context(), runtime.principal
        )

    unloaded = await runtime.manager.unload(
        loaded.ref.name,
        replace(call_context(), idempotency_key=IdempotencyKey("registered-unload")),
        runtime.principal,
    )
    assert unloaded.state is PluginLifecycleState.UNLOADED
    assert loader.cleanup_calls == 1


@pytest.mark.asyncio
async def test_activation_evaluates_declared_permissions_before_hook() -> None:
    policy = RecordingPolicy(denied_actions={CAPABILITY_ACTION.name})
    runtime = plugin_runtime(policy=policy)
    loader = FakePluginLoader()
    loaded = await runtime.manager.load(
        manifest_data(lifecycle=["on_activate"]),
        loader,
        call_context(),
        runtime.principal,
    )

    with pytest.raises(CoreError) as denied:
        await runtime.manager.activate(
            loaded.ref.name, call_context(), runtime.principal
        )

    assert denied.value.detail.code == "permission_denied"
    assert denied.value.detail.cause_id == "test_denied"
    assert (
        await runtime.lifecycle.get(loaded.ref.name)
    ).state is PluginLifecycleState.REGISTERED
    assert PluginHookName.ON_ACTIVATE not in loader.hook_calls


@pytest.mark.asyncio
async def test_invocation_requires_declared_action_and_effective_scope() -> None:
    data = manifest_data()
    permission = {
        "action": CAPABILITY_ACTION.to_data(),
        "scope_pattern": "core:workspace:workspace-1",
    }
    data["permissions"] = [permission]
    provides = data["provides"]
    assert isinstance(provides, list)
    declaration = provides[0]
    assert isinstance(declaration, dict)
    declaration["permissions"] = [permission]
    runtime = plugin_runtime()
    loader = FakePluginLoader()
    loaded = await runtime.manager.load(
        data, loader, call_context(), runtime.principal
    )
    active = await runtime.manager.activate(
        loaded.ref.name, call_context(), runtime.principal
    )
    capability = active.manifest.provides[0]

    async def never_called(implementation: object, context: object) -> None:
        del implementation, context
        pytest.fail("operation must not be called")

    with pytest.raises(CoreError) as action_denied:
        await runtime.manager.invoke(
            active.ref.name,
            capability.key,
            ActionRef("test", "other.invoke", "1"),
            call_context(),
            runtime.principal,
            never_called,
        )
    assert action_denied.value.detail.code == "permission_denied"

    other_scope = ScopeRef.core(CoreScopeKind.WORKSPACE, "workspace-2")
    with pytest.raises(CoreError) as scope_denied:
        await runtime.manager.invoke(
            active.ref.name,
            capability.key,
            CAPABILITY_ACTION,
            call_context(scope=other_scope),
            runtime.principal,
            never_called,
        )
    assert scope_denied.value.detail.code == "permission_denied"
    assert await runtime.lifecycle.active_lease_count(active.ref.name) == 0


@pytest.mark.asyncio
async def test_concurrent_unload_serializes_cleanup_once() -> None:
    loader = FakePluginLoader()
    runtime, _, active = await load_active(loader)

    first, second = await asyncio.gather(
        runtime.manager.unload(
            active.ref.name,
            replace(call_context(), idempotency_key=IdempotencyKey("unload-a")),
            runtime.principal,
        ),
        runtime.manager.unload(
            active.ref.name,
            replace(call_context(), idempotency_key=IdempotencyKey("unload-b")),
            runtime.principal,
        ),
    )

    assert first.state is PluginLifecycleState.UNLOADED
    assert second.state is PluginLifecycleState.UNLOADED
    assert loader.cleanup_calls == 1
    assert loader.hook_calls.count(PluginHookName.ON_UNLOAD) == 1
