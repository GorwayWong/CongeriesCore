"""Authorized progressive Skill resource loading."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime

from congeries_core.plugin.invocation import PluginCapabilityInvoker
from congeries_core.plugin.registry import CapabilityRegistration
from congeries_core.policy.authorization import (
    AuthorizedCall,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider.events import (
    NullProviderEventPublisher,
    ProviderEventPublisher,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import PrincipalId, ResourceId
from congeries_core.runtime.json_types import JsonValue

from .model import SkillImplementation, SkillResource, SkillResourceRequest
from .registry import SkillRegistry

SKILL_RESOURCE_LOAD_STARTED = "core.skill.resource_load_started"
SKILL_RESOURCE_LOAD_COMPLETED = "core.skill.resource_load_completed"
SKILL_RESOURCE_LOAD_FAILED = "core.skill.resource_load_failed"


class SkillResourceGateway:
    def __init__(
        self,
        *,
        skills: SkillRegistry,
        invoker: PluginCapabilityInvoker,
        clock: Clock,
        events: ProviderEventPublisher | None = None,
    ) -> None:
        self._skills = skills
        self._invoker = invoker
        self._clock = clock
        self._events = events or NullProviderEventPublisher()

    async def load(
        self, request: SkillResourceRequest, context: RuntimeCallContext
    ) -> SkillResource:
        if context.idempotency_key is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "missing_invocation_identity",
                "Skill resource loading requires an invocation identity",
            )
        resolved = self._skills.resolve(request.skill)
        try:
            descriptor = resolved.descriptor.resource(request.resource_id)
        except KeyError as error:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "skill_resource_undeclared",
                "Skill resource is not declared",
            ) from error
        if request.max_bytes > descriptor.max_bytes:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "skill_resource_budget_exceeded",
                "Skill resource request exceeds its declared byte budget",
            )
        started_at = self._clock.now()
        await self._emit(
            SKILL_RESOURCE_LOAD_STARTED, context, self._payload(request, 0)
        )
        constraints: Mapping[str, JsonValue] = {
            "resource_id": descriptor.resource_id.value,
            "path": descriptor.path,
            "media_type": descriptor.media_type,
            "max_bytes": request.max_bytes,
        }
        authorization_resource = ResourceRef(
            "core",
            "skill_resource",
            ResourceId(f"{request.skill.id.value}:{request.resource_id.value}"),
            owning_extension=request.skill.owning_extension,
        )
        try:

            async def operation(
                registration: CapabilityRegistration, call: AuthorizedCall
            ) -> SkillResource:
                # The invoker holds one Plugin lease for this entire callback:
                # grant narrowing, loader access, media verification, and the
                # actual byte-count check. Returning the resource never injects
                # it into Agent context; composition remains a caller decision.
                effective_max = self._validate_grant(
                    descriptor.to_data(), request.max_bytes, call
                )
                implementation = registration.implementation
                if not isinstance(implementation, SkillImplementation):
                    raise self._protocol_failure("Skill implementation is invalid")
                try:
                    content = await implementation.loader.load_resource(
                        descriptor, call.context
                    )
                except CoreError:
                    raise
                except Exception as error:
                    raise core_error(
                        ErrorCategory.UNAVAILABLE,
                        "skill_resource_loader_failure",
                        "Skill resource loader failed",
                        retryable=True,
                        cause_id=type(error).__name__,
                    ) from error
                if content.media_type != descriptor.media_type:
                    raise self._protocol_failure(
                        "Skill resource media type does not match descriptor"
                    )
                resource = SkillResource(request.skill, descriptor, content)
                if resource.byte_count > effective_max:
                    raise core_error(
                        ErrorCategory.INVALID_REQUEST,
                        "skill_resource_budget_exceeded",
                        "Loaded Skill resource exceeds the effective byte budget",
                    )
                return resource

            resource = await self._invoker.invoke(
                plugin_id=request.skill.owning_extension,
                capability_key=request.skill.registration_key,
                action=descriptor.action,
                resource=authorization_resource,
                context=context,
                principal=RuntimePrincipal.core(
                    CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
                ),
                constraints=constraints,
                operation=operation,
                resource_validator=lambda registration, resource: (
                    registration.declaration.key == request.skill.registration_key
                    and resource == authorization_resource
                ),
            )
            await self._emit(
                SKILL_RESOURCE_LOAD_COMPLETED,
                context,
                self._payload(
                    request, resource.byte_count, latency=self._elapsed_ms(started_at)
                ),
            )
            return resource
        except CoreError as error:
            await self._emit_failure(request, context, started_at, error.detail)
            raise

    def _validate_grant(
        self, descriptor: Mapping[str, object], requested_max: int, call: AuthorizedCall
    ) -> int:
        constraints = call.grant.constraints
        allowed = {"resource_id", "path", "media_type", "max_bytes"}
        if set(constraints).difference(allowed):
            raise self._invalid_grant(
                "Skill resource grant contains unknown constraints"
            )
        for key in ("resource_id", "path", "media_type"):
            if constraints.get(key, descriptor[key]) != descriptor[key]:
                raise self._invalid_grant(f"Skill resource grant changes {key}")
        raw_max = constraints.get("max_bytes")
        if raw_max is None:
            return requested_max
        if isinstance(raw_max, bool) or not isinstance(raw_max, int):
            raise self._invalid_grant("Skill resource max_bytes grant is invalid")
        if raw_max < 1 or raw_max > requested_max:
            raise self._invalid_grant("Skill resource grant broadens max_bytes")
        return raw_max

    def _payload(
        self, request: SkillResourceRequest, byte_count: int, *, latency: int = 0
    ) -> Mapping[str, JsonValue]:
        return {
            "skill_id": request.skill.id.value,
            "resource_id": request.resource_id.value,
            "byte_count": byte_count,
            "latency_ms": latency,
        }

    async def _emit_failure(
        self,
        request: SkillResourceRequest,
        context: RuntimeCallContext,
        started_at: datetime,
        error: ErrorDetail,
    ) -> None:
        payload = dict(self._payload(request, 0, latency=self._elapsed_ms(started_at)))
        payload.update({"category": error.category.value, "error_code": error.code})
        await self._emit(SKILL_RESOURCE_LOAD_FAILED, context, payload)

    async def _emit(
        self,
        event_type: str,
        context: RuntimeCallContext,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with suppress(Exception):
            await self._events.provider_event(event_type, context, payload)

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((self._clock.now() - started_at).total_seconds() * 1_000))

    def _protocol_failure(self, message: str) -> CoreError:
        return core_error(
            ErrorCategory.PROTOCOL_FAILURE, "skill_protocol_failure", message
        )

    def _invalid_grant(self, message: str) -> CoreError:
        return core_error(ErrorCategory.DENIED, "invalid_grant", message)
