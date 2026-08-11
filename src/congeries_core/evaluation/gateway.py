"""Authorized, replaceable Evaluation policy and quality provider boundaries."""

from __future__ import annotations

from collections.abc import Collection
from typing import Never, Protocol

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import PrincipalId, ProviderId, ResourceId

from .model import (
    EVALUATION_CONTRACT_VERSION,
    EvaluationRequest,
    EvaluationStage,
    EvaluationStageResult,
    EvaluationVerdict,
    QualityEvaluatorCapabilities,
)

EVALUATION_POLICY_EVALUATE_ACTION = ActionRef("core", "evaluation.policy.evaluate", "1")
QUALITY_CAPABILITIES_ACTION = ActionRef("core", "evaluation.quality.capabilities", "1")
QUALITY_EVALUATE_ACTION = ActionRef("core", "evaluation.quality.evaluate", "1")


def evaluation_actions() -> tuple[ActionRef, ...]:
    return (
        EVALUATION_POLICY_EVALUATE_ACTION,
        QUALITY_CAPABILITIES_ACTION,
        QUALITY_EVALUATE_ACTION,
    )


class EvaluationPolicy(Protocol):
    """Judges content compliance; it does not grant permission to call itself."""

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult: ...


class QualityEvaluator(Protocol):
    """Replaceable quality provider whose business semantics stay outside Core."""

    async def capabilities(
        self, context: RuntimeCallContext
    ) -> QualityEvaluatorCapabilities: ...

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult: ...


class EvaluationPolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[str, EvaluationPolicy] = {}

    def register(self, policy_ref: str, policy: EvaluationPolicy) -> None:
        if policy_ref in self._policies:
            raise core_error(
                ErrorCategory.CONFLICT,
                "evaluation_policy_already_registered",
                "Evaluation policy is already registered",
            )
        self._policies[policy_ref] = policy

    def get(self, policy_ref: str) -> EvaluationPolicy:
        policy = self._policies.get(policy_ref)
        if policy is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "evaluation_policy_not_registered",
                "Evaluation policy is not registered",
            )
        return policy


class QualityEvaluatorRegistry:
    def __init__(self) -> None:
        self._evaluators: dict[ProviderId, QualityEvaluator] = {}

    def register(self, evaluator_id: ProviderId, evaluator: QualityEvaluator) -> None:
        if evaluator_id in self._evaluators:
            raise core_error(
                ErrorCategory.CONFLICT,
                "quality_evaluator_already_registered",
                "quality evaluator is already registered",
            )
        self._evaluators[evaluator_id] = evaluator

    def get(self, evaluator_id: ProviderId) -> QualityEvaluator:
        evaluator = self._evaluators.get(evaluator_id)
        if evaluator is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "quality_evaluator_not_registered",
                "quality evaluator is not registered",
            )
        return evaluator


class EvaluationPolicyGateway:
    """Applies access authorization around the independent content-policy call."""

    def __init__(
        self,
        *,
        policies: EvaluationPolicyRegistry,
        dispatcher: AuthorizedDispatcher[EvaluationStageResult],
        clock: Clock,
    ) -> None:
        self._policies = policies
        self._dispatcher = dispatcher
        self._clock = clock

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult:
        policy = self._policies.get(request.policy_ref)
        access = _access_request(
            request,
            context,
            EVALUATION_POLICY_EVALUATE_ACTION,
            "evaluation_policy",
            request.policy_ref,
        )

        async def operation(call: AuthorizedCall) -> EvaluationStageResult:
            # Authorization answers "may this Run call this policy?".  Only the
            # provider result below answers "does the content pass the policy?".
            _validate_grant_constraints(call)
            result = await await_provider(
                policy.evaluate(request, call.context), call.context, self._clock
            )
            _validate_stage_result(
                result,
                EvaluationStage.POLICY,
                {
                    EvaluationVerdict.PASSED,
                    EvaluationVerdict.POLICY_DENIED,
                    EvaluationVerdict.POLICY_INDETERMINATE,
                },
            )
            _validate_evidence(result, request, ())
            return result

        return await self._dispatcher.dispatch(access, operation)


class QualityEvaluatorGateway:
    """Authorizes quality calls and normalizes provider output into Core types."""

    def __init__(
        self,
        *,
        evaluators: QualityEvaluatorRegistry,
        capabilities_dispatcher: AuthorizedDispatcher[QualityEvaluatorCapabilities],
        evaluate_dispatcher: AuthorizedDispatcher[EvaluationStageResult],
        clock: Clock,
    ) -> None:
        self._evaluators = evaluators
        self._capabilities_dispatcher = capabilities_dispatcher
        self._evaluate_dispatcher = evaluate_dispatcher
        self._clock = clock

    async def capabilities(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> QualityEvaluatorCapabilities:
        evaluator = self._evaluators.get(request.quality_evaluator_id)
        access = _access_request(
            request,
            context,
            QUALITY_CAPABILITIES_ACTION,
            "quality_evaluator",
            request.quality_evaluator_id.value,
        )

        async def operation(call: AuthorizedCall) -> QualityEvaluatorCapabilities:
            _validate_grant_constraints(call)
            capabilities = await await_provider(
                evaluator.capabilities(call.context), call.context, self._clock
            )
            self._validate_capabilities(capabilities, request)
            return capabilities

        return await self._capabilities_dispatcher.dispatch(access, operation)

    async def evaluate(
        self, request: EvaluationRequest, context: RuntimeCallContext
    ) -> EvaluationStageResult:
        evaluator = self._evaluators.get(request.quality_evaluator_id)
        # Discovery is intentionally part of every protected evaluation.  It
        # prevents dispatch under stale assumptions about schema/profile support
        # and gives capabilities its own independently auditable action.
        capabilities = await self.capabilities(request, context)
        access = _access_request(
            request,
            context,
            QUALITY_EVALUATE_ACTION,
            "quality_evaluator",
            request.quality_evaluator_id.value,
        )

        async def operation(call: AuthorizedCall) -> EvaluationStageResult:
            _validate_grant_constraints(call)
            result = await await_provider(
                evaluator.evaluate(request, call.context), call.context, self._clock
            )
            _validate_stage_result(
                result,
                EvaluationStage.QUALITY,
                {EvaluationVerdict.PASSED, EvaluationVerdict.QUALITY_FAILED},
            )
            _validate_evidence(result, request, capabilities.evidence_kinds)
            return result

        return await self._evaluate_dispatcher.dispatch(access, operation)

    @staticmethod
    def _validate_capabilities(
        capabilities: QualityEvaluatorCapabilities, request: EvaluationRequest
    ) -> None:
        if capabilities.evaluator_id != request.quality_evaluator_id:
            _protocol_failure("quality_evaluator_identity_mismatch")
        if EVALUATION_CONTRACT_VERSION not in capabilities.request_versions:
            _protocol_failure("evaluation_request_version_unsupported")
        if EVALUATION_CONTRACT_VERSION not in capabilities.result_versions:
            _protocol_failure("evaluation_result_version_unsupported")
        if request.input_schema not in capabilities.input_schemas:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "evaluation_input_schema_unsupported",
                "quality evaluator does not support the Evaluation input schema",
            )
        profile_kind, separator, _ = request.quality_profile_ref.partition(":")
        if not separator or profile_kind not in capabilities.profile_kinds:
            raise core_error(
                ErrorCategory.UNSUPPORTED_CAPABILITY,
                "evaluation_quality_profile_unsupported",
                "quality evaluator does not support the external profile kind",
            )


def _access_request(
    request: EvaluationRequest,
    context: RuntimeCallContext,
    action: ActionRef,
    resource_kind: str,
    resource_id: str,
) -> AccessRequest:
    return AccessRequest(
        principal=RuntimePrincipal.core(
            CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
        ),
        action=action,
        resource=ResourceRef("core", resource_kind, ResourceId(resource_id)),
        scope=request.scope,
        context=context,
        constraints={
            "evaluation_id": request.evaluation_id.value,
            "request_version": request.contract_version,
            "schema": "/".join(request.input_schema.key),
            "request_fingerprint": request.fingerprint,
        },
    )


def _validate_stage_result(
    result: object,
    stage: EvaluationStage,
    verdicts: Collection[EvaluationVerdict],
) -> None:
    if not isinstance(result, EvaluationStageResult):
        _protocol_failure("malformed_evaluation_stage_result")
    if result.stage is not stage or result.verdict not in verdicts:
        _protocol_failure("invalid_evaluation_stage_result")


def _validate_evidence(
    result: EvaluationStageResult,
    request: EvaluationRequest,
    evidence_kinds: Collection[str],
) -> None:
    # Providers persist evidence before returning.  Core accepts only typed
    # references that remain inside the request's Scope; evidence content never
    # crosses this boundary.
    for reference in result.evidence_refs:
        if not reference.scope.is_equal_or_descendant_of(request.scope):
            _protocol_failure("evaluation_evidence_scope_mismatch")
        if evidence_kinds and reference.resource_type not in evidence_kinds:
            _protocol_failure("evaluation_evidence_kind_unsupported")


def _validate_grant_constraints(call: AuthorizedCall) -> None:
    # A grant may omit requested constraints (narrowing access), but it cannot
    # introduce a constraint or change an identity/fingerprint selected by the
    # caller.
    requested = call.request.constraints
    for key, value in call.grant.constraints.items():
        if key not in requested or requested[key] != value:
            raise core_error(
                ErrorCategory.DENIED,
                "invalid_grant",
                "Evaluation grant constraints do not match the request",
            )


def _protocol_failure(code: str) -> Never:
    raise core_error(
        ErrorCategory.PROTOCOL_FAILURE,
        code,
        "Evaluation provider returned an invalid contract",
    )
