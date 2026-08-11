"""Immutable Skill v1 descriptors and progressive resource contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from congeries_core.policy.authorization import ActionRef
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.ids import ResourceId
from congeries_core.runtime.json_types import as_array, as_int, as_object

SKILL_CONTRACT_VERSION = "1"
SKILL_RESOURCE_READ_ACTION = ActionRef("core", "skill.resource.read", "1")


def skill_actions() -> tuple[ActionRef, ...]:
    return (SKILL_RESOURCE_READ_ACTION,)


class SkillResourceKind(StrEnum):
    INSTRUCTION = "instruction"
    EXAMPLE = "example"
    SCRIPT = "script"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class SkillResourceDescriptor:
    resource_id: ResourceId
    kind: SkillResourceKind
    path: str
    media_type: str
    max_bytes: int
    action: ActionRef = SKILL_RESOURCE_READ_ACTION

    def __post_init__(self) -> None:
        _require_resource_path(self.path)
        _require_text(self.media_type, "Skill resource media type")
        if self.max_bytes < 1:
            raise ValueError("Skill resource max_bytes must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id.value,
            "kind": self.kind.value,
            "path": self.path,
            "media_type": self.media_type,
            "max_bytes": self.max_bytes,
            "action": self.action.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillResourceDescriptor:
        if set(data) != {
            "resource_id",
            "kind",
            "path",
            "media_type",
            "max_bytes",
            "action",
        }:
            raise ValueError("Skill resource descriptor fields are invalid")
        return cls(
            resource_id=ResourceId(str(data["resource_id"])),
            kind=SkillResourceKind(str(data["kind"])),
            path=str(data["path"]),
            media_type=str(data["media_type"]),
            max_bytes=as_int(data["max_bytes"], "Skill resource max_bytes"),
            action=ActionRef.from_data(
                as_object(data["action"], "Skill resource action")
            ),
        )


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    ref: CapabilityRef
    title: str
    summary: str
    resources: tuple[SkillResourceDescriptor, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))
        if self.ref.namespace != "core" or self.ref.kind != "skill":
            raise ValueError("Skill descriptor requires a core Skill reference")
        if self.ref.contract_version != SKILL_CONTRACT_VERSION:
            raise ValueError("Skill descriptor contract version is unsupported")
        _require_text(self.title, "Skill title")
        _require_text(self.summary, "Skill summary")
        ids = [item.resource_id for item in self.resources]
        paths = [item.path for item in self.resources]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ValueError("Skill resources must have unique identities and paths")

    def resource(self, resource_id: ResourceId) -> SkillResourceDescriptor:
        for resource in self.resources:
            if resource.resource_id == resource_id:
                return resource
        raise KeyError(resource_id.value)

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_data(),
            "title": self.title,
            "summary": self.summary,
            "resources": [item.to_data() for item in self.resources],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillDescriptor:
        if set(data) != {"ref", "title", "summary", "resources"}:
            raise ValueError("Skill descriptor fields are invalid")
        return cls(
            ref=CapabilityRef.from_data(as_object(data["ref"], "Skill reference")),
            title=str(data["title"]),
            summary=str(data["summary"]),
            resources=tuple(
                SkillResourceDescriptor.from_data(as_object(item, "Skill resource"))
                for item in as_array(data["resources"], "Skill resources")
            ),
        )


@dataclass(frozen=True, slots=True)
class SkillResourceRequest:
    skill: CapabilityRef
    resource_id: ResourceId
    max_bytes: int

    def __post_init__(self) -> None:
        if (
            self.skill.namespace != "core"
            or self.skill.kind != "skill"
            or self.skill.contract_version != SKILL_CONTRACT_VERSION
        ):
            raise ValueError("Skill resource request requires a Skill v1 reference")
        if self.max_bytes < 1:
            raise ValueError("Skill resource request max_bytes must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "skill": self.skill.to_data(),
            "resource_id": self.resource_id.value,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillResourceRequest:
        if set(data) != {"skill", "resource_id", "max_bytes"}:
            raise ValueError("Skill resource request fields are invalid")
        return cls(
            skill=CapabilityRef.from_data(as_object(data["skill"], "Skill reference")),
            resource_id=ResourceId(str(data["resource_id"])),
            max_bytes=as_int(data["max_bytes"], "Skill resource request max_bytes"),
        )


@dataclass(frozen=True, slots=True)
class SkillResource:
    skill: CapabilityRef
    descriptor: SkillResourceDescriptor
    content: ContentBlock

    def __post_init__(self) -> None:
        if (
            self.skill.namespace != "core"
            or self.skill.kind != "skill"
            or self.skill.contract_version != SKILL_CONTRACT_VERSION
        ):
            raise ValueError("Skill resource requires a Skill v1 reference")
        if self.content.media_type != self.descriptor.media_type:
            raise ValueError(
                "Skill resource content media type does not match descriptor"
            )

    @property
    def byte_count(self) -> int:
        if self.content.kind in {ContentKind.TEXT, ContentKind.REFERENCE}:
            return len(str(self.content.value).encode())
        return len(
            json.dumps(
                self.content.value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )

    def to_data(self) -> dict[str, object]:
        return {
            "skill": self.skill.to_data(),
            "descriptor": self.descriptor.to_data(),
            "content": self.content.to_data(),
            "byte_count": self.byte_count,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SkillResource:
        if set(data) != {"skill", "descriptor", "content", "byte_count"}:
            raise ValueError("Skill resource fields are invalid")
        resource = cls(
            skill=CapabilityRef.from_data(as_object(data["skill"], "Skill reference")),
            descriptor=SkillResourceDescriptor.from_data(
                as_object(data["descriptor"], "Skill resource descriptor")
            ),
            content=ContentBlock.from_data(as_object(data["content"], "Skill content")),
        )
        if (
            as_int(data["byte_count"], "Skill resource byte_count")
            != resource.byte_count
        ):
            raise ValueError("Skill resource byte_count does not match content")
        return resource


class SkillResourceLoader(Protocol):
    async def load_resource(
        self, descriptor: SkillResourceDescriptor, context: RuntimeCallContext
    ) -> ContentBlock: ...


@dataclass(frozen=True, slots=True)
class SkillImplementation:
    descriptor: SkillDescriptor
    loader: SkillResourceLoader


def _require_resource_path(value: str) -> None:
    if not value or value != value.strip() or "\\" in value or value.startswith("/"):
        raise ValueError("Skill resource path must be a trimmed relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts) or str(path) != value:
        raise ValueError("Skill resource path must be normalized and traversal-free")


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")
