# RFC-0011: Checkpoint and Recovery

- ID: RFC-0011
- Title: Checkpoint and Recovery
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-11
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0008](../adrs/ADR-0008-at-least-once-recovery.md), [RFC-0003](RFC-0003-workflow.md), [RFC-0004](RFC-0004-execution-run-lifecycle.md)
- Supersedes: None

## 1. Scope

This RFC defines atomic workflow checkpoints, stable boundaries, CheckpointStore,
at-least-once recovery, graph-version migration, integrity, and side-effect
idempotency. It does not promise distributed exactly-once execution.

## 2. Checkpoint

The public wire contract is version `1`. Checkpoint contains:

| Field | Meaning |
| --- | --- |
| `contract_version` | Checkpoint wire contract version; `1` in this RFC |
| `checkpoint_id` | Stable checkpoint identity and CheckpointRef value |
| `run_id` | Owning WorkflowRun |
| `workflow_id` | Stable Workflow identity |
| `definition_id` | Exact Workflow definition identity |
| `graph_version` | Exact graph contract version |
| `scope` | Owning ScopeRef; migration preserves it |
| `sequence` | Monotonic checkpoint sequence within the Run |
| `attempt` | Attempt that produced the checkpoint |
| `previous_checkpoint_ref` | Previous committed or source checkpoint, if any |
| `node_states` | Stable node outcomes and output or error references |
| `pending_nodes` | Nodes eligible or waiting for execution |
| `external_refs` | Typed, scoped Context, Memory, Artifact, or provider references |
| `side_effects` | Logical operation keys, request fingerprints, outcomes, and result references |
| `approvals` | Durable approval requests and decisions |
| `created_at` | Commit timestamp |
| `integrity` | Algorithm and canonical payload digest |

`CheckpointReference` contains only a resource type, ResourceRef, ScopeRef, and
optional version. Node output, errors, prompts, Context, Memory, Artifact content,
provider secrets, and other sensitive bodies are never copied into a checkpoint.

Every node identity, pending node, external reference, side-effect operation, and
approval identity is unique within its collection. Stable side effects require a
non-empty IdempotencyKey, a SHA-256 request fingerprint, and either a durable
result reference or a terminal recorded outcome.

### 2.1 Canonical Integrity

Integrity uses SHA-256 over UTF-8 canonical JSON. Object keys are sorted,
separators are compact, non-finite numbers are rejected, and UTC datetimes use
`datetime.isoformat()`. The digest input excludes only the digest value itself;
it includes `contract_version` and integrity algorithm. Loading or restoring a
checkpoint verifies the digest before using any embedded state.

## 3. CheckpointStore

CheckpointStore exposes:

```text
save(Checkpoint, RuntimeCallContext) -> CheckpointRef
load(CheckpointRef, RuntimeCallContext) -> Checkpoint
list(CheckpointQuery, RuntimeCallContext) -> CheckpointPage
delete(DeleteCheckpointRequest, RuntimeCallContext) -> DeleteResult
```

Save atomically replaces no stored value until the new checkpoint is fully
validated and durable. The same checkpoint reference and digest is an idempotent
success. Reusing a reference or `(run_id, sequence)` for different content is a
conflict. Sequence conflict rejects stale writers and never overwrites data.

List and delete remain within the granted Run and Scope. List order is descending
sequence and cursors bind Provider, Run, Scope, graph version, limit, and query
fingerprint. Delete does not change Run outcome or erase required audit records.

WorkflowRun.latest_checkpoint_ref is the recovery commit marker. The commit
sequence is:

```text
validate checkpoint
    -> authorized atomic Store save
    -> compare-and-set WorkflowRun.latest_checkpoint_ref
    -> publish core.checkpoint.saved
```

A Store write that is not followed by the Run compare-and-set is an orphan. It
does not become a recovery source and does not emit `checkpoint.saved`. Recovery
always starts from the Run marker. In v0.2 delete is permitted only for a true
orphan; a checkpoint reachable from the current marker through
`previous_checkpoint_ref` is retained. An orphan occupying a sequence remains a
conflict until explicitly deleted. Deleting the orphan releases that uncommitted
sequence; a sequence already present in the committed marker chain is never
reused.

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
load persisted non-terminal WorkflowRun
    -> load its committed marker
    -> authorize Scope and references
    -> verify integrity and graph version
    -> enter RECOVERING with a new open attempt whose checkpoint source is set
    -> restore stable node state and pending work
    -> enter RUNNING
    -> replay from the recovery boundary
```

Recovery is valid only for persisted Runs that are neither CREATED nor terminal.
FAILED, SUCCEEDED, and CANCELLED Runs do not resume. The Run cannot return to
RUNNING until CheckpointRestorer succeeds. Restore failure remains RECOVERING by
default; an explicit policy may transition to FAILED. PAUSE followed by an
ordinary resume must never bypass restore.

The coordinator validates reference identity and Scope but does not dereference
Context, Memory, or Artifact bodies. Those reads continue through their owning
authorized gateways.

### 6.1 Workflow Executor Integration

A Workflow executor commits stable boundaries through CheckpointCoordinator; it
does not call CheckpointStore directly or advance the Run marker itself. The
committed marker returned by the coordinator is the only recovery source that
the executor may schedule from.

During recovery, CheckpointRestorer rehydrates stable node outcomes, pending
nodes, external references, approvals, and side-effect identities before the Run
returns to RUNNING. The scheduler does not redispatch a node whose stable outcome
is present in the committed checkpoint. Work that was active after the committed
boundary may replay with the same idempotency identity. Runtime Events never add,
remove, or override restored work.

## 7. Graph Version and Migration

Exact definition and graph-version match resumes directly. Mismatch returns
version mismatch unless a registered CheckpointMigrator supports the key
`(workflow_id, source definition/version, target definition/version)`.

A migrator:

- Is explicit and version-pair specific.
- Produces a new reference and sequence linked to the source without modifying it.
- Preserves Run, Scope, Artifact, and idempotency identity.
- Validates all node mappings and pending work.
- Preserves external references and side-effect idempotency records.
- Requires reliable `core.checkpoint.migration_authorized` audit acknowledgement
  before a single Run compare-and-set updates definition, graph version, and marker.

Migration failure leaves the source checkpoint valid and unchanged.

## 8. Corruption and Fallback

Load validates identity, schema version, sequence, Scope, and integrity. A corrupt
latest checkpoint may fall back only to an earlier valid checkpoint selected by
explicit recovery policy. Fallback selects the highest earlier valid sequence,
requires reliable `core.checkpoint.fallback_authorized` audit acknowledgement,
then moves the Run marker with compare-and-set. It is disabled by default.

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
- Workflow execution commits through the coordinator and schedules only after
  restoration succeeds.
