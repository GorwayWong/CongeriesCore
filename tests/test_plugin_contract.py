from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import permutations
from threading import Event, Thread

import pytest

from congeries_core.plugin import (
    AllowRepresentablePermissions,
    CapabilityCatalogSnapshot,
    CapabilityRegistrationPlan,
    CapabilityRegistry,
    CatalogCapability,
    DependencyResolver,
    LoadedCapability,
    ManifestValidator,
    PluginLifecycleState,
    PluginPreflight,
    RegistrationReceipt,
    SemVer,
    VersionRange,
)
from congeries_core.runtime.errors import CoreError, ErrorCategory

from .plugin_support import (
    RejectPermissions,
    capability_dependency,
    manifest,
    manifest_data,
    plugin_dependency,
)


def test_semver_and_version_range_follow_v1_precedence() -> None:
    alpha = SemVer.parse("1.2.3-alpha.1+build.7")
    release = SemVer.parse("1.2.3")

    assert str(alpha) == "1.2.3-alpha.1+build.7"
    assert alpha < release
    assert SemVer.parse("1.0.0+one") == SemVer.parse("1.0.0+two")
    assert VersionRange.parse(">=1.2.0,<2.0.0").matches(release)
    assert not VersionRange.parse("1.2.4").matches(release)


@pytest.mark.parametrize(
    "value",
    ["1", "01.2.3", "1.2.3-01", "v1.2.3", "1.2.3 || 2.0.0", "^1.2.3"],
)
def test_semver_and_range_reject_unsupported_forms(value: str) -> None:
    parser = (
        VersionRange.parse
        if any(token in value for token in "|^,><=")
        else SemVer.parse
    )
    with pytest.raises(ValueError):
        parser(value)


def test_manifest_round_trips_strictly() -> None:
    data = manifest_data(lifecycle=["on_load", "on_unload"])
    value = ManifestValidator().validate(data)

    assert value.to_data() == data
    assert ManifestValidator().validate(value.to_data()) == value


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda data: data.update({"unknown": True}), "invalid_manifest"),
        (lambda data: data.pop("entrypoint"), "invalid_manifest"),
        (lambda data: data.update({"contract_version": "2"}), "invalid_manifest"),
        (lambda data: data.update({"name": "Not Namespaced"}), "invalid_manifest"),
        (lambda data: data.update({"version": "latest"}), "invalid_manifest"),
        (lambda data: data.update({"lifecycle": ["hidden_hook"]}), "invalid_manifest"),
    ],
)
def test_manifest_rejects_invalid_top_level_contract(mutation, code: str) -> None:
    data = manifest_data()
    mutation(data)

    with pytest.raises(CoreError) as raised:
        ManifestValidator().validate(data)

    assert raised.value.detail.code == code


def test_manifest_rejects_duplicate_capabilities_dependencies_and_permissions() -> None:
    for field in ("provides", "requires", "permissions"):
        data = manifest_data(requires=[plugin_dependency("test.base")])
        values = data[field]
        assert isinstance(values, list)
        values.append(deepcopy(values[0]))
        with pytest.raises(CoreError, match="invalid") as raised:
            ManifestValidator().validate(data)
        assert raised.value.detail.code == "invalid_manifest"


def test_manifest_rejects_unknown_nested_fields_and_wrong_types() -> None:
    data = manifest_data()
    provides = data["provides"]
    assert isinstance(provides, list)
    declaration = provides[0]
    assert isinstance(declaration, dict)
    declaration["unknown"] = "value"
    with pytest.raises(CoreError) as raised:
        ManifestValidator().validate(data)
    assert raised.value.detail.code == "invalid_manifest"

    data = manifest_data()
    data["provides"] = "not-an-array"
    with pytest.raises(CoreError):
        ManifestValidator().validate(data)


@pytest.mark.parametrize(
    "scope_pattern",
    ["workspace:*", "Core:workspace:*", "core:Workspace:*", "core:workspace:w*"],
)
def test_manifest_rejects_invalid_scope_pattern(scope_pattern: str) -> None:
    data = manifest_data()
    permissions = data["permissions"]
    assert isinstance(permissions, list)
    permission = permissions[0]
    assert isinstance(permission, dict)
    permission["scope_pattern"] = scope_pattern

    with pytest.raises(CoreError) as invalid:
        ManifestValidator().validate(data)
    assert invalid.value.detail.code == "invalid_manifest"


def test_preflight_separates_core_compatibility_and_permission_representation() -> None:
    value = manifest()
    PluginPreflight(
        core_api_version=SemVer.parse("0.2.5"),
        permissions=AllowRepresentablePermissions(),
    ).check_local(value)

    with pytest.raises(CoreError) as version_error:
        PluginPreflight(
            core_api_version=SemVer.parse("1.0.0"),
            permissions=AllowRepresentablePermissions(),
        ).check_local(value)
    assert version_error.value.detail.code == "incompatible_core_api"

    with pytest.raises(CoreError) as permission_error:
        PluginPreflight(
            core_api_version=SemVer.parse("0.2.0"),
            permissions=RejectPermissions(),
        ).check_local(value)
    assert permission_error.value.detail.code == "permission_denied"


def test_dependency_resolution_is_permutation_independent() -> None:
    base = manifest(name="test.base", version="1.0.0")
    middle = manifest(
        name="test.middle",
        version="1.0.0",
        requires=[plugin_dependency("test.base")],
    )
    top = manifest(
        name="test.top",
        version="1.0.0",
        requires=[plugin_dependency("test.middle")],
    )
    plans = {
        DependencyResolver().resolve(tuple(items)).ordered_plugins
        for items in permutations((base, middle, top))
    }

    assert plans == {(base.ref, middle.ref, top.ref)}


def test_dependency_resolution_uses_available_plugin_and_capability() -> None:
    base = manifest(name="test.base", version="1.1.0")
    consumer = manifest(
        name="test.consumer",
        requires=[
            plugin_dependency("test.base"),
            capability_dependency(base.provides[0].capability_id),
        ],
    )
    available = CapabilityCatalogSnapshot(
        plugins=(base.ref,),
        capabilities=(CatalogCapability(base.ref, base.provides[0]),),
    )

    plan = DependencyResolver().resolve((consumer,), available)

    assert plan.ordered_plugins == (consumer.ref,)
    assert len(plan.bindings) == 2
    assert {item.provider for item in plan.bindings} == {base.ref}


@pytest.mark.parametrize(
    ("requires", "available", "code", "category", "retryable"),
    [
        (
            [plugin_dependency("test.missing")],
            CapabilityCatalogSnapshot(),
            "dependency_unavailable",
            ErrorCategory.UNAVAILABLE,
            True,
        ),
        (
            [plugin_dependency("test.base", ">=2.0.0")],
            CapabilityCatalogSnapshot(plugins=(manifest(name="test.base").ref,)),
            "dependency_unavailable",
            ErrorCategory.VERSION_MISMATCH,
            False,
        ),
    ],
)
def test_dependency_resolution_reports_missing_and_incompatible(
    requires: list[object],
    available: CapabilityCatalogSnapshot,
    code: str,
    category: ErrorCategory,
    retryable: bool,
) -> None:
    consumer = manifest(name="test.consumer", requires=requires)
    with pytest.raises(CoreError) as raised:
        DependencyResolver().resolve((consumer,), available)
    assert raised.value.detail.code == code
    assert raised.value.detail.category is category
    assert raised.value.detail.retryable is retryable


def test_dependency_resolution_rejects_cycle_self_cycle_and_ambiguity() -> None:
    left = manifest(
        name="test.left",
        requires=[plugin_dependency("test.right")],
    )
    right = manifest(
        name="test.right",
        requires=[plugin_dependency("test.left")],
    )
    with pytest.raises(CoreError) as cycle:
        DependencyResolver().resolve((right, left))
    assert cycle.value.detail.code == "dependency_cycle"

    self_dependent = manifest(
        name="test.self",
        requires=[plugin_dependency("test.self")],
    )
    with pytest.raises(CoreError) as self_cycle:
        DependencyResolver().resolve((self_dependent,))
    assert self_cycle.value.detail.code == "dependency_cycle"

    first = manifest(name="test.one")
    second = manifest(name="test.two", version="1.1.0")
    consumer = manifest(
        name="test.consumer",
        requires=[capability_dependency(first.provides[0].capability_id)],
    )
    duplicate = CatalogCapability(
        second.ref,
        replace(
            first.provides[0],
            contract_version=SemVer.parse("1.1.0"),
        ),
    )
    available = CapabilityCatalogSnapshot(
        capabilities=(CatalogCapability(first.ref, first.provides[0]), duplicate)
    )
    with pytest.raises(CoreError) as ambiguity:
        DependencyResolver().resolve((consumer,), available)
    assert ambiguity.value.detail.code == "dependency_ambiguous"


def test_dependency_catalog_rejects_duplicate_versions_and_capabilities() -> None:
    value = manifest()
    with pytest.raises(ValueError):
        CapabilityCatalogSnapshot(plugins=(value.ref, value.ref))
    with pytest.raises(ValueError):
        CapabilityCatalogSnapshot(
            capabilities=(
                CatalogCapability(value.ref, value.provides[0]),
                CatalogCapability(value.ref, value.provides[0]),
            )
        )


def test_registry_commit_is_atomic_owned_and_idempotently_unregistered() -> None:
    value = manifest()
    registry = CapabilityRegistry()
    plan = CapabilityRegistrationPlan(
        value.ref,
        (LoadedCapability(value.provides[0], object()),),
    )

    receipt = registry.commit(plan, expected_version=0)
    committed = registry.snapshot()

    assert committed.version == 1
    assert tuple(committed.registrations) == receipt.capability_keys
    assert registry.get(receipt.capability_keys[0]).owner == value.ref
    assert RegistrationReceipt.from_data(receipt.to_data()) == receipt
    removed = registry.unregister(receipt)
    assert removed.version == 2
    assert not removed.registrations
    assert registry.unregister(receipt) is removed


def test_registry_generation_identity_rejects_stale_receipt() -> None:
    value = manifest()
    registry = CapabilityRegistry()
    plan = CapabilityRegistrationPlan(
        value.ref,
        (LoadedCapability(value.provides[0], object()),),
    )

    first = registry.commit(plan, expected_version=0)
    registry.unregister(first)
    second = registry.commit(plan, expected_version=2)

    assert second.registration_id != first.registration_id
    assert registry.unregister(first) is registry.snapshot()
    assert registry.get(second.capability_keys[0]).registration_id == (
        second.registration_id
    )
    removed = registry.unregister(second)
    assert not removed.registrations


def test_registry_readers_never_observe_partial_registration() -> None:
    data = manifest_data()
    provides = data["provides"]
    assert isinstance(provides, list)
    second = deepcopy(provides[0])
    assert isinstance(second, dict)
    second["capability_id"] = "test.echo.second"
    provides.append(second)
    value = ManifestValidator().validate(data)
    registry = CapabilityRegistry()
    initial = registry.snapshot()
    plan = CapabilityRegistrationPlan(
        value.ref,
        tuple(LoadedCapability(item, object()) for item in value.provides),
    )
    stop = Event()
    observed = [len(initial.registrations)]

    def read_snapshots() -> None:
        while not stop.is_set():
            observed.append(len(registry.snapshot().registrations))

    reader = Thread(target=read_snapshots)
    reader.start()
    try:
        registry.commit(plan, expected_version=0)
        observed.append(len(registry.snapshot().registrations))
    finally:
        stop.set()
        reader.join()

    assert set(observed) <= {0, 2}
    assert not initial.registrations


def test_registry_conflicts_never_change_committed_snapshot() -> None:
    value = manifest()
    registry = CapabilityRegistry()
    plan = CapabilityRegistrationPlan(
        value.ref,
        (LoadedCapability(value.provides[0], object()),),
    )
    receipt = registry.commit(plan, expected_version=0)
    committed = registry.snapshot()

    for expected_version in (0, 1):
        with pytest.raises(CoreError) as raised:
            registry.commit(plan, expected_version=expected_version)
        assert raised.value.detail.code == "registration_conflict"
        assert registry.snapshot() is committed

    wrong = RegistrationReceipt(
        receipt.registration_id + "-wrong",
        manifest(name="test.other").ref,
        receipt.registry_version,
        receipt.capability_keys,
    )
    with pytest.raises(CoreError) as identity:
        registry.rollback(wrong)
    assert identity.value.detail.code == "registration_identity_conflict"
    assert registry.snapshot() is committed


def test_registry_rejects_duplicate_plan_and_missing_capability() -> None:
    value = manifest()
    loaded = LoadedCapability(value.provides[0], object())
    with pytest.raises(ValueError):
        CapabilityRegistrationPlan(value.ref, (loaded, loaded))
    with pytest.raises(CoreError) as missing:
        CapabilityRegistry().get(value.provides[0].key)
    assert missing.value.detail.code == "capability_not_registered"


def test_plugin_lifecycle_state_enum_is_exact() -> None:
    assert [state.value for state in PluginLifecycleState] == [
        "discovered",
        "validated",
        "loaded",
        "registered",
        "active",
        "draining",
        "unregistered",
        "unloaded",
    ]
