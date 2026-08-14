# RFC-0016: Tool Operation Log

- ID: RFC-0016
- Title: Tool Operation Log
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-12
- Updated: 2026-08-12
- Related: [ADR-0010](../adrs/ADR-0010-tool-unknown-operation-log.md), [RFC-0003](RFC-0003-workflow.md), [RFC-0011](RFC-0011-checkpoint-recovery.md), [RFC-0013](RFC-0013-skill-tool-contracts.md)
- Supersedes: None

## 1. Scope

This RFC defines the version 1 durable state boundary for side-effecting Tool
operations. It does not define business reconciliation, compensation, automatic
external queries, a distributed transaction, or exactly-once execution.

## 2. Record

`ToolOperationRecord` is strict and immutable. It contains contract and CAS
versions; operation, Run, Workspace, Node, Scope, and Tool identities; one caller
idempotency key; a lowercase canonical SHA-256 request fingerprint; a durable
request reference; side-effect classification; status; optional outcome and
evidence references; and UTC creation/update times. It never embeds Tool input or
output.

The operation reference is `core/tool_operation/{operation_id}` and is scoped no
wider than the Workflow node. `(run_id, idempotency_key)` and operation identity
are unique.

## 3. State Machine

```text
prepared -> dispatching -> succeeded
                        -> unknown -> succeeded | failed
prepared -> failed
```

A side-effect-free Tool may also use `dispatching -> failed`; that transition is
not valid for an uncertain external side effect.

Every transition supplies the expected CAS version. Succeeded and failed records
are immutable. Repeating `prepare` for the same operation, key, fingerprint, and
request reference returns the original record. Identity or payload drift is a
conflict. Repeating an identical terminal transition is idempotent; different
terminal evidence is a conflict.

`dispatching` is durable before the first executor attempt. Recovery observes a
persisted `dispatching` record as `unknown`; it never redispatches that operation.

## 4. Port and Authorization

The replaceable `ToolOperationStore` exposes prepare, read, and compare-and-set
transition operations. `ToolOperationGateway` is the only authorized caller and
registers:

- `core.tool_operation.prepare` v1
- `core.tool_operation.read` v1
- `core.tool_operation.transition` v1
- `core.tool_operation.resolve` v1

Every access binds the principal, Run, Workspace, Scope, operation identity,
idempotency key, and fingerprint. Runtime execution uses the Workflow node
principal. Explicit resolution uses the supplied application `RuntimePrincipal`.
Default denial and invalid-grant behavior follow RFC-0008.

## 5. Durability and Adapters

The in-memory and SQLite adapters pass one contract suite. SQLite uses WAL,
`BEGIN IMMEDIATE`, a unique Run/key constraint, and version-predicate updates.
Restart preserves all records. Backend failures are normalized and no adapter
object appears in public records.

## 6. Workflow Recovery

Before Tool dispatch, Workflow persists the typed request, prepares the operation,
and commits a Checkpoint containing its external reference while the node remains
pending. Only the WorkflowRun marker selects the Checkpoint as a recovery source.

- `prepared`: execution may start with the same key and fingerprint.
- `dispatching`: transition conservatively to `unknown`, persist a suspension
  boundary, and pause the Run.
- `unknown`: return the same Tool-operation suspension without Tool execution.
- `succeeded`: reuse the durable result and commit the stable node boundary.
- `failed`: reuse the durable error and terminalize the Run.

Unknown creates no Checkpoint v1 `SideEffectRecord`. A resolved succeeded or
failed record produces the stable side-effect entry and node state together.
Runtime Events are observability only.

## 7. Explicit Resolution

`ToolOperationResolution` supplies operation identity, expected version, terminal
outcome, successful JSON output or a structured error, attempt count, and a
mandatory durable evidence reference. The application actor is supplied separately
as a `RuntimePrincipal`. The gateway authorizes `resolve`, checks identity and
Scope, validates the successful Tool output Schema, persists the typed result, and
performs one CAS transition.

Resolution success commits a successful node Checkpoint, resumes the same paused
WorkflowRun, and continues dependency scheduling. Resolution failure commits a
failed node Checkpoint and terminates the Run. A crash between these steps is
recoverable by rereading the terminal operation record and the last committed Run
marker.

## 8. Conformance

Conformance requires exact fixtures; shared in-memory and SQLite tests for replay,
payload drift, CAS, authorization, Scope, restart, and concurrency; Workflow crash
tests at every persistence boundary; proof that unknown never unlocks dependents
or automatically invokes a Tool; and explicit succeeded/failed resolution tests.
