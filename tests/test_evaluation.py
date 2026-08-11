from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import timedelta

import pytest

from congeries_core.checkpoint import CheckpointReference
from congeries_core.evaluation import (
    EVALUATION_CONTRACT_VERSION,
    EvaluationHarness,
    EvaluationPolicyGateway,
    EvaluationPolicyRegistry,
    EvaluationRequest,
    EvaluationResult,
    EvaluationResultSchemaValidator,
    EvaluationStage,
    EvaluationStageResult,
    EvaluationVerdict,
    NullEvaluationEventPublisher,
    QualityEvaluatorCapabilities,
    QualityEvaluatorGateway,
    QualityEvaluatorRegistry,
    SchemaEvaluator,
    evaluation_actions,
    result_from_stages,
)
from congeries_core.policy.authorization import (
    ActionRegistry,
    AuthorizedDispatcher,
    ResourceRef,
)
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import EvaluationId, ProviderId, ResourceId
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef

from .provider_support import AuditRecorder, FailureRecorder, RecordingPolicy
from .support import (
    FixedClock,
    MatchingAllowPolicy,
    call_context,
    child_scope,
    root_scope,
)

SCHEMA = SchemaRef("test", "evaluation_input", "1")
EVALUATOR_ID = ProviderId("quality-1")


class InputValidator:
    def validate(self, value: object) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), str):
            raise ValueError("value must contain a string")


@dataclass(slots=True)
class FakeEvaluationPolicy:
    verdict: EvaluationVerdict = EvaluationVerdict.PASSED
    calls: list[object] = field(default_factory=list)

    async def evaluate(
        self, request: EvaluationRequest, context: object
    ) -> EvaluationStageResult:
        self.calls.append((request, context))
        return EvaluationStageResult(
            EvaluationStage.POLICY,
            self.verdict,
            "policy_result",
        )


@dataclass(slots=True)
class FakeQualityEvaluator:
    verdict: EvaluationVerdict = EvaluationVerdict.PASSED
    evaluator_id: ProviderId = EVALUATOR_ID
    evidence: tuple[CheckpointReference, ...] = ()
    calls: list[str] = field(default_factory=list)
    result_override: object | None = None
    wait_started: asyncio.Event | None = None
    wait_cancelled: asyncio.Event | None = None

    async def capabilities(self, context: object) -> QualityEvaluatorCapabilities:
        del context
        self.calls.append("capabilities")
        return QualityEvaluatorCapabilities(
            self.evaluator_id,
            "1",
            ("1",),
            ("1",),
            (SCHEMA,),
            ("external",),
            ("evaluation_evidence",),
        )

    async def evaluate(
        self, request: EvaluationRequest, context: object
    ) -> EvaluationStageResult:
        del request, context
        self.calls.append("evaluate")
        if self.wait_started is not None:
            self.wait_started.set()
            try:
                await asyncio.Future()
            finally:
                if self.wait_cancelled is not None:
                    self.wait_cancelled.set()
        if self.result_override is not None:
            return self.result_override  # type: ignore[return-value]
        return EvaluationStageResult(
            EvaluationStage.QUALITY,
            self.verdict,
            "quality_result",
            {"opaque": 1},
            self.evidence,
        )


class AlternateFakeQualityEvaluator(FakeQualityEvaluator):
    """A distinct replaceable implementation used by the shared contract test."""


@dataclass(slots=True)
class EvaluationEventRecorder:
    started: int = 0
    verdicts: list[EvaluationResult] = field(default_factory=list)
    fail_verdict: bool = False

    async def evaluation_started(self, request: object, context: object) -> None:
        del request, context
        self.started += 1

    async def evaluation_verdict_recorded(
        self, request: object, result: EvaluationResult, context: object
    ) -> None:
        del request, context
        if self.fail_verdict:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "evaluation_audit_failed",
                "audit unavailable",
            )
        self.verdicts.append(result)


def _request(value: object = None, *, schema: SchemaRef = SCHEMA) -> EvaluationRequest:
    return EvaluationRequest(
        EVALUATION_CONTRACT_VERSION,
        EvaluationId("evaluation-1"),
        {"value": "ok"} if value is None else value,  # type: ignore[arg-type]
        schema,
        "policy-1",
        EVALUATOR_ID,
        "external:profile-1",
        child_scope(),
        {"safe": True},
    )


def _dispatcher(policy: object | None = None) -> AuthorizedDispatcher[object]:
    return AuthorizedDispatcher(
        action_registry=ActionRegistry(evaluation_actions()),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=FixedClock(),
        policy=policy or MatchingAllowPolicy(),  # type: ignore[arg-type]
    )


def _harness(
    *,
    content_policy: FakeEvaluationPolicy | None = None,
    quality: FakeQualityEvaluator | None = None,
    access_policy: object | None = None,
    events: EvaluationEventRecorder | None = None,
    failures: FailureRecorder | None = None,
) -> tuple[
    EvaluationHarness,
    FakeEvaluationPolicy,
    FakeQualityEvaluator,
    EvaluationEventRecorder,
]:
    schemas = SchemaRegistry()
    schemas.register(SCHEMA, InputValidator())
    policy_provider = content_policy or FakeEvaluationPolicy()
    policies = EvaluationPolicyRegistry()
    policies.register("policy-1", policy_provider)
    quality_provider = quality or FakeQualityEvaluator()
    evaluators = QualityEvaluatorRegistry()
    evaluators.register(EVALUATOR_ID, quality_provider)
    dispatcher = _dispatcher(access_policy)
    event_recorder = events or EvaluationEventRecorder()
    return (
        EvaluationHarness(
            schema=SchemaEvaluator(schemas),
            policy=EvaluationPolicyGateway(
                policies=policies, dispatcher=dispatcher, clock=FixedClock()
            ),
            quality=QualityEvaluatorGateway(
                evaluators=evaluators,
                capabilities_dispatcher=dispatcher,
                evaluate_dispatcher=dispatcher,
                clock=FixedClock(),
            ),
            events=event_recorder,
            audit_failure_handler=failures or FailureRecorder(),
            clock=FixedClock(),
        ),
        policy_provider,
        quality_provider,
        event_recorder,
    )


def test_evaluation_contracts_are_strict_frozen_and_round_trip() -> None:
    reference = CheckpointReference(
        "evaluation_evidence",
        ResourceRef("test", "evidence", ResourceId("evidence-1")),
        child_scope(),
        "1",
    )
    request = _request()
    assert EvaluationRequest.from_data(request.to_data()) == request
    assert (
        request.fingerprint
        == EvaluationRequest.from_data(request.to_data()).fingerprint
    )
    with pytest.raises(FrozenInstanceError):
        request.policy_ref = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="fields"):
        EvaluationRequest.from_data({**request.to_data(), "extra": True})

    stages = (
        EvaluationStageResult(EvaluationStage.SCHEMA, EvaluationVerdict.PASSED, "ok"),
        EvaluationStageResult(EvaluationStage.POLICY, EvaluationVerdict.PASSED, "ok"),
        EvaluationStageResult(
            EvaluationStage.QUALITY,
            EvaluationVerdict.PASSED,
            "ok",
            {"score": 0.8},
            (reference,),
        ),
    )
    result = result_from_stages(request.evaluation_id, stages)
    assert EvaluationResult.from_data(result.to_data()) == result
    assert len(result.digest) == 64
    EvaluationResultSchemaValidator().validate(result.to_data())  # type: ignore[arg-type]

    capabilities = QualityEvaluatorCapabilities(
        EVALUATOR_ID,
        "1",
        ("1",),
        ("1",),
        (SCHEMA,),
        ("external",),
        ("evaluation_evidence",),
    )
    assert (
        QualityEvaluatorCapabilities.from_data(capabilities.to_data()) == capabilities
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EvaluationStageResult(
            EvaluationStage.SCHEMA, EvaluationVerdict.QUALITY_FAILED, "bad"
        ),
        lambda: EvaluationResult(
            "1",
            EvaluationId("evaluation-1"),
            EvaluationVerdict.PASSED,
            (
                EvaluationStageResult(
                    EvaluationStage.SCHEMA, EvaluationVerdict.PASSED, "ok"
                ),
            ),
            EvaluationStage.SCHEMA,
            (),
        ),
        lambda: EvaluationResult(
            "1",
            EvaluationId("evaluation-1"),
            EvaluationVerdict.ERROR,
            (
                EvaluationStageResult(
                    EvaluationStage.SCHEMA, EvaluationVerdict.ERROR, "bad"
                ),
            ),
            EvaluationStage.SCHEMA,
            (),
        ),
        lambda: QualityEvaluatorCapabilities(
            EVALUATOR_ID, "1", (), ("1",), (SCHEMA,), ("external",), ("evidence",)
        ),
    ],
)
def test_evaluation_contract_rejects_illegal_combinations(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


@pytest.mark.asyncio
async def test_harness_passes_in_fixed_order_with_stable_stage_keys() -> None:
    harness, policy, quality, events = _harness()
    first = await harness.evaluate(_request(), call_context())
    second = await harness.evaluate(_request(), call_context())

    assert first == second
    assert first.verdict is EvaluationVerdict.PASSED
    assert [item.stage for item in first.stage_results] == list(EvaluationStage)
    assert len(policy.calls) == 2
    assert quality.calls == ["capabilities", "evaluate"] * 2
    first_policy_context = policy.calls[0][1]
    second_policy_context = policy.calls[1][1]
    assert first_policy_context.idempotency_key == second_policy_context.idempotency_key
    assert first_policy_context.scope == _request().scope
    assert events.started == 2
    assert events.verdicts == [first, second]


@pytest.mark.asyncio
async def test_two_quality_evaluator_implementations_normalize_identically() -> None:
    first_harness, _, _, _ = _harness(quality=FakeQualityEvaluator())
    alternate_harness, _, _, _ = _harness(quality=AlternateFakeQualityEvaluator())
    first = await first_harness.evaluate(_request(), call_context())
    alternate = await alternate_harness.evaluate(_request(), call_context())
    assert first.to_data() == alternate.to_data()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [EvaluationVerdict.POLICY_DENIED, EvaluationVerdict.POLICY_INDETERMINATE],
)
async def test_policy_non_success_short_circuits_quality(
    verdict: EvaluationVerdict,
) -> None:
    harness, _, quality, _ = _harness(content_policy=FakeEvaluationPolicy(verdict))
    result = await harness.evaluate(_request(), call_context())
    assert result.verdict is verdict
    assert tuple(item.stage for item in result.stage_results) == (
        EvaluationStage.SCHEMA,
        EvaluationStage.POLICY,
    )
    assert quality.calls == []


@pytest.mark.asyncio
async def test_schema_failure_and_unknown_version_never_dispatch_external_stages() -> (
    None
):
    harness, policy, quality, _ = _harness()
    failed = await harness.evaluate(_request({"value": 42}), call_context())
    assert failed.verdict is EvaluationVerdict.SCHEMA_FAILED
    assert policy.calls == [] and quality.calls == []

    missing = await harness.evaluate(
        _request(schema=SchemaRef("test", "missing", "9")), call_context()
    )
    assert missing.verdict is EvaluationVerdict.ERROR
    assert missing.error is not None
    assert missing.error.category is ErrorCategory.VERSION_MISMATCH
    assert policy.calls == [] and quality.calls == []


@pytest.mark.asyncio
async def test_quality_failure_is_terminal_and_preserves_opaque_evidence() -> None:
    evidence = CheckpointReference(
        "evaluation_evidence",
        ResourceRef("test", "evidence", ResourceId("evidence-1")),
        child_scope(),
        "1",
    )
    harness, _, quality, _ = _harness(
        quality=FakeQualityEvaluator(
            EvaluationVerdict.QUALITY_FAILED, evidence=(evidence,)
        )
    )
    result = await harness.evaluate(_request(), call_context())
    assert result.verdict is EvaluationVerdict.QUALITY_FAILED
    assert result.evidence_refs == (evidence,)
    assert quality.calls == ["capabilities", "evaluate"]


@pytest.mark.asyncio
async def test_access_denial_is_error_not_content_policy_denial() -> None:
    access = RecordingPolicy(denied_actions={"evaluation.policy.evaluate"})
    harness, _, quality, _ = _harness(access_policy=access)
    result = await harness.evaluate(_request(), call_context())
    assert result.verdict is EvaluationVerdict.ERROR
    assert result.error is not None
    assert result.error.category is ErrorCategory.DENIED
    assert quality.calls == []


@pytest.mark.asyncio
async def test_audit_failure_is_reported_and_no_result_is_returned() -> None:
    events = EvaluationEventRecorder(fail_verdict=True)
    failures = FailureRecorder()
    harness, _, _, _ = _harness(events=events, failures=failures)
    with pytest.raises(CoreError) as error:
        await harness.evaluate(_request(), call_context())
    assert error.value.detail.code == "evaluation_audit_failed"
    assert [item.code for item in failures.errors] == ["evaluation_audit_failed"]
    assert events.verdicts == []


@pytest.mark.asyncio
async def test_malformed_quality_result_becomes_typed_error() -> None:
    quality = FakeQualityEvaluator(result_override={"verdict": "passed"})
    harness, _, _, _ = _harness(quality=quality)
    result = await harness.evaluate(_request(), call_context())
    assert result.verdict is EvaluationVerdict.ERROR
    assert result.error is not None
    assert result.error.code == "malformed_evaluation_stage_result"


@pytest.mark.asyncio
async def test_cancelled_quality_task_is_cancelled_and_late_success_discarded() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    quality = FakeQualityEvaluator(wait_started=started, wait_cancelled=cancelled)
    harness, _, _, events = _harness(quality=quality)
    context = call_context()
    task = asyncio.create_task(harness.evaluate(_request(), context))
    await started.wait()
    context.cancellation.cancel()
    result = await task
    assert result.verdict is EvaluationVerdict.CANCELLED
    assert cancelled.is_set()
    assert events.verdicts[-1].verdict is EvaluationVerdict.CANCELLED


@pytest.mark.asyncio
async def test_quality_deadline_cancels_provider_and_records_timeout() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    quality = FakeQualityEvaluator(wait_started=started, wait_cancelled=cancelled)
    harness, _, _, events = _harness(quality=quality)
    context = replace(
        call_context(),
        deadline=Deadline(FixedClock().now() + timedelta(milliseconds=1)),
    )
    result = await harness.evaluate(_request(), context)
    assert started.is_set() and cancelled.is_set()
    assert result.verdict is EvaluationVerdict.TIMED_OUT
    assert events.verdicts[-1].verdict is EvaluationVerdict.TIMED_OUT


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["scope", "kind"])
async def test_invalid_quality_evidence_is_a_provider_error(invalid: str) -> None:
    scope = child_scope()
    reference = CheckpointReference(
        "wrong_kind" if invalid == "kind" else "evaluation_evidence",
        ResourceRef("test", "evidence", ResourceId("bad-evidence")),
        (
            ScopeRef.core(CoreScopeKind.RUN, "other-scope", root_scope())
            if invalid == "scope"
            else scope
        ),
        "1",
    )
    quality = FakeQualityEvaluator(evidence=(reference,))
    harness, _, _, _ = _harness(quality=quality)
    result = await harness.evaluate(_request(), call_context())
    assert result.verdict is EvaluationVerdict.ERROR
    assert result.error is not None
    assert result.error.code in {
        "evaluation_evidence_scope_mismatch",
        "evaluation_evidence_kind_unsupported",
    }


def test_registries_reject_duplicate_and_missing_providers() -> None:
    policies = EvaluationPolicyRegistry()
    policies.register("policy-1", FakeEvaluationPolicy())
    with pytest.raises(CoreError) as duplicate:
        policies.register("policy-1", FakeEvaluationPolicy())
    assert duplicate.value.detail.code == "evaluation_policy_already_registered"
    with pytest.raises(CoreError) as missing:
        policies.get("missing")
    assert missing.value.detail.code == "evaluation_policy_not_registered"

    evaluators = QualityEvaluatorRegistry()
    evaluators.register(EVALUATOR_ID, FakeQualityEvaluator())
    with pytest.raises(CoreError) as duplicate_evaluator:
        evaluators.register(EVALUATOR_ID, FakeQualityEvaluator())
    assert (
        duplicate_evaluator.value.detail.code == "quality_evaluator_already_registered"
    )
    with pytest.raises(CoreError) as missing_evaluator:
        evaluators.get(ProviderId("missing"))
    assert missing_evaluator.value.detail.code == "quality_evaluator_not_registered"


@pytest.mark.asyncio
async def test_null_event_publisher_is_a_safe_noop() -> None:
    publisher = NullEvaluationEventPublisher()
    result = result_from_stages(
        EvaluationId("evaluation-1"),
        (
            EvaluationStageResult(
                EvaluationStage.SCHEMA, EvaluationVerdict.SCHEMA_FAILED, "bad"
            ),
        ),
    )
    await publisher.evaluation_started(_request(), call_context())
    await publisher.evaluation_verdict_recorded(_request(), result, call_context())
