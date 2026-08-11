# Evaluation Pipeline Code Review Guide

Status: Non-normative reviewer aid  
Reviewed baseline: CongeriesCore 0.2.0, RFC-0012

## Purpose

This guide explains the Evaluation implementation in review order. It does not
replace [RFC-0012](../rfcs/RFC-0012-evaluation.md); when prose here and the RFC
disagree, the RFC wins.

The central rule is simple: a schema failure, policy denial, or quality failure
must never become success later, and it must never unlock a dependent Workflow
node.

## One-Minute Mental Model

```text
EvaluationRequest
    |
    v
SchemaEvaluator -------- schema_failed / version error ------+
    | passed                                                |
    v                                                       |
EvaluationPolicyGateway -- denied / indeterminate / error ---+--> typed result
    | passed                                                |       |
    v                                                       |       v
QualityEvaluatorGateway --- quality_failed / error ----------+   AUDIT ACK
    | passed                                                        |
    +---------------------------------------------------------------+
                                                                    v
                                                        persist result reference
                                                                    |
                                                                    v
                                                           commit Checkpoint
                                                            /             \
                                                     passed                 other
                                                       |                      |
                                                mark completed          terminate Run
                                                       |                      |
                                                unlock dependents      unlock nothing
```

There are two different policy questions:

- `AuthorizationPolicy`: may this caller invoke this capability and resource?
- `EvaluationPolicy`: does the evaluated content comply with the selected policy?

An access denial is therefore an `ERROR` with `ErrorDetail(DENIED)`. A content
denial is `POLICY_DENIED`. Reviewers should treat any code that merges these two
meanings as a contract violation.

## Recommended Review Order

### 1. Public contracts and impossible states

Start with `src/congeries_core/evaluation/model.py`.

Check:

- every public type is frozen, slotted, and explicitly serialized;
- `from_data` requires the exact v1 field set;
- request values and constraints are copied into JSON-only immutable forms;
- stage verdicts are valid for their stage;
- stage results form the exact prefix `schema -> policy -> quality`;
- only the terminal stage may be non-successful;
- `PASSED` requires all three stages;
- error/timeout/cancellation carry `ErrorDetail`, while expected rejection does
  not; and
- result evidence is the ordered, de-duplicated union of stage evidence.

The important review idea is that the dataclasses reject impossible states at
construction time. The harness should not be the only place preserving these
rules because values can also arrive through deserialization or recovery.

### 2. Provider boundaries and authorization

Then review `src/congeries_core/evaluation/gateway.py`.

The registries own replaceable implementations. The gateways own access checks
and provider-contract validation. For each external call, verify this order:

1. resolve the declared policy or evaluator;
2. build an `AccessRequest` for the exact action and resource;
3. let `AuthorizedDispatcher` validate the grant and effective Scope;
4. await the provider with active deadline/cancellation control;
5. validate the returned stage, verdict, capabilities, and evidence references;
6. return only a normalized Core contract.

Quality evaluation deliberately performs authorized capability discovery before
the evaluate call. Core checks evaluator identity, request/result versions,
input-schema support, profile kind, evidence kind, and evidence Scope. Core does
not interpret scores, thresholds, rubrics, or the body behind an evidence
reference.

### 3. Deterministic harness and audit gate

Review `src/congeries_core/evaluation/harness.py` next.

The nested control flow is intentional: the policy stage exists only inside the
schema-passed branch, and quality exists only inside the policy-passed branch.
This shape makes accidental continuation after a failure difficult.

Pay particular attention to:

- a parent idempotency key is mandatory;
- request Scope can only narrow the parent Scope;
- the started event is best-effort observability;
- each external stage receives a deterministic child idempotency key containing
  the parent key, evaluation identity, contract version, request fingerprint,
  and stage;
- activity is checked after awaits so late success is discarded;
- external `CoreError` details are reduced to safe metadata; and
- `_finish` does not return the result until the required verdict AUDIT has been
  acknowledged. Audit failure invokes the existing failure handler and raises,
  so persistence cannot begin.

### 4. Event identity, redaction, and acknowledgement

Review these files together:

- `src/congeries_core/event/integration.py`
- `src/congeries_core/event/dispatcher.py`
- `src/congeries_core/event/model.py`
- `src/congeries_core/event/schema.py`

The started event contains references only and is `OBSERVABILITY`. The verdict
event is `AUDIT`; it contains safe verdict metadata, evidence references, and the
canonical result digest, but not input values, measurement values, evidence
bodies, or secret constraints.

The verdict event ID is derived from the result digest. Replaying the same
logical result therefore reuses the same event identity. The dispatcher rejects
reuse with different event data and required audit sinks must acknowledge before
publication returns. `RuntimeEvent.payload_digest` excludes allocation-time
fields such as timestamp and sequence so the logical audit payload stays stable
across replay.

### 5. Workflow validation and durable ordering

Review these files together:

- `src/congeries_core/workflow/model.py`
- `src/congeries_core/workflow/validation.py`
- `src/congeries_core/workflow/runtime.py`

Validation requires one input binding, a registered input schema, the fixed
`core/evaluation_result/1` output schema, checkpointing, idempotency, every
Evaluation action, and exact policy/evaluator resources. This prevents a node
from declaring permission for one resource while dispatching another.

The runtime success sequence must remain:

1. harness returns only after AUDIT acknowledgement;
2. persist the complete `EvaluationResult`;
3. create `NodeCheckpointState(SUCCEEDED, output_ref=...)`;
4. commit the Checkpoint and Run marker;
5. call `scheduler.mark_completed`;
6. allow dependent dispatch.

The non-success sequence must remain:

1. harness returns only after AUDIT acknowledgement;
2. persist the complete `EvaluationResult`;
3. create a typed non-success state with `error_ref`;
4. commit the Checkpoint and Run marker;
5. terminalize the Run;
6. never call `scheduler.mark_completed`.

The placement of `_stable_evaluation_failure` before scheduler construction is a
recovery gate. It loads and validates the committed `error_ref`, reconstructs
the terminal error, and exits before any evaluator or dependent node can be
dispatched.

## Failure-to-State Map

| Evaluation verdict | Node outcome | Workflow behavior |
| --- | --- | --- |
| `passed` | `SUCCEEDED` | Stable output may unlock dependents. |
| `policy_denied` | `DENIED` | Stable error boundary; terminate; unlock nothing. |
| `policy_indeterminate` | `DENIED` | Fail closed; terminate; unlock nothing. |
| `schema_failed` | `FAILED` | Stable error boundary; terminate; unlock nothing. |
| `quality_failed` | `FAILED` | Stable error boundary; terminate; unlock nothing. |
| `error` | `FAILED` | Preserve safe `ErrorDetail`; terminate. |
| `timed_out` | `TIMED_OUT` | Cancel active work, persist failure, terminate. |
| `cancelled` | `CANCELLED` | Cancel active work, persist failure, cancel Run. |

## Test Evidence Map

| Concern | Primary evidence |
| --- | --- |
| Strict versions, serialization, illegal combinations | `tests/test_evaluation.py::test_evaluation_contracts_are_strict_frozen_and_round_trip`; `test_evaluation_contract_rejects_illegal_combinations` |
| Fixed order and stable stage keys | `tests/test_evaluation.py::test_harness_passes_in_fixed_order_with_stable_stage_keys` |
| Replaceable evaluator normalization | `tests/test_evaluation.py::test_two_quality_evaluator_implementations_normalize_identically` |
| No failure laundering or later-stage call | `tests/test_evaluation.py::test_policy_non_success_short_circuits_quality`; `test_schema_failure_and_unknown_version_never_dispatch_external_stages`; `test_quality_failure_is_terminal_and_preserves_opaque_evidence` |
| Access denial remains distinct from content denial | `tests/test_evaluation.py::test_access_denial_is_error_not_content_policy_denial` |
| Provider validation and evidence Scope | `tests/test_evaluation.py::test_malformed_quality_result_becomes_typed_error`; `test_invalid_quality_evidence_is_a_provider_error` |
| Deadline, cancellation, and late-result discard | `tests/test_evaluation.py::test_cancelled_quality_task_is_cancelled_and_late_success_discarded`; `test_quality_deadline_cancels_provider_and_records_timeout` |
| Audit acknowledgement, deduplication, and redaction | `tests/test_events.py::test_evaluation_verdict_audit_is_acked_deduplicated_and_redacted`; `tests/test_evaluation.py::test_audit_failure_is_reported_and_no_result_is_returned` |
| Workflow success ordering | `tests/integration/test_workflow_runtime.py::test_evaluation_success_commits_before_unlocking_downstream` |
| Stable failure and dependency gate | `tests/integration/test_workflow_runtime.py::test_evaluation_failure_is_stable_and_never_unlocks_downstream` |
| Recovery from committed failure | `tests/integration/test_workflow_runtime.py::test_recovery_terminalizes_stable_evaluation_failure_without_redispatch` |
| Persistence/Checkpoint crash windows | `tests/integration/test_workflow_runtime.py::test_evaluation_output_crash_replays_same_result_and_persistence_key`; `test_evaluation_checkpoint_failure_never_unlocks_and_reuses_output_ref`; `test_recovery_skips_evaluation_after_checkpoint_before_mark_crash` |
| Exact public fixtures and catalogs | `tests/test_compatibility_fixtures.py::test_evaluation_v02_fixtures_round_trip_exactly`; `test_provider_action_and_core_event_catalog_v02_fixtures` |

## High-Risk Review Checklist

- [ ] No code path calls policy or quality after an earlier non-success verdict.
- [ ] No `AuthorizationPolicy` result is mapped to a content-policy verdict.
- [ ] No raw input, measurements, evidence body, or secret constraint enters an
      event, Checkpoint, or error metadata.
- [ ] No persistence call can happen before verdict AUDIT acknowledgement.
- [ ] No `mark_completed` call can happen before Checkpoint commit.
- [ ] No non-success outcome can enter the scheduler completed set.
- [ ] Recovery checks stable Evaluation failures before selecting a ready node.
- [ ] Replay preserves evaluation ID, request fingerprint, stage keys, output
      persistence key, and verdict event ID.
- [ ] Quality providers remain replaceable and Core contains no business score or
      threshold.
- [ ] Fixtures and action/event catalogs match their registries exactly.

## Deliberate v1 Limits

- One policy and one quality evaluator are selected per request.
- Stages are serial; there is no voting, weighting, fallback, or parallel score.
- Quality profiles and evidence storage are external and opaque to Core.
- EvaluationNode emits a typed `EvaluationResult`; it does not pass the original
  value through as node output.
- The direct Workflow runtime remains single-node-at-a-time and does not yet
  execute SkillNode, ToolNode, ContextNode, or custom node types.
