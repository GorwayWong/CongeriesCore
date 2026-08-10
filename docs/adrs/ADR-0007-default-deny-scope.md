# ADR-0007: Scope Authorization Denies by Default

- ID: ADR-0007
- Title: Scope Authorization Denies by Default
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [RFC-0008](../rfcs/RFC-0008-scope-authorization.md), [Requirements](../../requirements.md)
- Supersedes: None

## Context

Tools and providers cross application and infrastructure boundaries. Provider-
specific authorization would create inconsistent enforcement and audit gaps.

## Decision

Core defines generic namespaced ScopeRef and one AuthorizationPolicy boundary.
Missing, unknown, or indeterminate authorization is denied. Application scope
kinds remain plugin-defined.

## Consequences

- Tool, Provider, Storage, EventSink, CheckpointStore, and MCP dispatch share the
  same authorization path.
- Cross-scope access requires an explicit audited grant.
- Core does not add user or organization models.

## Alternatives Rejected

- Default allow with provider-specific checks.
- Built-in business scope enumeration.

