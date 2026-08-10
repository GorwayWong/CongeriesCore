# ADR-0001: Workflow Engines Are Adapters

- ID: ADR-0001
- Title: Workflow Engines Are Adapters
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Principles](../../principles.md), [RFC-0003](../rfcs/RFC-0003-workflow.md)
- Supersedes: Legacy ADR-001 in RFC-0005

## Context

Workflow execution may be implemented by a custom DAG engine, LangGraph,
Temporal, or another framework. Binding Core contracts to one engine would make
plugins and persisted state framework-specific.

## Decision

Core owns the vendor-neutral Workflow, Run, event, and checkpoint contracts.
Execution frameworks integrate through adapters and declare supported features.

## Consequences

- Public Core types contain no framework-native objects.
- Adapter conformance tests verify identical observable semantics.
- A framework feature unavailable through the Core contract remains
  adapter-specific and cannot silently change portable Workflow behavior.

## Alternatives Rejected

- Making LangGraph or Temporal a mandatory Core dependency.
- Defining only a lowest-level callback API with no portable workflow semantics.

