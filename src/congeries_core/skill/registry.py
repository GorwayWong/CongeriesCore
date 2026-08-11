"""Typed read-only Skill view over the atomic Plugin capability registry."""

from __future__ import annotations

from dataclasses import dataclass

from congeries_core.plugin.model import CapabilityType, PluginRef
from congeries_core.plugin.registry import CapabilityRegistry
from congeries_core.policy.authorization import ActionRegistry
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.errors import ErrorCategory, core_error

from .model import SKILL_CONTRACT_VERSION, SkillDescriptor, SkillImplementation


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    descriptor: SkillDescriptor
    owner: PluginRef
    registration_id: str


class SkillRegistry:
    def __init__(self, registry: CapabilityRegistry, actions: ActionRegistry) -> None:
        self._registry = registry
        self._actions = actions

    def resolve(self, ref: CapabilityRef) -> ResolvedSkill:
        if ref.namespace != "core" or ref.kind != CapabilityType.SKILL.value:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "invalid_skill_reference",
                "Skill reference namespace or kind is invalid",
            )
        if ref.contract_version != SKILL_CONTRACT_VERSION:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "skill_contract_version_mismatch",
                "Skill contract version is unsupported",
            )
        registration = self._registry.get(ref.registration_key)
        if registration.owner.name != ref.owning_extension:
            raise core_error(
                ErrorCategory.CONFLICT,
                "skill_owner_mismatch",
                "Skill reference does not identify its owning Plugin",
            )
        implementation = registration.implementation
        if not isinstance(implementation, SkillImplementation):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "invalid_skill_implementation",
                "Plugin Skill implementation does not satisfy the Skill v1 contract",
            )
        descriptor = implementation.descriptor
        if descriptor.ref != ref:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "skill_descriptor_mismatch",
                "Skill descriptor identity does not match its registration",
            )
        declared = {item.action.key for item in registration.declaration.permissions}
        for resource in descriptor.resources:
            if resource.action.key not in declared:
                raise core_error(
                    ErrorCategory.DENIED,
                    "skill_permission_undeclared",
                    "Skill resource Action is not declared by its Plugin capability",
                )
            if not self._actions.contains(resource.action):
                raise core_error(
                    ErrorCategory.DENIED,
                    "skill_action_not_registered",
                    "Skill resource Action is not registered",
                )
        return ResolvedSkill(
            descriptor, registration.owner, registration.registration_id
        )

    def validate(self, ref: CapabilityRef) -> None:
        self.resolve(ref)
