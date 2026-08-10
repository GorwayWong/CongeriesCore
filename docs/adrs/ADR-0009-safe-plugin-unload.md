# ADR-0009: Plugin Unload Drains Active Work

- ID: ADR-0009
- Title: Plugin Unload Drains Active Work
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [RFC-0002](../rfcs/RFC-0002-plugin-sdk.md), [Requirements](../../requirements.md)
- Supersedes: None

## Context

Unregistering or disposing a plugin while capability calls are active can corrupt
Runs and leak resources. A simple unload hook provides no concurrency boundary.

## Decision

Plugin invocation uses active leases. Unload enters DRAINING, rejects new leases,
waits for existing leases, unregisters capability, and then disposes resources.
Unload is idempotent. Drain timeout does not force disposal.

## Consequences

- Active work completes or follows its normal cancellation policy.
- Timed-out drain remains retryable or can explicitly return to ACTIVE.
- Plugin lifecycle requires observable lease and state management.

## Alternatives Rejected

- Immediate unregister and disposal.
- Process termination as the default unload mechanism.
