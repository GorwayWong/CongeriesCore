# RFC-0004: Execution Run Lifecycle

- ID: RFC-0004
- Title: Execution Run Lifecycle
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0006](../adrs/ADR-0006-run-and-session.md), [RFC-0011](RFC-0011-checkpoint-recovery.md)
- Supersedes: None

## 1. Scope

This RFC defines the common Run envelope, AgentRun and WorkflowRun
specialization, lightweight SessionRef, state machine, attempts, concurrency,
and lifecycle outcomes.

## 2. Run Envelope

Run contains:

| Field | Meaning |
| --- | --- |
| `run_id` | Stable execution identity |
| `kind` | AGENT or WORKFLOW |
| `definition_id` | AgentSpec or Workflow definition identity |
| `root_run_id` | Root of the execution tree; equal to run_id for a root Run |
| `parent_run_id` | Immediate parent, absent for a root Run |
| `workspace_id` | Durable state boundary |
| `session_ref` | Optional cross-Run correlation and isolation reference |
| `scope` | Effective ScopeRef |
| `status` | Current RunStatus |
| `attempt` | Positive execution attempt number |
| `state_version` | Monotonic concurrency version |
| `created_at` | Creation timestamp |
| `started_at` | First execution timestamp, if started |
| `updated_at` | Last state change timestamp |
| `ended_at` | Terminal timestamp, if terminal |
| `error_summary` | Structured terminal or latest attempt error, if present |

AgentRun adds `agent_id` and model binding reference. WorkflowRun adds
`workflow_id`, graph version, and latest checkpoint reference.

Either kind may be a root Run. Workflow AgentNode creates a child AgentRun.

## 3. SessionRef

SessionRef contains a namespace and session identity. Associated Session state
contains OPEN or CLOSED, creation time, and optional close time.

SessionRef correlates related Runs and contributes to Scope. Core Session state
does not contain messages, participants, user profiles, or business data.

A CLOSED Session accepts no new Run association. Existing Runs and history
remain addressable according to authorization and retention policy.

## 4. Run Status

Non-terminal states:

- CREATED
- STARTING
- CONTEXT_LOADING
- RUNNING
- WAITING_APPROVAL
- PAUSED
- RETRYING
- RECOVERING

Terminal states:

- SUCCEEDED
- FAILED
- CANCELLED

Terminal states are irreversible.

## 5. State Transitions

The principal transitions are:

```text
CREATED -> STARTING -> CONTEXT_LOADING -> RUNNING

RUNNING -> WAITING_APPROVAL -> RUNNING
RUNNING -> PAUSED -> RUNNING
STARTING | CONTEXT_LOADING | RUNNING -> RETRYING
RETRYING -> STARTING | CONTEXT_LOADING | RUNNING
RUNNING -> RECOVERING -> RUNNING

Any active state -> CANCELLED
RUNNING -> SUCCEEDED
Any active state -> FAILED for a final or non-retryable failure
```

Additional legal transitions:

- STARTING may fail or be cancelled.
- CONTEXT_LOADING may retry, fail, or be cancelled.
- WAITING_APPROVAL may be paused, failed by policy, or cancelled.
- PAUSED may be cancelled.
- RETRYING or RECOVERING may fail or be cancelled.

Direct WAITING_APPROVAL to SUCCEEDED is invalid; execution resumes to RUNNING so
the authorized decision and remaining work are recorded consistently.

## 6. Lifecycle Operations

| Operation | Valid source | Result |
| --- | --- | --- |
| start | CREATED | STARTING |
| pause | STARTING, CONTEXT_LOADING, RUNNING, WAITING_APPROVAL, RETRYING, RECOVERING | PAUSED after active calls acknowledge pause policy |
| resume | PAUSED | Previous resumable phase or RUNNING after required resolution |
| decide approval | WAITING_APPROVAL | RUNNING, FAILED, or CANCELLED according to authorized decision and policy |
| cancel | Any non-terminal state | CANCELLED after cancellation cleanup |
| retry | Retryable attempt failure in a non-terminal phase | RETRYING; close the current attempt, increment attempt, then redispatch the failed phase |
| recover | Non-terminal persisted Run after interruption | RECOVERING with incremented attempt |

Retry and recovery are not valid after FAILED. A retryable attempt failure does
not transition the Run through FAILED. The controller atomically records the
attempt outcome and enters RETRYING when retry policy permits another attempt.
When the failure is non-retryable or retry policy is exhausted, the controller
records the final attempt outcome and transitions the Run directly to FAILED.

## 7. Attempts

Attempt begins at 1 and increments before retry or recovery dispatch. Attempt
history records start, end, outcome, checkpoint source, and error. Attempt
outcomes distinguish at least `SUCCEEDED`, `RETRYABLE_FAILURE`,
`FINAL_FAILURE`, `CANCELLED`, and `INTERRUPTED`; they are not Run states.

`RETRYABLE_FAILURE` closes the current attempt and enters RETRYING without
terminalizing the Run. Redispatch returns to the failed resumable phase defined
by retry policy: STARTING, CONTEXT_LOADING, or RUNNING. `FINAL_FAILURE` closes
the current attempt and enters terminal FAILED.

The Run identity remains stable across attempts. Idempotency keys include the
logical operation identity and remain stable when replay safety requires it;
they are not regenerated solely because attempt increments.

## 8. Concurrency

State mutation includes the expected `state_version`. A stale mutation returns a
conflict and does not emit a state transition event.

When completion and cancellation race, exactly one transition commits. Cleanup
may still run, but it cannot rewrite the committed terminal state.

Repeated operations are idempotent when they request an already reached state,
and return a conflict when their requested transition is no longer legal.

## 9. Events and Audit

Every committed transition emits RunStateChanged with previous status, new
status, attempt, reason, and state version. Approval request and decision,
authorization denial, and security-sensitive cancellation are audit events.

Event emission follows [RFC-0010](RFC-0010-runtime-events.md).

## 10. Conformance

A conforming implementation demonstrates:

- Root AgentRun and root WorkflowRun execution.
- Correct parent and root relationships.
- PAUSED and WAITING_APPROVAL as resumable non-terminal states.
- Irreversible terminal states.
- Monotonic attempts and state versions.
- Retryable attempt failure entering RETRYING without entering FAILED.
- Exhausted or non-retryable failure entering terminal FAILED.
- Deterministic cancellation/completion races.
- Rejection of illegal transitions, retry after FAILED, and recovery after
  FAILED.
- CLOSED Session association rejection without history deletion.
