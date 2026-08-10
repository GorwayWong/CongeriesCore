# RFC-0010: Runtime Events

- ID: RFC-0010
- Title: Runtime Events
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0003](../adrs/ADR-0003-runtime-events-not-event-sourcing.md), [RFC-0008](RFC-0008-scope-authorization.md)
- Supersedes: None

## 1. Scope

This RFC defines the RuntimeEvent envelope, event classes, ordering,
observability delivery, reliable audit delivery, redaction, and sink behavior.
Runtime Events do not own execution state and do not introduce Event Sourcing.

## 2. Envelope

RuntimeEvent contains:

| Field | Meaning |
| --- | --- |
| `event_id` | Globally unique event identity used for deduplication |
| `event_type` | Namespaced event type |
| `schema_version` | Payload contract version |
| `occurred_at` | Source timestamp |
| `run_id` | Owning Run |
| `root_run_id` | Root execution correlation |
| `parent_run_id` | Optional immediate parent correlation |
| `sequence` | Monotonic sequence within the owning Run |
| `scope` | Effective ScopeRef |
| `correlation_id` | Cross-component operation correlation |
| `causation_id` | Event or command that caused this event |
| `sensitivity` | Public, internal, sensitive, or restricted |
| `delivery_class` | OBSERVABILITY or AUDIT |
| `payload` | Versioned event-specific data |

Event type and schema version determine payload validation. Unknown versions are
not silently parsed as a known version.

## 3. Delivery Classes

### OBSERVABILITY

Observability events are non-blocking. Sink failure:

- Does not change Run state.
- Produces local diagnostics and metrics where available.
- May be retried within bounded sink policy.
- May be dropped after policy exhaustion.

### AUDIT

Approval, authorization, security, and explicit cross-scope events use AUDIT.
The emitting protected transition or action waits for acknowledgement.

Audit sink failure pauses the Run by default. ExecutionPolicy may choose FAILED.
Core does not continue the protected action without required acknowledgement.

Audit delivery is at least once. Sinks deduplicate by event_id.

## 4. Ordering

Sequence increases monotonically for committed events within one Run. Consumers
must not infer global ordering across Runs from timestamps.

Child Run events have their own sequence and carry root and parent correlation.
Event retries preserve event_id and sequence.

State transition event allocation occurs only after the state mutation commits.
A failed stale transition emits no RunStateChanged event.

## 5. Event Catalog

Core event types include:

- `core.run.state_changed`
- `core.tool.invocation_started`
- `core.tool.invocation_completed`
- `core.tool.invocation_failed`
- `core.approval.requested`
- `core.approval.decided`
- `core.authorization.denied`
- `core.authorization.cross_scope_granted`
- `core.checkpoint.saved`
- `core.checkpoint.failed`
- `core.artifact.created`
- `core.plugin.lifecycle_changed`

Plugins use namespaced business event types outside the Core catalog.

## 6. Redaction

Event payloads contain references and summaries by default, not Context, Memory,
model prompt, Tool secret, or Artifact content.

Redaction policy applies before sink dispatch. A sink never receives data beyond
its authorized Scope and sensitivity grant. Redaction failure blocks AUDIT
delivery and drops or sanitizes OBSERVABILITY delivery according to policy.

## 7. EventSink

EventSink declares supported delivery classes, schema versions, sensitivity,
acknowledgement, batch, and retry behavior.

Dispatch receives RuntimeCallContext and is authorized like other providers.
Audit sinks provide durable acknowledgement identity. Observability sinks may be
in-process, remote, or absent.

## 8. State Independence

Run, Workspace, Session, Plugin, and Checkpoint stores remain the source of
runtime state. The system can read current state without replaying Runtime
Events. Event retention does not define state retention.

## 9. Failure Semantics

The event subsystem represents invalid event, unsupported schema, denied,
unavailable, timeout, cancelled, redaction failure, and acknowledgement conflict.

Duplicate acknowledgement is success for the same event identity. An
acknowledgement for a different payload under the same identity is conflict.

## 10. Conformance

A conforming implementation demonstrates:

- Envelope validation and per-Run monotonic sequence.
- Non-blocking observability sink failure.
- Audit acknowledgement before protected action.
- Default pause and policy-selected failure on audit sink failure.
- At-least-once audit retry with sink deduplication.
- Redaction before dispatch.
- Runtime state access without event replay.
