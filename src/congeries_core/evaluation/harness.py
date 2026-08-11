"""Deterministic fail-fast Evaluation composition."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from typing import Protocol

from congeries_core.policy.authorization import AuditFailureHandler
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import IdempotencyKey
from congeries_core.runtime.schema import SchemaRegistry

from .gateway import EvaluationPolicyGateway, QualityEvaluatorGateway
from .model import (
    EVALUATION_CONTRACT_VERSION,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStage,
    EvaluationStageResult,
    EvaluationVerdict,
    result_from_stages,
)


class EvaluationEventPublisher(Protocol):
    async def evaluation_started(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> None: ...

    async def evaluation_verdict_recorded(
        self,
        request: EvaluationRequest,
        result: EvaluationResult,
        context: RuntimeCallContext,
    ) -> None: ...


class NullEvaluationEventPublisher:
    async def evaluation_started(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> None:
        del request, context

    async def evaluation_verdict_recorded(
        self,
        request: EvaluationRequest,
        result: EvaluationResult,
        context: RuntimeCallContext,
    ) -> None:
        del request, result, context


class SchemaEvaluator:
    """Pure first gate: no policy/provider dispatch and no side effects."""

    def __init__(self, schemas: SchemaRegistry) -> None:
        self._schemas = schemas

    def evaluate(self, request: EvaluationRequest) -> EvaluationStageResult:
        if not self._schemas.contains(request.input_schema):
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "schema_not_registered",
                "Evaluation input schema is not registered",
            )
        try:
            self._schemas.validate(request.input_schema, request.value)
        except CoreError as error:
            if error.detail.code != "schema_validation_failed":
                raise
            return EvaluationStageResult(
                EvaluationStage.SCHEMA,
                EvaluationVerdict.SCHEMA_FAILED,
                "schema_validation_failed",
            )
        return EvaluationStageResult(
            EvaluationStage.SCHEMA, EvaluationVerdict.PASSED, "schema_valid"
        )


class EvaluationHarness:
    """Runs the fixed fail-fast pipeline and closes it with reliable audit."""

    def __init__(
        self,
        *,
        schema: SchemaEvaluator,
        policy: EvaluationPolicyGateway,
        quality: QualityEvaluatorGateway,
        events: EvaluationEventPublisher,
        audit_failure_handler: AuditFailureHandler,
        clock: Clock,
    ) -> None:
        self._schema = schema
        self._policy = policy
        self._quality = quality
        self._events = events
        self._audit_failure = audit_failure_handler
        self._clock = clock

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationResult:
        if context.idempotency_key is None:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "evaluation_idempotency_required",
                "Evaluation requires a parent idempotency key",
            )
        request.scope.require_narrower_than(context.scope)
        context.check_active(self._clock)
        # Started is observability only.  Losing it must not change a verdict;
        # the terminal audit below is the reliability gate.
        with suppress(Exception):
            await self._events.evaluation_started(request, context)

        stages: list[EvaluationStageResult] = []
        terminal_error: ErrorDetail | None = None
        try:
            # Keep later stages lexically inside the preceding PASSED branch.
            # This is deliberately less abstract than a generic reducer because
            # the visible control flow protects the no-failure-laundering rule.
            schema_result = self._schema.evaluate(request)
            stages.append(schema_result)
            if schema_result.verdict is EvaluationVerdict.PASSED:
                policy_context = self._stage_context(
                    context, request, EvaluationStage.POLICY
                )
                policy_result = await self._policy.evaluate(request, policy_context)
                policy_context.check_active(self._clock)
                stages.append(policy_result)
                if policy_result.verdict is EvaluationVerdict.PASSED:
                    quality_context = self._stage_context(
                        context, request, EvaluationStage.QUALITY
                    )
                    quality_result = await self._quality.evaluate(
                        request, quality_context
                    )
                    quality_context.check_active(self._clock)
                    stages.append(quality_result)
        except CoreError as error:
            stage = (
                EvaluationStage.SCHEMA
                if not stages
                else EvaluationStage.POLICY
                if len(stages) == 1
                else EvaluationStage.QUALITY
            )
            terminal_error = _safe_error(error.detail)
            verdict = _error_verdict(terminal_error)
            stages.append(EvaluationStageResult(stage, verdict, terminal_error.code))
        return await self._finish(request, tuple(stages), context, error=terminal_error)

    async def _finish(
        self,
        request: EvaluationRequest,
        stages: tuple[EvaluationStageResult, ...],
        context: RuntimeCallContext,
        *,
        error: ErrorDetail | None = None,
    ) -> EvaluationResult:
        result = result_from_stages(request.evaluation_id, stages, error=error)
        try:
            # Do not suppress this publication.  Returning from _finish means a
            # required audit sink acknowledged the exact terminal result, which
            # is the Workflow runtime's permission to begin persistence.
            await self._events.evaluation_verdict_recorded(request, result, context)
        except CoreError as audit_error:
            await self._audit_failure.handle(context.run_id, audit_error.detail)
            raise
        return result

    @staticmethod
    def _stage_context(
        context: RuntimeCallContext,
        request: EvaluationRequest,
        stage: EvaluationStage,
    ) -> RuntimeCallContext:
        if context.idempotency_key is None:
            raise AssertionError("Evaluation parent idempotency was validated")
        # Include the request fingerprint as well as stage identity.  Replaying
        # the same request reproduces the key; reusing an evaluation_id with
        # changed input or bindings cannot alias an earlier provider operation.
        material = ":".join(
            (
                context.idempotency_key.value,
                request.evaluation_id.value,
                EVALUATION_CONTRACT_VERSION,
                request.fingerprint,
                stage.value,
            )
        )
        key = IdempotencyKey(hashlib.sha256(material.encode("utf-8")).hexdigest())
        return context.narrow(scope=request.scope, idempotency_key=key)


def _error_verdict(error: ErrorDetail) -> EvaluationVerdict:
    if error.category is ErrorCategory.TIMEOUT:
        return EvaluationVerdict.TIMED_OUT
    if error.category is ErrorCategory.CANCELLED:
        return EvaluationVerdict.CANCELLED
    return EvaluationVerdict.ERROR


def _safe_error(error: ErrorDetail) -> ErrorDetail:
    # Provider messages and metadata may contain evaluated content.  Preserve
    # only stable classification fields that are safe for results and Run state.
    return ErrorDetail(
        category=error.category,
        code=error.code,
        message="Evaluation stage did not complete successfully",
        retryable=error.retryable,
    )
