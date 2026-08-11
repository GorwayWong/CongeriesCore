# RFC-0012: Evaluation

- ID: RFC-0012
- Title: Evaluation
- Status: Implemented
- Target Version: 0.2.0
- Owner: Congeries Core Maintainers
- Created: 2026-08-11
- Updated: 2026-08-11
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [Tasks](../../tasks.md), [RFC-0003](RFC-0003-workflow.md), [RFC-0008](RFC-0008-scope-authorization.md), [RFC-0010](RFC-0010-runtime-events.md), [RFC-0011](RFC-0011-checkpoint-recovery.md)
- Supersedes: None

## 1. Scope

This RFC defines the provider-neutral Evaluation contract and runtime semantics.
Evaluation composes schema validation, content policy evaluation, and one
replaceable quality evaluator in that fixed order. Core defines orchestration,
typed outcomes, authorization, audit, idempotency, and recovery boundaries; it
does not define business rubrics, scores, thresholds, evidence storage, or
vendor-specific behavior.

### 1.1 Plain-language model (non-normative)

An Evaluation is three gates in a row:

1. **Schema:** Is the value shaped like the contract says it should be?
2. **Evaluation policy:** Is the content acceptable under the selected content
   policy? This answers a content question; it is not access authorization.
3. **Quality:** Does the selected external evaluator accept the value under the
   referenced quality profile?

The first closed gate ends the Evaluation. Core never asks a later gate to
override an earlier failure, so a bad schema or denied policy cannot be "washed"
into success by a quality score. Once the final verdict exists, Core first gets
an audit receipt, then stores the typed result, then commits a Checkpoint. Only a
committed `passed` result opens downstream Workflow nodes. A committed failure is
also a real recovery boundary: after a restart, Core reports that failure again
instead of calling the evaluators a second time.

## 2. Public Contract

### 2.1 Request

`EvaluationRequest` version 1 contains:

| Field | Meaning |
| --- | --- |
| `contract_version` | Evaluation request contract version. |
| `evaluation_id` | Stable identity retained across retry and recovery. |
| `value` | JSON value being evaluated. |
| `input_schema` | Versioned `SchemaRef` applied before external dispatch. |
| `policy_ref` | Opaque reference selecting an `EvaluationPolicy`. |
| `quality_evaluator_id` | The single quality evaluator selected for the request. |
| `quality_profile_ref` | Opaque external profile reference; Core does not interpret it. |
| `scope` | Scope in which the value and evidence may be evaluated. |
| `constraints` | JSON constraints passed without business interpretation. |

`RuntimeCallContext` is transported separately and remains the authority for
run identity, deadline, cancellation, trace, Scope, and idempotency.

### 2.2 Stages and verdicts

`EvaluationStage` is exactly `schema`, `policy`, or `quality`.

`EvaluationVerdict` is exactly `passed`, `schema_failed`, `policy_denied`,
`policy_indeterminate`, `quality_failed`, `error`, `timed_out`, or `cancelled`.

`EvaluationStageResult` contains its stage, verdict, a safe reason code,
optional opaque JSON-scalar measurements, and typed evidence references.
Evidence references are existing Scope-bearing `CheckpointReference` values.

`EvaluationResult` version 1 contains the evaluation identity, final verdict,
the ordered results of stages that actually ran, the terminal stage, the union
of evidence references, and an optional `ErrorDetail`.

The following invariants are normative:

- `passed` contains exactly three successful results in schema, policy, quality
  order.
- A non-success result contains only the terminal stage and its predecessors.
  A later stage MUST NOT run or appear.
- Expected schema, policy, and quality rejection uses its typed verdict and a
  safe reason code. It MUST NOT be represented as a provider error.
- `error`, `timed_out`, and `cancelled` MUST carry an `ErrorDetail`. Other
  verdicts MUST NOT carry one.
- Results, events, and Checkpoints contain evidence references, never raw
  evidence bodies.

### 2.3 Quality capabilities

`QualityEvaluatorCapabilities` identifies the evaluator and declares supported
request and result versions, exact input schemas, profile kinds, and evidence
kinds. Capabilities are validated before evaluation. They do not contain a
rubric, threshold, or scoring algorithm.

All Evaluation nodes publish `EvaluationResult` against the fixed output schema
`core/evaluation_result/1`.

## 3. Schema Evaluation

Schema evaluation is pure and reuses `SchemaRegistry`. An unregistered schema
or version fails before policy or quality dispatch with a
`VERSION_MISMATCH` error. A value that does not satisfy a registered schema
produces `schema_failed` and immediately terminates evaluation.

## 4. Evaluation Policy

`EvaluationPolicy.evaluate(request, context)` determines whether evaluated
content complies with a selected content policy. It is distinct from
`AuthorizationPolicy`, which controls whether the runtime may invoke the
capability.

Policy invocation is guarded by `AuthorizedDispatcher` using action
`core.evaluation.policy.evaluate`. A dispatch authorization denial is an
`ErrorDetail` with category `DENIED` and final verdict `error`; it MUST NOT be
reported as `policy_denied`. Both policy `denied` and `indeterminate` are
fail-closed terminal outcomes.

## 5. Quality Evaluator

`QualityEvaluator` is a replaceable provider with `capabilities` and `evaluate`
operations. Both accept `RuntimeCallContext` and are guarded by:

- `core.evaluation.quality.capabilities`
- `core.evaluation.quality.evaluate`

The gateway validates capabilities, request and result versions, evaluation
identity, effective Scope, grant constraints, and evidence references. A
provider MUST return already-persisted evidence references. Core provides no
mandatory evidence store and does not interpret measurements, profiles,
rubrics, scores, or thresholds.

## 6. Deterministic Harness

The Evaluation harness executes schema, policy, then quality, and stops at the
first non-success result. Each request selects exactly one policy and one
quality evaluator. Parallel scoring, voting, weighting, fallback, and business
rules are outside Core.

Each external stage receives a context narrowed from its parent. The harness
propagates Scope, deadline, cancellation, and trace. A stage idempotency key is
derived deterministically from the parent key, evaluation identity, contract
version, request fingerprint, and stage. The request fingerprint covers the
canonical input digest, schema, policy, evaluator, profile, Scope, and
constraints.

The harness checks activity before and after every external await. A timeout or
cancellation cancels the provider task, discards any late result, and cannot
produce a successful verdict.

## 7. Events and Audit

Evaluation adds two version 1 Runtime Events:

| Event | Delivery | Payload |
| --- | --- | --- |
| `core.evaluation.started` | `OBSERVABILITY` | Evaluation identity and safe contract references. |
| `core.evaluation.verdict_recorded` | `AUDIT` | Evaluation identity, schema/evaluator/profile references, verdict, terminal stage, safe reason or error code, evidence references, and canonical result digest. |

The verdict event uses the [Runtime Event](RFC-0010-runtime-events.md) envelope,
authorization, redaction, acknowledgement, and at-least-once delivery contract.
Its event identity is deterministic across replay. A required audit sink MUST
acknowledge the verdict before the result is eligible for persistence or a
stable Checkpoint. Audit failure invokes the existing audit-failure handler and
prevents a committable result from being returned.

Raw input, measurements, evidence bodies, and secret constraints MUST NOT
appear in event payloads, Checkpoints, or error metadata.

## 8. EvaluationNode and Durable Boundaries

`EvaluationNodeConfig` selects a policy, quality evaluator, and quality profile.
Validation requires exactly one input binding, a registered input schema, the
fixed `core/evaluation_result/1` output schema, `checkpoint=true`,
`idempotency_required=true`, and all Evaluation actions in the node permission
set.

After the harness returns, the Workflow runtime persists the complete typed
result through `NodeOutputPersistence`.

Successful execution orders effects as follows:

1. verdict AUDIT acknowledgement;
2. result persistence;
3. stable `NodeCheckpointState` with `outcome=SUCCEEDED` and `output_ref`;
4. Checkpoint compare-and-swap commit;
5. `scheduler.mark_completed` and downstream unlock.

Non-success execution orders effects as follows:

1. verdict AUDIT acknowledgement;
2. result persistence;
3. stable node state with `error_ref` and `FAILED`, `DENIED`, `TIMED_OUT`, or
   `CANCELLED` outcome;
4. Checkpoint compare-and-swap commit;
5. terminal Run transition without calling `scheduler.mark_completed`.

`policy_denied` and `policy_indeterminate` map to `DENIED`; schema failure,
quality failure, and ordinary error map to `FAILED`; timeout and cancellation
retain their corresponding outcomes.

Recovery skips a stable successful Evaluation node. When a stable non-success
Evaluation node is present, recovery loads `EvaluationResult` from `error_ref`
and terminalizes the Run before scheduler dispatch. It MUST NOT invoke any
evaluator again or unlock a dependent node. Interrupted execution before a
stable Checkpoint replays with the same evaluation identity, fingerprint,
stage keys, output persistence key, and audit event identity.

## 9. Failure Semantics

Malformed provider output, capability mismatch, identity mismatch, Scope or
evidence violation, and access authorization denial become typed `error`
results. Unknown schema versions fail before dispatch. Schema failure, policy
denial or indeterminacy, and quality failure can never be converted to success
by a later stage or execution policy.

Persistence or Checkpoint failure leaves the node unstable. A committed
non-success node boundary is authoritative during recovery even if the process
failed before the Run terminal transition.

## 10. Conformance

An implementation conforms to this RFC when it:

- implements the strict versioned contracts and invariants above;
- authorizes every external policy and quality call with a propagated
  `RuntimeCallContext`;
- short-circuits deterministically and discards late provider results;
- obtains reliable, redacted AUDIT acknowledgement before persistence;
- persists both successful and non-successful Evaluation node boundaries;
- unlocks dependencies only for a committed successful boundary; and
- proves replay, idempotency, recovery, exact serialization, and redaction with
  compatibility, unit, and Workflow integration tests.

## 11. Implementation Review Map (non-normative)

The public contract above remains the source of truth. Reviewers can follow its
reference implementation, effect ordering, failure gates, and test evidence in
the [Evaluation Pipeline Code Review Guide](../reviews/evaluation-pipeline-code-review.md).
