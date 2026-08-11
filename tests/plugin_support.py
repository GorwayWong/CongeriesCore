"""Deterministic Plugin SDK test collaborators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from congeries_core.plugin import (
    AllowRepresentablePermissions,
    CapabilityDeclaration,
    CapabilityRegistry,
    DependencyResolver,
    LoadedCapability,
    ManifestValidator,
    PluginHookName,
    PluginHooks,
    PluginLifecycleController,
    PluginLifecycleState,
    PluginManager,
    PluginManifest,
    PluginPreflight,
    PluginRef,
    PreparedPlugin,
    SemVer,
    plugin_actions,
)
from congeries_core.policy.authorization import (
    ActionRef,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorCategory, ErrorDetail, core_error
from congeries_core.runtime.ids import PrincipalId

from .provider_support import AuditRecorder, FailureRecorder, RecordingPolicy
from .support import FixedClock

CAPABILITY_ACTION = ActionRef("test", "echo.invoke", "1")


def manifest_data(
    *,
    name: str = "test.echo",
    version: str = "1.2.3",
    requires: list[object] | None = None,
    lifecycle: list[str] | None = None,
) -> dict[str, object]:
    permission = {
        "action": CAPABILITY_ACTION.to_data(),
        "scope_pattern": "core:workspace:*",
    }
    return {
        "contract_version": "1",
        "name": name,
        "version": version,
        "core_api": ">=0.2.0,<0.3.0",
        "entrypoint": f"{name}:plugin",
        "provides": [
            {
                "type": "tool",
                "capability_id": f"{name}.capability",
                "contract_version": "1.0.0",
                "entry": "echo",
                "permissions": [permission],
            }
        ],
        "requires": requires or [],
        "permissions": [permission],
        "lifecycle": lifecycle or [],
    }


def manifest(**kwargs: object) -> PluginManifest:
    return ManifestValidator().validate(manifest_data(**kwargs))


@dataclass(slots=True)
class FakePluginLoader:
    fail_prepare: bool = False
    fail_hooks_once: set[PluginHookName] = field(default_factory=set)
    fail_cleanup_once: bool = False
    prepare_calls: int = 0
    cleanup_calls: int = 0
    hook_calls: list[PluginHookName] = field(default_factory=list)

    async def prepare(
        self, value: PluginManifest, context: RuntimeCallContext
    ) -> PreparedPlugin:
        del context
        self.prepare_calls += 1
        if self.fail_prepare:
            raise RuntimeError("prepare failed")
        hooks: dict[str, object] = {}
        for hook_name in PluginHookName:
            if hook_name in value.lifecycle:
                hooks[hook_name.value] = self._hook(hook_name)
        return PreparedPlugin(
            value,
            tuple(
                LoadedCapability(item, {"entry": item.entry}) for item in value.provides
            ),
            PluginHooks(**hooks),
        )

    def _hook(self, hook_name: PluginHookName):
        async def invoke(context: RuntimeCallContext) -> None:
            del context
            self.hook_calls.append(hook_name)
            if hook_name in self.fail_hooks_once:
                self.fail_hooks_once.remove(hook_name)
                raise RuntimeError(f"{hook_name.value} failed")

        return invoke

    async def cleanup(
        self, prepared: PreparedPlugin, context: RuntimeCallContext
    ) -> None:
        del prepared, context
        self.cleanup_calls += 1
        if self.fail_cleanup_once:
            self.fail_cleanup_once = False
            raise RuntimeError("cleanup failed")


class AlternateFakePluginLoader(FakePluginLoader):
    pass


@dataclass(slots=True)
class PluginEventRecorder:
    requested: list[tuple[PluginRef, str, PluginLifecycleState, str, int]] = field(
        default_factory=list
    )
    changed: list[tuple[PluginRef, str, PluginLifecycleState, str, int]] = field(
        default_factory=list
    )
    failed: list[tuple[PluginRef, str, PluginLifecycleState, str, int, ErrorDetail]] = (
        field(default_factory=list)
    )
    fail_requested: bool = False
    fail_failed: bool = False

    async def transition_requested(
        self,
        plugin: PluginRef,
        from_state: str,
        to_state: PluginLifecycleState,
        operation_id: str,
        active_lease_count: int,
        context: RuntimeCallContext,
    ) -> None:
        del context
        if self.fail_requested:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "audit_failed",
                "required audit failed",
            )
        self.requested.append(
            (plugin, from_state, to_state, operation_id, active_lease_count)
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
        del context
        self.changed.append(
            (plugin, from_state, to_state, operation_id, active_lease_count)
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
        del context
        if self.fail_failed:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "audit_failed",
                "required audit failed",
            )
        self.failed.append(
            (
                plugin,
                from_state,
                to_state,
                operation_id,
                active_lease_count,
                error,
            )
        )


@dataclass(slots=True)
class PluginRuntimeFixture:
    manager: PluginManager
    registry: CapabilityRegistry
    lifecycle: PluginLifecycleController
    policy: RecordingPolicy
    events: PluginEventRecorder
    principal: RuntimePrincipal


def plugin_runtime(
    *,
    policy: RecordingPolicy | None = None,
    events: PluginEventRecorder | None = None,
) -> PluginRuntimeFixture:
    actual_policy = policy or RecordingPolicy()
    actual_events = events or PluginEventRecorder()
    registry = CapabilityRegistry()
    clock = FixedClock()
    lifecycle = PluginLifecycleController(clock)
    dispatcher: AuthorizedDispatcher[object] = AuthorizedDispatcher(
        action_registry=ActionRegistry((*plugin_actions(), CAPABILITY_ACTION)),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=clock,
        policy=actual_policy,
    )
    manager = PluginManager(
        validator=ManifestValidator(),
        preflight=PluginPreflight(
            core_api_version=SemVer.parse("0.2.0"),
            permissions=AllowRepresentablePermissions(),
        ),
        resolver=DependencyResolver(),
        registry=registry,
        lifecycle=lifecycle,
        dispatcher=dispatcher,
        clock=clock,
        events=actual_events,
    )
    return PluginRuntimeFixture(
        manager,
        registry,
        lifecycle,
        actual_policy,
        actual_events,
        RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1")),
    )


class RejectPermissions:
    def can_represent(self, permission: object) -> bool:
        del permission
        return False


def capability_declaration(
    value: PluginManifest | None = None,
) -> CapabilityDeclaration:
    return (value or manifest()).provides[0]


def plugin_dependency(
    plugin_id: str, version_range: str = ">=1.0.0,<2.0.0"
) -> Mapping[str, object]:
    return {
        "kind": "plugin",
        "plugin_id": plugin_id,
        "version_range": version_range,
    }


def capability_dependency(
    capability_id: str,
    version_range: str = ">=1.0.0,<2.0.0",
) -> Mapping[str, object]:
    return {
        "kind": "capability",
        "capability_type": "tool",
        "capability_id": capability_id,
        "version_range": version_range,
    }
