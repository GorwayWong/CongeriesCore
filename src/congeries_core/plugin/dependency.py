"""Pure deterministic Plugin dependency resolution."""

from __future__ import annotations

from dataclasses import dataclass

from congeries_core.runtime.errors import ErrorCategory

from .errors import plugin_error
from .model import (
    CapabilityDeclaration,
    DependencyKind,
    PluginDependency,
    PluginManifest,
    PluginRef,
)


@dataclass(frozen=True, slots=True)
class CatalogCapability:
    owner: PluginRef
    declaration: CapabilityDeclaration

    @property
    def stable_key(self) -> tuple[str, str, str, str, str]:
        return (*self.declaration.key, self.owner.name, str(self.owner.version))


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSnapshot:
    plugins: tuple[PluginRef, ...] = ()
    capabilities: tuple[CatalogCapability, ...] = ()

    def __post_init__(self) -> None:
        if len({plugin.name for plugin in self.plugins}) != len(self.plugins):
            raise ValueError("catalog contains multiple versions of one Plugin")
        keys = [item.declaration.key for item in self.capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError("catalog contains duplicate capability identities")


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    consumer: PluginRef
    dependency: PluginDependency
    provider: PluginRef
    capability: CapabilityDeclaration | None = None


@dataclass(frozen=True, slots=True)
class DependencyResolutionPlan:
    ordered_plugins: tuple[PluginRef, ...]
    bindings: tuple[ResolvedDependency, ...]


class DependencyResolver:
    def resolve(
        self,
        candidates: tuple[PluginManifest, ...],
        available: CapabilityCatalogSnapshot | None = None,
    ) -> DependencyResolutionPlan:
        available = available or CapabilityCatalogSnapshot()
        by_name: dict[str, PluginManifest] = {}
        for manifest in candidates:
            if manifest.name in by_name or any(
                plugin.name == manifest.name for plugin in available.plugins
            ):
                raise plugin_error(
                    ErrorCategory.CONFLICT,
                    "dependency_ambiguous",
                    "Multiple candidate or active versions exist for one Plugin",
                    plugin=manifest.name,
                )
            by_name[manifest.name] = manifest

        # Edges point from provider to consumer. Walking that direction produces
        # the order in which Plugins can be activated without consulting mutable
        # runtime state halfway through the calculation.
        edges: dict[str, set[str]] = {name: set() for name in by_name}
        incoming: dict[str, int] = {name: 0 for name in by_name}
        bindings: list[ResolvedDependency] = []
        for manifest in sorted(candidates, key=lambda item: (item.name, item.version)):
            for dependency in sorted(
                manifest.requires, key=lambda item: item.stable_key
            ):
                binding = self._resolve_one(dependency, manifest, by_name, available)
                bindings.append(binding)
                provider_name = binding.provider.name
                if provider_name in by_name and provider_name != manifest.name:
                    if manifest.name not in edges[provider_name]:
                        edges[provider_name].add(manifest.name)
                        incoming[manifest.name] += 1
                elif provider_name == manifest.name:
                    self._reject_self_plugin_dependency(manifest, dependency)

        # Sorting both the ready set and each dependent set is part of the public
        # contract: input permutation must never change the plan or cycle report.
        ready = sorted(name for name, count in incoming.items() if count == 0)
        ordered: list[PluginRef] = []
        while ready:
            current = ready.pop(0)
            ordered.append(by_name[current].ref)
            for dependent in sorted(edges[current]):
                incoming[dependent] -= 1
                if incoming[dependent] == 0:
                    ready.append(dependent)
                    ready.sort()
        if len(ordered) != len(candidates):
            cycle = ", ".join(sorted(name for name, count in incoming.items() if count))
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "dependency_cycle",
                f"Plugin dependency graph contains a cycle: {cycle}",
            )
        bindings.sort(
            key=lambda item: (
                item.consumer.name,
                item.dependency.stable_key,
                item.provider.name,
                item.capability.key if item.capability else ("", "", ""),
            )
        )
        return DependencyResolutionPlan(tuple(ordered), tuple(bindings))

    def _resolve_one(
        self,
        dependency: PluginDependency,
        consumer: PluginManifest,
        candidates: dict[str, PluginManifest],
        available: CapabilityCatalogSnapshot,
    ) -> ResolvedDependency:
        if dependency.kind is DependencyKind.PLUGIN:
            return self._resolve_plugin(dependency, consumer, candidates, available)
        return self._resolve_capability(dependency, consumer, candidates, available)

    def _resolve_plugin(
        self,
        dependency: PluginDependency,
        consumer: PluginManifest,
        candidates: dict[str, PluginManifest],
        available: CapabilityCatalogSnapshot,
    ) -> ResolvedDependency:
        plugin_id = dependency.plugin_id
        if plugin_id is None:
            raise AssertionError("validated Plugin dependency requires plugin_id")
        matches: list[PluginRef] = []
        candidate = candidates.get(plugin_id)
        if candidate is not None:
            matches.append(candidate.ref)
        matches.extend(
            plugin for plugin in available.plugins if plugin.name == plugin_id
        )
        compatible = [
            plugin
            for plugin in matches
            if dependency.version_range.matches(plugin.version)
        ]
        selected = self._select_provider(consumer, dependency, matches, compatible)
        return ResolvedDependency(consumer.ref, dependency, selected)

    def _resolve_capability(
        self,
        dependency: PluginDependency,
        consumer: PluginManifest,
        candidates: dict[str, PluginManifest],
        available: CapabilityCatalogSnapshot,
    ) -> ResolvedDependency:
        capability_type = dependency.capability_type
        capability_id = dependency.capability_id
        if capability_type is None or capability_id is None:
            raise AssertionError("validated capability dependency requires identity")
        # Resolve candidates and active registrations against one immutable
        # snapshot. More than one compatible provider is an error rather than an
        # arbitrary tie-break, which keeps capability ownership reproducible.
        providers: list[CatalogCapability] = list(available.capabilities)
        for manifest in candidates.values():
            providers.extend(
                CatalogCapability(manifest.ref, declaration)
                for declaration in manifest.provides
            )
        matches = [
            item
            for item in providers
            if item.declaration.type is capability_type
            and item.declaration.capability_id == capability_id
        ]
        compatible = [
            item
            for item in matches
            if dependency.version_range.matches(item.declaration.contract_version)
        ]
        if not compatible:
            self._unavailable(consumer, dependency, bool(matches))
        if len(compatible) > 1:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "dependency_ambiguous",
                "Capability dependency has multiple compatible providers",
                plugin=consumer.name,
                capability=capability_id,
            )
        selected = compatible[0]
        return ResolvedDependency(
            consumer.ref,
            dependency,
            selected.owner,
            selected.declaration,
        )

    def _select_provider(
        self,
        consumer: PluginManifest,
        dependency: PluginDependency,
        matches: list[PluginRef],
        compatible: list[PluginRef],
    ) -> PluginRef:
        if not compatible:
            self._unavailable(consumer, dependency, bool(matches))
        if len(compatible) > 1:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "dependency_ambiguous",
                "Plugin dependency has multiple compatible providers",
                plugin=consumer.name,
                capability=dependency.plugin_id,
            )
        return compatible[0]

    def _unavailable(
        self,
        consumer: PluginManifest,
        dependency: PluginDependency,
        incompatible: bool,
    ) -> None:
        raise plugin_error(
            ErrorCategory.VERSION_MISMATCH
            if incompatible
            else ErrorCategory.UNAVAILABLE,
            "dependency_unavailable",
            "Plugin dependency is incompatible"
            if incompatible
            else "Plugin dependency is unavailable",
            retryable=not incompatible,
            plugin=consumer.name,
            capability=dependency.capability_id or dependency.plugin_id,
        )

    def _reject_self_plugin_dependency(
        self, manifest: PluginManifest, dependency: PluginDependency
    ) -> None:
        if dependency.kind is DependencyKind.PLUGIN:
            raise plugin_error(
                ErrorCategory.CONFLICT,
                "dependency_cycle",
                "Plugin depends on itself",
                plugin=manifest.name,
            )
