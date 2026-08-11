"""Shared Skill/Tool reference resolver for Agent and future Workflow adapters."""

from __future__ import annotations

from congeries_core.runtime.capability import CapabilityRef
from congeries_core.tool.registry import ResolvedTool, ToolRegistry

from .registry import ResolvedSkill, SkillRegistry


class SkillToolResolver:
    def __init__(self, skills: SkillRegistry, tools: ToolRegistry) -> None:
        self._skills = skills
        self._tools = tools

    def resolve_skill(self, ref: CapabilityRef) -> ResolvedSkill:
        return self._skills.resolve(ref)

    def resolve_tool(self, ref: CapabilityRef) -> ResolvedTool:
        return self._tools.resolve(ref)

    def validate_skill(self, ref: CapabilityRef) -> None:
        self.resolve_skill(ref)

    def validate_tool(self, ref: CapabilityRef) -> None:
        self.resolve_tool(ref)
