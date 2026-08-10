"""Provider-neutral typed content blocks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .json_types import JsonValue, as_json_value


class ContentKind(StrEnum):
    TEXT = "text"
    JSON = "json"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class ContentBlock:
    """A serializable content unit that never exposes provider SDK types."""

    kind: ContentKind
    value: JsonValue
    name: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        normalized = as_json_value(self.value, "content value")
        object.__setattr__(self, "value", normalized)
        if self.kind in {ContentKind.TEXT, ContentKind.REFERENCE} and not isinstance(
            normalized, str
        ):
            raise ValueError(f"{self.kind.value} content requires a string value")
        for field_name, value in (("name", self.name), ("media_type", self.media_type)):
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"content {field_name} must be non-empty and trimmed")

    @classmethod
    def text(cls, value: str, *, name: str | None = None) -> ContentBlock:
        return cls(ContentKind.TEXT, value, name=name, media_type="text/plain")

    @classmethod
    def json(cls, value: JsonValue, *, name: str | None = None) -> ContentBlock:
        return cls(ContentKind.JSON, value, name=name, media_type="application/json")

    @classmethod
    def reference(
        cls, value: str, *, name: str | None = None, media_type: str | None = None
    ) -> ContentBlock:
        return cls(ContentKind.REFERENCE, value, name=name, media_type=media_type)

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "name": self.name,
            "media_type": self.media_type,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> ContentBlock:
        return cls(
            kind=ContentKind(str(data["kind"])),
            value=as_json_value(data.get("value"), "content value"),
            name=str(data["name"]) if data.get("name") is not None else None,
            media_type=(
                str(data["media_type"]) if data.get("media_type") is not None else None
            ),
        )
