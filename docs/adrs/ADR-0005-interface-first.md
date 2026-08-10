# ADR-0005: External Capability Is Interface-First

- ID: ADR-0005
- Title: External Capability Is Interface-First
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Principles](../../principles.md), [Design](../../design.md)
- Supersedes: Legacy ADR-005 in RFC-0005

## Context

Core interacts with models, context, memory, storage, workflow engines, MCP, and
event sinks. Direct dependencies would make implementations mandatory.

## Decision

Core defines typed provider and adapter interfaces and receives implementations
through dependency injection. Vendor and framework types stop at adapters.

## Consequences

- Implementations remain independently replaceable and testable.
- Contract tests apply to multiple implementations.
- A new dependency requires a Core-wide need or remains optional in an adapter.

## Alternatives Rejected

- Direct database or model-vendor access from runtime modules.
- Framework-specific public Core APIs.

