"""Atomic Plugin capability composition validation for MCP adapters."""

from __future__ import annotations

from congeries_core.plugin.loader import LoadedCapability
from congeries_core.plugin.model import CapabilityType
from congeries_core.provider.context import (
    CONTEXT_CAPABILITIES_ACTION,
    CONTEXT_PROVIDE_ACTION,
)
from congeries_core.tool.model import ToolImplementation

from .integration import McpContextProviderImplementation, McpToolExecutor
from .model import McpAdapterImplementation


def validate_mcp_composition(
    adapter: McpAdapterImplementation,
    plugin_id: str,
    capabilities: tuple[LoadedCapability, ...],
) -> None:
    """Validate the complete prepared Plugin before its atomic registry commit.

    Discovery is never allowed to fill a missing declaration later. Every local
    facade must already be present and must point at this exact adapter instance,
    so publication either exposes the whole mapping or exposes none of it.
    """

    descriptor = adapter.descriptor
    if descriptor.ref.owning_extension != plugin_id:
        raise ValueError("MCP Adapter owner does not match its Plugin")
    by_key = {item.declaration.key: item for item in capabilities}
    adapter_capability = by_key.get(descriptor.ref.registration_key)
    if adapter_capability is None or adapter_capability.implementation is not adapter:
        raise ValueError("MCP Adapter declaration and implementation do not match")

    for binding in descriptor.tool_bindings:
        capability = by_key.get(binding.tool.registration_key)
        if capability is None or capability.declaration.type is not CapabilityType.TOOL:
            raise ValueError("MCP mapped Tool is not atomically declared")
        implementation = capability.implementation
        if (
            not isinstance(implementation, ToolImplementation)
            or implementation.descriptor.ref != binding.tool
            or not isinstance(implementation.executor, McpToolExecutor)
            or implementation.executor.adapter is not adapter
        ):
            raise ValueError("MCP mapped Tool implementation is invalid")
        permissions = {item.action.key for item in capability.declaration.permissions}
        if implementation.descriptor.action.key not in permissions:
            raise ValueError("MCP mapped Tool permission is not declared")

    context_actions = {
        CONTEXT_CAPABILITIES_ACTION.key,
        CONTEXT_PROVIDE_ACTION.key,
    }
    for binding in descriptor.context_bindings:
        capability = by_key.get(binding.provider.registration_key)
        if (
            capability is None
            or capability.declaration.type is not CapabilityType.CONTEXT_PROVIDER
        ):
            raise ValueError("MCP mapped ContextProvider is not atomically declared")
        implementation = capability.implementation
        if (
            not isinstance(implementation, McpContextProviderImplementation)
            or implementation.adapter is not adapter
            or implementation.binding != binding
        ):
            raise ValueError("MCP mapped ContextProvider implementation is invalid")
        permissions = {item.action.key for item in capability.declaration.permissions}
        if not context_actions.issubset(permissions):
            raise ValueError("MCP mapped ContextProvider permissions are incomplete")
