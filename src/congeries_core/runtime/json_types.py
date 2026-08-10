"""JSON value types and strict boundary narrowing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)


def as_object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{field_name} keys must be strings")
    return {cast(str, key): item for key, item in raw.items()}


def as_array(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return cast(list[object], value)


def as_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def as_json_value(value: object, field_name: str = "value") -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [as_json_value(item, field_name) for item in cast(list[object], value)]
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in raw):
            raise ValueError(f"{field_name} object keys must be strings")
        return {
            cast(str, key): as_json_value(item, field_name) for key, item in raw.items()
        }
    raise ValueError(f"{field_name} must be valid JSON data")
