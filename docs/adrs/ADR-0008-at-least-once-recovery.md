# ADR-0008: Recovery Is At Least Once

- ID: ADR-0008
- Title: Recovery Is At Least Once
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [RFC-0011](../rfcs/RFC-0011-checkpoint-recovery.md), [RFC-0003](../rfcs/RFC-0003-workflow.md)
- Supersedes: None

## Context

Exactly-once execution across arbitrary tools, providers, models, and external
systems would require distributed transaction semantics they do not share.

## Decision

Core checkpoints stable node boundaries and provides at-least-once recovery.
The interrupted node may replay. Side-effecting operations require stable
idempotency keys across attempts.

## Consequences

- Providers participate through idempotent operation contracts.
- Graph-version mismatch requires explicit checkpoint migration.
- Core does not claim distributed exactly-once behavior.

## Alternatives Rejected

- At-most-once recovery that can lose unfinished work.
- A mandatory distributed transaction coordinator.

