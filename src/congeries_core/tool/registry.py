"""Typed read-only Tool view over the atomic Plugin capability registry."""

from __future__ import annotations

from dataclasses import dataclass

from congeries_core.plugin.model import CapabilityType, PluginRef
from congeries_core.plugin.registry import CapabilityRegistry
from congeries_core.policy.authorization import ActionRegistry
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.schema import SchemaRegistry

from .model import TOOL_CONTRACT_VERSION, ToolDescriptor, ToolImplementation


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    descriptor: ToolDescriptor
    owner: PluginRef
    registration_id: str


class ToolRegistry:
    def __init__(
        self,
        registry: CapabilityRegistry,
        schemas: SchemaRegistry,
        actions: ActionRegistry,
    ) -> None:
        self._registry = registry
        self._schemas = schemas
        self._actions = actions

    def resolve(self, ref: CapabilityRef) -> ResolvedTool:
        if ref.namespace != "core" or ref.kind != CapabilityType.TOOL.value:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "invalid_tool_reference",
                "Tool reference namespace or kind is invalid",
            )
        if ref.contract_version != TOOL_CONTRACT_VERSION:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "tool_contract_version_mismatch",
                "Tool contract version is unsupported",
            )
        registration = self._registry.get(ref.registration_key)
        if registration.owner.name != ref.owning_extension:
            raise core_error(
                ErrorCategory.CONFLICT,
                "tool_owner_mismatch",
                "Tool reference does not identify its owning Plugin",
            )
        implementation = registration.implementation
        if not isinstance(implementation, ToolImplementation):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "invalid_tool_implementation",
                "Plugin Tool implementation does not satisfy the Tool v1 contract",
            )
        descriptor = implementation.descriptor
        if descriptor.ref != ref:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "tool_descriptor_mismatch",
                "Tool descriptor identity does not match its registration",
            )
        if not self._schemas.contains(
            descriptor.input_schema
        ) or not self._schemas.contains(descriptor.output_schema):
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "tool_schema_not_registered",
                "Tool input or output Schema is not registered",
            )
        declared = {item.action.key for item in registration.declaration.permissions}
        if descriptor.action.key not in declared:
            raise core_error(
                ErrorCategory.DENIED,
                "tool_permission_undeclared",
                "Tool Action is not declared by its Plugin capability",
            )
        if not self._actions.contains(descriptor.action):
            raise core_error(
                ErrorCategory.DENIED,
                "tool_action_not_registered",
                "Tool Action is not registered",
            )
        return ResolvedTool(
            descriptor, registration.owner, registration.registration_id
        )

    def validate(self, ref: CapabilityRef) -> None:
        self.resolve(ref)
