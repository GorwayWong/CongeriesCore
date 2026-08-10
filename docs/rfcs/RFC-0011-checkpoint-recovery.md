# RFC-0011: Checkpoint and Recovery

- ID: RFC-0011
- Title: Checkpoint and Recovery
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0008](../adrs/ADR-0008-at-least-once-recovery.md), [RFC-0003](RFC-0003-workflow.md), [RFC-0004](RFC-0004-execution-run-lifecycle.md)
- Supersedes: None

## 1. Scope

This RFC defines atomic workflow checkpoints, stable boundaries, CheckpointStore,
at-least-once recovery, graph-version migration, integrity, and side-effect
idempotency. It does not promise distributed exactly-once execution.

## 2. Checkpoint

Checkpoint contains:

| Field | Meaning |
| --- | --- |
| `checkpoint_id` | Stable checkpoint identity |
| `run_id` | Owning WorkflowRun |
| `graph_id` | Workflow definition identity |
| `graph_version` | Exact graph contract version |
| `sequence` | Monotonic checkpoint sequence within the Run |
| `attempt` | Attempt that produced the checkpoint |
| `node_states` | Completed and stable node outcome references |
| `pending_nodes` | Nodes eligible or waiting for execution |
| `context_refs` | Authorized references to Context state |
| `memory_refs` | Authorized references to Memory state |
| `artifact_refs` | Artifact references |
| `idempotency` | Logical operation keys and recorded outcomes |
| `created_at` | Commit timestamp |
| `integrity` | Contract version and checksum or equivalent integrity data |

Sensitive external state is stored by reference or protected opaque payload. A
checkpoint does not copy provider secrets or unrestricted Context and Memory.

## 3. CheckpointStore

CheckpointStore exposes:

```text
save(Checkpoint, RuntimeCallContext) -> CheckpointRef
load(CheckpointRef, RuntimeCallContext) -> Checkpoint
list(CheckpointQuery, RuntimeCallContext) -> CheckpointPage
delete(DeleteCheckpointRequest, RuntimeCallContext) -> DeleteResult
```

Save atomically replaces no valid checkpoint until the new checkpoint is fully
validated and durable. Sequence conflict rejects stale writers.

List and delete remain within the granted Run and Scope. Delete does not change
Run outcome or erase required audit records.

## 4. Stable Boundaries

A stable boundary exists after a node outcome and required side-effect record are
durable. Core commits checkpoints:

- At workflow start after validation
- After configured node boundaries
- Before entering WAITING_APPROVAL
- After an approval decision is durably recorded
- Before a planned pause when policy requires durability
- Before terminal success when final output references are stable

Core does not checkpoint an externally visible side effect without its
idempotency identity or recorded outcome.

## 5. At-Least-Once Recovery

Recovery may replay the node that was active after the latest valid checkpoint.
Every side-effecting Tool and Provider operation therefore declares and receives
an idempotency key.

The same logical side effect retains its key across attempts. Providers either
return the recorded result or reject a mismatched payload as conflict.

Pure computation nodes may replay without external deduplication.

## 6. Recovery Flow

```text
load Run and latest valid checkpoint
    -> authorize Scope and references
    -> verify integrity and graph version
    -> create a new attempt
    -> enter RECOVERING
    -> restore stable node state and pending work
    -> enter RUNNING
    -> replay from the recovery boundary
```

Recovery is valid only for non-terminal persisted Runs. FAILED, SUCCEEDED, and
CANCELLED Runs do not resume.

## 7. Graph Version and Migration

Exact graph-version match resumes directly. Mismatch returns version mismatch
unless a registered CheckpointMigrator supports the source and target versions.

A migrator:

- Is explicit and version-pair specific.
- Produces a new checkpoint without modifying the source.
- Preserves Run, Scope, Artifact, and idempotency identity.
- Validates all node mappings and pending work.
- Emits an audit event for success or failure.

Migration failure leaves the source checkpoint valid and unchanged.

## 8. Corruption and Fallback

Load validates identity, schema version, sequence, and integrity. A corrupt
latest checkpoint may fall back only to an earlier valid checkpoint selected by
explicit recovery policy. Fallback increases the replay window and is audited.

Missing, denied, or corrupt references return structured failure; they are not
silently removed from restored state.

## 9. Failure Semantics

Outcomes include invalid checkpoint, denied, unavailable, timeout, cancelled,
sequence conflict, version mismatch, integrity failure, missing reference,
migration failure, and idempotency conflict.

A failed save never replaces the latest valid checkpoint. Recovery failure may
remain non-terminal for operator action or transition to FAILED by policy.

## 10. Conformance

A conforming implementation demonstrates:

- Atomic save and stale sequence rejection.
- Stable boundaries before and after approval.
- Recovery with a new attempt and possible node replay.
- Idempotent side effects across replay.
- Rejection of graph mismatch without migrator.
- Non-destructive migration.
- Corrupt-checkpoint detection and explicit fallback audit.
- No recovery from terminal Run states.
