"""Strict public contracts for deterministic Evaluation.

The model layer deliberately owns the legal state space.  Callers, providers,
deserializers, and recovery code all construct these same frozen values, so an
invalid stage order or a "passed" result with missing stages is rejected at the
boundary instead of relying on the harness to behave correctly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from congeries_core.checkpoint.model import CheckpointReference
from congeries_core.runtime.errors import ErrorDetail, JsonScalar
from congeries_core.runtime.ids import EvaluationId, ProviderId
from congeries_core.runtime.json_types import JsonValue, as_array, as_object
from congeries_core.runtime.schema import SchemaRef
from congeries_core.runtime.scope import ScopeRef

EVALUATION_CONTRACT_VERSION = "1"
EVALUATION_RESULT_SCHEMA = SchemaRef("core", "evaluation_result", "1")


class EvaluationStage(StrEnum):
    SCHEMA = "schema"
    POLICY = "policy"
    QUALITY = "quality"


class EvaluationVerdict(StrEnum):
    PASSED = "passed"
    SCHEMA_FAILED = "schema_failed"
    POLICY_DENIED = "policy_denied"
    POLICY_INDETERMINATE = "policy_indeterminate"
    QUALITY_FAILED = "quality_failed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_EXPECTED_FAILURES = {
    EvaluationVerdict.SCHEMA_FAILED,
    EvaluationVerdict.POLICY_DENIED,
    EvaluationVerdict.POLICY_INDETERMINATE,
    EvaluationVerdict.QUALITY_FAILED,
}
_ERROR_VERDICTS = {
    EvaluationVerdict.ERROR,
    EvaluationVerdict.TIMED_OUT,
    EvaluationVerdict.CANCELLED,
}
# Keep stage/verdict legality as data rather than scattered conditionals.  This
# table is the first defense against a provider returning (for example) a
# quality failure from the policy stage.
_ALLOWED_STAGE_VERDICTS = {
    EvaluationStage.SCHEMA: {
        EvaluationVerdict.PASSED,
        EvaluationVerdict.SCHEMA_FAILED,
        *_ERROR_VERDICTS,
    },
    EvaluationStage.POLICY: {
        EvaluationVerdict.PASSED,
        EvaluationVerdict.POLICY_DENIED,
        EvaluationVerdict.POLICY_INDETERMINATE,
        *_ERROR_VERDICTS,
    },
    EvaluationStage.QUALITY: {
        EvaluationVerdict.PASSED,
        EvaluationVerdict.QUALITY_FAILED,
        *_ERROR_VERDICTS,
    },
}


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """One stable, replayable request; runtime control travels in context."""

    contract_version: str
    evaluation_id: EvaluationId
    value: JsonValue
    input_schema: SchemaRef
    policy_ref: str
    quality_evaluator_id: ProviderId
    quality_profile_ref: str
    scope: ScopeRef
    constraints: Mapping[str, JsonValue] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        if self.contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError("unsupported Evaluation request contract version")
        _require_text(self.policy_ref, "policy_ref")
        _require_text(self.quality_profile_ref, "quality_profile_ref")
        # Defensive JSON copies prevent a caller from mutating a list/dict after
        # the fingerprint or idempotency material has been calculated.
        object.__setattr__(self, "value", _json_copy(self.value))
        for key in self.constraints:
            _require_text(key, "constraint key")
        object.__setattr__(
            self,
            "constraints",
            MappingProxyType(
                {key: _json_copy(value) for key, value in self.constraints.items()}
            ),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "evaluation_id": self.evaluation_id.value,
            "value": self.value,
            "input_schema": self.input_schema.to_data(),
            "policy_ref": self.policy_ref,
            "quality_evaluator_id": self.quality_evaluator_id.value,
            "quality_profile_ref": self.quality_profile_ref,
            "scope": self.scope.to_data(),
            "constraints": dict(self.constraints),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> EvaluationRequest:
        _require_keys(
            data,
            {
                "contract_version",
                "evaluation_id",
                "value",
                "input_schema",
                "policy_ref",
                "quality_evaluator_id",
                "quality_profile_ref",
                "scope",
                "constraints",
            },
            "Evaluation request",
        )
        return cls(
            contract_version=str(data["contract_version"]),
            evaluation_id=EvaluationId(str(data["evaluation_id"])),
            value=_json_copy(data["value"]),
            input_schema=SchemaRef.from_data(
                as_object(data["input_schema"], "Evaluation input schema")
            ),
            policy_ref=str(data["policy_ref"]),
            quality_evaluator_id=ProviderId(str(data["quality_evaluator_id"])),
            quality_profile_ref=str(data["quality_profile_ref"]),
            scope=ScopeRef.from_data(as_object(data["scope"], "Evaluation scope")),
            constraints={
                key: _json_copy(value)
                for key, value in as_object(
                    data["constraints"], "Evaluation constraints"
                ).items()
            },
        )

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.to_data())


@dataclass(frozen=True, slots=True)
class EvaluationStageResult:
    """Normalized outcome of exactly one stage, without raw evidence bodies."""

    stage: EvaluationStage
    verdict: EvaluationVerdict
    reason_code: str
    measurements: Mapping[str, JsonScalar] = field(default_factory=lambda: {})
    evidence_refs: tuple[CheckpointReference, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in _ALLOWED_STAGE_VERDICTS[self.stage]:
            raise ValueError("verdict is not valid for Evaluation stage")
        _require_text(self.reason_code, "reason_code")
        checked: dict[str, JsonScalar] = {}
        for key, value in self.measurements.items():
            _require_text(key, "measurement key")
            checked[key] = _json_scalar(value)
        object.__setattr__(self, "measurements", MappingProxyType(checked))
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")

    def to_data(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "verdict": self.verdict.value,
            "reason_code": self.reason_code,
            "measurements": dict(self.measurements),
            "evidence_refs": [reference.to_data() for reference in self.evidence_refs],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> EvaluationStageResult:
        _require_keys(
            data,
            {"stage", "verdict", "reason_code", "measurements", "evidence_refs"},
            "Evaluation stage result",
        )
        raw_measurements = as_object(data["measurements"], "measurements")
        return cls(
            stage=EvaluationStage(str(data["stage"])),
            verdict=EvaluationVerdict(str(data["verdict"])),
            reason_code=str(data["reason_code"]),
            measurements={
                key: _json_scalar(value) for key, value in raw_measurements.items()
            },
            evidence_refs=tuple(
                CheckpointReference.from_data(as_object(item, "evidence reference"))
                for item in as_array(data["evidence_refs"], "evidence references")
            ),
        )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Terminal Evaluation record whose constructor enforces fail-fast history."""

    contract_version: str
    evaluation_id: EvaluationId
    verdict: EvaluationVerdict
    stage_results: tuple[EvaluationStageResult, ...]
    terminal_stage: EvaluationStage
    evidence_refs: tuple[CheckpointReference, ...]
    error: ErrorDetail | None = None

    def __post_init__(self) -> None:
        if self.contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError("unsupported Evaluation result contract version")
        if not self.stage_results:
            raise ValueError("Evaluation result requires at least one stage result")
        # A result is a prefix, never an arbitrary subset: schema; schema/policy;
        # or schema/policy/quality.  This makes "a later stage ran after failure"
        # impossible to serialize as a valid result.
        stages = tuple(result.stage for result in self.stage_results)
        expected_prefix = (
            EvaluationStage.SCHEMA,
            EvaluationStage.POLICY,
            EvaluationStage.QUALITY,
        )[: len(stages)]
        if stages != expected_prefix or self.terminal_stage is not stages[-1]:
            raise ValueError("Evaluation stages must be an ordered terminal prefix")
        if any(
            result.verdict is not EvaluationVerdict.PASSED
            for result in self.stage_results[:-1]
        ):
            raise ValueError("only the terminal Evaluation stage may be non-successful")
        terminal_verdict = self.stage_results[-1].verdict
        if terminal_verdict is not self.verdict:
            raise ValueError("final verdict must match the terminal stage verdict")
        if self.verdict is EvaluationVerdict.PASSED and len(stages) != 3:
            raise ValueError("passed Evaluation requires all three stages")
        if self.verdict in _ERROR_VERDICTS and self.error is None:
            raise ValueError("error, timeout, and cancellation require ErrorDetail")
        if self.verdict not in _ERROR_VERDICTS and self.error is not None:
            raise ValueError("expected Evaluation verdicts must not carry ErrorDetail")
        if (
            self.verdict is EvaluationVerdict.TIMED_OUT
            and self.error is not None
            and self.error.category.value != "timeout"
        ):
            raise ValueError("timed_out verdict requires a timeout error category")
        if (
            self.verdict is EvaluationVerdict.CANCELLED
            and self.error is not None
            and self.error.category.value != "cancelled"
        ):
            raise ValueError("cancelled verdict requires a cancelled error category")
        # The top-level evidence list is derived, not independently selectable.
        # Recovery and audit consumers can therefore trust it to represent the
        # ordered union of every stage that actually ran.
        stage_evidence = tuple(
            reference
            for result in self.stage_results
            for reference in result.evidence_refs
        )
        if self.evidence_refs != tuple(dict.fromkeys(stage_evidence)):
            raise ValueError("result evidence must be the ordered stage evidence union")

    def to_data(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "evaluation_id": self.evaluation_id.value,
            "verdict": self.verdict.value,
            "stage_results": [result.to_data() for result in self.stage_results],
            "terminal_stage": self.terminal_stage.value,
            "evidence_refs": [reference.to_data() for reference in self.evidence_refs],
            "error": self.error.to_data() if self.error else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> EvaluationResult:
        _require_keys(
            data,
            {
                "contract_version",
                "evaluation_id",
                "verdict",
                "stage_results",
                "terminal_stage",
                "evidence_refs",
                "error",
            },
            "Evaluation result",
        )
        raw_error = data["error"]
        return cls(
            contract_version=str(data["contract_version"]),
            evaluation_id=EvaluationId(str(data["evaluation_id"])),
            verdict=EvaluationVerdict(str(data["verdict"])),
            stage_results=tuple(
                EvaluationStageResult.from_data(as_object(item, "stage result"))
                for item in as_array(data["stage_results"], "stage results")
            ),
            terminal_stage=EvaluationStage(str(data["terminal_stage"])),
            evidence_refs=tuple(
                CheckpointReference.from_data(as_object(item, "evidence reference"))
                for item in as_array(data["evidence_refs"], "evidence references")
            ),
            error=(
                ErrorDetail.from_data(as_object(raw_error, "Evaluation error"))
                if raw_error is not None
                else None
            ),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_data())


@dataclass(frozen=True, slots=True)
class QualityEvaluatorCapabilities:
    """Provider compatibility facts; no rubric or business threshold belongs here."""

    evaluator_id: ProviderId
    contract_version: str
    request_versions: tuple[str, ...]
    result_versions: tuple[str, ...]
    input_schemas: tuple[SchemaRef, ...]
    profile_kinds: tuple[str, ...]
    evidence_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError("unsupported capabilities contract version")
        for values, name in (
            (self.request_versions, "request_versions"),
            (self.result_versions, "result_versions"),
            (self.profile_kinds, "profile_kinds"),
            (self.evidence_kinds, "evidence_kinds"),
        ):
            if not values or len(set(values)) != len(values):
                raise ValueError(f"{name} must be non-empty and unique")
            for value in values:
                _require_text(value, name)
        if not self.input_schemas or len(set(self.input_schemas)) != len(
            self.input_schemas
        ):
            raise ValueError("input_schemas must be non-empty and unique")

    def to_data(self) -> dict[str, object]:
        return {
            "evaluator_id": self.evaluator_id.value,
            "contract_version": self.contract_version,
            "request_versions": list(self.request_versions),
            "result_versions": list(self.result_versions),
            "input_schemas": [schema.to_data() for schema in self.input_schemas],
            "profile_kinds": list(self.profile_kinds),
            "evidence_kinds": list(self.evidence_kinds),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> QualityEvaluatorCapabilities:
        _require_keys(
            data,
            {
                "evaluator_id",
                "contract_version",
                "request_versions",
                "result_versions",
                "input_schemas",
                "profile_kinds",
                "evidence_kinds",
            },
            "quality evaluator capabilities",
        )
        return cls(
            evaluator_id=ProviderId(str(data["evaluator_id"])),
            contract_version=str(data["contract_version"]),
            request_versions=tuple(
                str(item)
                for item in as_array(data["request_versions"], "request versions")
            ),
            result_versions=tuple(
                str(item)
                for item in as_array(data["result_versions"], "result versions")
            ),
            input_schemas=tuple(
                SchemaRef.from_data(as_object(item, "input schema"))
                for item in as_array(data["input_schemas"], "input schemas")
            ),
            profile_kinds=tuple(
                str(item) for item in as_array(data["profile_kinds"], "profile kinds")
            ),
            evidence_kinds=tuple(
                str(item) for item in as_array(data["evidence_kinds"], "evidence kinds")
            ),
        )


class EvaluationResultSchemaValidator:
    def validate(self, value: JsonValue) -> None:
        EvaluationResult.from_data(as_object(value, "Evaluation result"))


def result_from_stages(
    evaluation_id: EvaluationId,
    stages: tuple[EvaluationStageResult, ...],
    *,
    error: ErrorDetail | None = None,
) -> EvaluationResult:
    terminal = stages[-1]
    evidence = tuple(
        dict.fromkeys(
            reference for stage in stages for reference in stage.evidence_refs
        )
    )
    return EvaluationResult(
        contract_version=EVALUATION_CONTRACT_VERSION,
        evaluation_id=evaluation_id,
        verdict=terminal.verdict,
        stage_results=stages,
        terminal_stage=terminal.stage,
        evidence_refs=evidence,
        error=error,
    )


def canonical_digest(value: object) -> str:
    """Hash canonical JSON so replay identities do not depend on dict ordering."""

    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_copy(value: object) -> JsonValue:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        decoded: object = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be valid JSON") from error
    return decoded  # type: ignore[return-value]


def _json_scalar(value: object) -> JsonScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError("measurements must contain JSON scalar values")


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")


def _require_keys(data: dict[str, object], expected: set[str], name: str) -> None:
    if set(data) != expected:
        raise ValueError(f"{name} fields do not match the versioned contract")
