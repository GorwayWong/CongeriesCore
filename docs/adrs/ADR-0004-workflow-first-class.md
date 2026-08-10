# ADR-0004: Workflow Is First-Class

- ID: ADR-0004
- Title: Workflow Is First-Class
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Principles](../../principles.md), [RFC-0003](../rfcs/RFC-0003-workflow.md)
- Supersedes: Legacy ADR-004 in RFC-0005

## Context

Reliable multi-step execution requires typed dependencies, explicit policy,
approval, checkpoints, and recovery. Treating workflow as prompt convention
would leave those semantics opaque.

## Decision

Workflow is a first-class, versioned Core contract. Core executes the contract;
plugins define concrete graphs and their business meaning.

## Consequences

- Workflows validate before execution.
- Engine adapters preserve portable Run and checkpoint semantics.
- Core contains no predefined business workflow.

## Alternatives Rejected

- Prompt collections as workflow definitions.
- Hardcoded application flows inside Core.

