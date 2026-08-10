# ADR-0003: Runtime Events Are Not Event Sourcing

- ID: ADR-0003
- Title: Runtime Events Are Not Event Sourcing
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Principles](../../principles.md), [RFC-0010](../rfcs/RFC-0010-runtime-events.md)
- Supersedes: Legacy ADR-003 in RFC-0005

## Context

Core needs tracing, lifecycle visibility, and security audit. Full Event Sourcing
would make event replay the state authority and add storage, migration, ordering,
and operational requirements beyond the runtime goal.

## Decision

Runtime state remains in explicit state stores. Runtime Events observe committed
state and protected actions. Observability delivery is non-blocking; approval,
authorization, and security audit delivery is reliable and at least once.

## Consequences

- Current state is readable without event replay.
- Audit sinks deduplicate by event identity.
- Event retention and state retention are independent.
- Business events remain plugin responsibility.

## Alternatives Rejected

- Full Event Sourcing as the Core persistence model.
- Best-effort delivery for security-sensitive audit events.

