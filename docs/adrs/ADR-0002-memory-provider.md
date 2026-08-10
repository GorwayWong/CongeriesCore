# ADR-0002: Memory Is Provided by Plugins

- ID: ADR-0002
- Title: Memory Is Provided by Plugins
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Principles](../../principles.md), [RFC-0007](../rfcs/RFC-0007-memory-provider.md)
- Supersedes: Legacy ADR-002 in RFC-0005

## Context

Applications require different memory schemas, retention, ranking, embedding,
and persistence strategies. A built-in implementation would introduce domain
and database coupling.

## Decision

Core defines the MemoryProvider contract only. Plugins own memory semantics,
data models, ranking, embeddings, and storage.

## Consequences

- Core can authorize and orchestrate memory without interpreting content.
- Applications can replace memory implementations independently.
- Conversation history is not redefined as Core memory storage.

## Alternatives Rejected

- A built-in vector database memory implementation.
- A single universal memory schema.

