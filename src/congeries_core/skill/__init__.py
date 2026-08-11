"""Skill v1 contracts, typed registry, resolver, and resource gateway."""

from .gateway import (
    SKILL_RESOURCE_LOAD_COMPLETED,
    SKILL_RESOURCE_LOAD_FAILED,
    SKILL_RESOURCE_LOAD_STARTED,
    SkillResourceGateway,
)
from .model import (
    SKILL_CONTRACT_VERSION,
    SKILL_RESOURCE_READ_ACTION,
    SkillDescriptor,
    SkillImplementation,
    SkillResource,
    SkillResourceDescriptor,
    SkillResourceKind,
    SkillResourceLoader,
    SkillResourceRequest,
    skill_actions,
)
from .registry import ResolvedSkill, SkillRegistry
from .resolver import SkillToolResolver

__all__ = [
    "SKILL_CONTRACT_VERSION",
    "SKILL_RESOURCE_LOAD_COMPLETED",
    "SKILL_RESOURCE_LOAD_FAILED",
    "SKILL_RESOURCE_LOAD_STARTED",
    "SKILL_RESOURCE_READ_ACTION",
    "ResolvedSkill",
    "SkillDescriptor",
    "SkillImplementation",
    "SkillRegistry",
    "SkillResource",
    "SkillResourceDescriptor",
    "SkillResourceGateway",
    "SkillResourceKind",
    "SkillResourceLoader",
    "SkillResourceRequest",
    "SkillToolResolver",
    "skill_actions",
]
