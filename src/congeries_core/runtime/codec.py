"""Explicit JSON boundary for public dataclass contracts."""

from __future__ import annotations

import json
from typing import Any, Protocol, cast


class JsonModel(Protocol):
    def to_data(self) -> dict[str, object]: ...


class JsonModelType[ModelT](Protocol):
    @classmethod
    def from_data(cls, data: dict[str, object]) -> ModelT: ...


def dumps(model: JsonModel) -> str:
    return json.dumps(
        model.to_data(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads[ModelT](model_type: JsonModelType[ModelT], payload: str) -> ModelT:
    decoded: Any = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("serialized model must be a JSON object")
    return model_type.from_data(cast(dict[str, object], decoded))
