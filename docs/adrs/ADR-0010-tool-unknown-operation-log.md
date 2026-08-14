# ADR-0010: Persist Unknown Tool Outcomes Outside Checkpoint v1

- ID: ADR-0010
- Title: Persist Unknown Tool Outcomes Outside Checkpoint v1
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-12
- Updated: 2026-08-12
- Related: [RFC-0016](../rfcs/RFC-0016-tool-operation-log.md), [RFC-0003](../rfcs/RFC-0003-workflow.md), [Requirements](../../requirements.md)
- Supersedes: None

## Context

A side-effecting Tool can complete externally while Core loses the response. A
Checkpoint v1 side-effect record describes only stable succeeded or failed
outcomes and is immutable once committed. Treating an uncertain response as a
failure permits unsafe automatic replay; treating it as success unlocks dependent
work without evidence.

## Decision

Core records Tool invocation intent and uncertain outcomes in an independent,
replaceable durable Tool Operation Log. The log owns `prepared`, `dispatching`,
`unknown`, `succeeded`, and `failed` states with compare-and-set transitions.
Checkpoint v1 remains byte-compatible and records a Tool side effect only after a
terminal operation outcome is durable.

An unknown outcome pauses the same WorkflowRun. Core never infers the outcome from
Runtime Events and never automatically queries or replays the external system.
An authorized application actor must provide a durable evidence reference and an
explicit succeeded or failed resolution. Success resumes the same Run; failure
terminates it.

## Consequences

- Unknown external effects are visible recovery work rather than hidden retries.
- Applications own reconciliation evidence without introducing a Core user model.
- A standard-library SQLite reference adapter provides restart durability without
  becoming a mandatory production dependency.
- Checkpoint v1 and existing approval suspension fixtures remain compatible.

Detailed contracts are owned by [RFC-0016](../rfcs/RFC-0016-tool-operation-log.md)
and Workflow composition by [RFC-0003](../rfcs/RFC-0003-workflow.md).
