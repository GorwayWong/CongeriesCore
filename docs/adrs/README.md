# Architecture Decision Record Registry

- Target Version: 0.2.0

ADRs record accepted cross-cutting decisions and their rationale. Public
interface details belong to the linked RFCs.

| ID | Decision | Status |
| --- | --- | --- |
| [ADR-0001](ADR-0001-workflow-engine-adapters.md) | Workflow engines are adapters | Accepted |
| [ADR-0002](ADR-0002-memory-provider.md) | Memory is provided by plugins | Accepted |
| [ADR-0003](ADR-0003-runtime-events-not-event-sourcing.md) | Runtime Events are not Event Sourcing | Accepted |
| [ADR-0004](ADR-0004-workflow-first-class.md) | Workflow is first-class | Accepted |
| [ADR-0005](ADR-0005-interface-first.md) | External capability is interface-first | Accepted |
| [ADR-0006](ADR-0006-run-and-session.md) | Run is generic and SessionRef is lightweight | Accepted |
| [ADR-0007](ADR-0007-default-deny-scope.md) | Scope authorization denies by default | Accepted |
| [ADR-0008](ADR-0008-at-least-once-recovery.md) | Recovery is at least once | Accepted |
| [ADR-0009](ADR-0009-safe-plugin-unload.md) | Plugin unload drains active work | Accepted |

The documentation lifecycle and metadata rules are defined in
[docs/README.md](../README.md).
