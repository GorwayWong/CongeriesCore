"""Replaceable Plugin loading boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from congeries_core.runtime.context import RuntimeCallContext

from .model import CapabilityDeclaration, PluginManifest

type PluginHook = Callable[[RuntimeCallContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PluginHooks:
    on_load: PluginHook | None = None
    on_activate: PluginHook | None = None
    on_drain: PluginHook | None = None
    on_unload: PluginHook | None = None


@dataclass(frozen=True, slots=True)
class LoadedCapability:
    declaration: CapabilityDeclaration
    implementation: object


@dataclass(frozen=True, slots=True)
class PreparedPlugin:
    manifest: PluginManifest
    capabilities: tuple[LoadedCapability, ...]
    hooks: PluginHooks = PluginHooks()


@runtime_checkable
class CompositeCapabilityImplementation(Protocol):
    """A capability that validates its atomically published companion set."""

    def validate_composition(
        self, plugin_id: str, capabilities: tuple[LoadedCapability, ...]
    ) -> None: ...


class PluginLoader(Protocol):
    async def prepare(
        self, manifest: PluginManifest, context: RuntimeCallContext
    ) -> PreparedPlugin: ...

    async def cleanup(
        self, prepared: PreparedPlugin, context: RuntimeCallContext
    ) -> None: ...
