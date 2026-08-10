# ADR-0006: Run Is Generic and SessionRef Is Lightweight

- ID: ADR-0006
- Title: Run Is Generic and SessionRef Is Lightweight
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [RFC-0004](../rfcs/RFC-0004-execution-run-lifecycle.md), [Design](../../design.md)
- Supersedes: None

## Context

Core supports direct Agent execution and Workflow execution. Making either one
the universal root would create an artificial dependency. Related Runs also need
correlation without introducing a conversation or user model.

## Decision

Run is a common envelope with AgentRun and WorkflowRun peer types. Either may be
a root, and nested execution uses parent/root relationships. SessionRef groups
related Runs and contributes isolation but owns no messages or participants.

## Consequences

- Direct Agent and direct Workflow entrypoints remain valid.
- Workflow AgentNode creates a child AgentRun.
- Workspace owns durable state; SessionRef owns correlation only.

## Alternatives Rejected

- Requiring AgentRun above every WorkflowRun.
- Requiring WorkflowRun above every AgentRun.
- A Core conversation-history Session model.

