"""Small event boundary shared by provider gateways."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.json_types import JsonValue


class ProviderEventPublisher(Protocol):
    async def provider_event(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None: ...


class NullProviderEventPublisher:
    async def provider_event(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        del event_type, context, payload
