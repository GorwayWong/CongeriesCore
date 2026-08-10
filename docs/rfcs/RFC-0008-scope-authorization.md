# RFC-0008: Scope and Authorization

- ID: RFC-0008
- Title: Scope and Authorization
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0007](../adrs/ADR-0007-default-deny-scope.md), [RFC-0010](RFC-0010-runtime-events.md)
- Supersedes: Security and isolation portions of the legacy RFC-0006 draft

## 1. Scope

This RFC defines generic runtime Scope, principals, access requests, policy
decisions, default-deny dispatch, grants, and audit requirements. It does not
define application users, organizations, roles, or business authorization.

## 2. ScopeRef

ScopeRef contains:

| Field | Meaning |
| --- | --- |
| `namespace` | Owner of the scope vocabulary |
| `kind` | Scope kind within the namespace |
| `id` | Opaque scope identity |
| `parent` | Optional parent ScopeRef |

Core defines runtime kinds in the `core` namespace:

- run
- workspace
- session
- agent

Plugins may define application kinds in their own namespaces. Values such as
user, organization, customer, or project are not Core kinds.

Scope ancestry narrows access. A child Scope does not automatically grant access
to its parent or siblings.

## 3. Runtime Principal

RuntimePrincipal identifies the actor making a call. Core principal kinds are:

- Run
- Agent
- Workflow node
- Plugin
- Core service

Application identity may be referenced through a namespaced external principal,
but Core does not interpret its business model.

## 4. AccessRequest

AccessRequest contains:

- RuntimePrincipal
- Action identifier
- ResourceRef with type, identity, and owning extension
- Requested ScopeRef
- RuntimeCallContext
- Requested constraints such as duration, budget, or projection

Actions and resources are namespaced and versioned. Unknown actions are denied.

## 5. PolicyDecision

AuthorizationPolicy exposes:

```text
authorize(AccessRequest) -> PolicyDecision
```

PolicyDecision is:

- ALLOW with an explicit grant and constraints
- DENY with a redacted reason code
- INDETERMINATE when the policy engine cannot decide

INDETERMINATE is treated as DENY. Absence of policy or grant is DENY.

A grant contains principal, action, resource, effective Scope, constraints,
issue time, optional expiration, policy version, and audit correlation.

## 6. Dispatch Boundary

Tool, ContextProvider, MemoryProvider, ModelProvider, StorageProvider, EventSink,
CheckpointStore, and MCP calls pass through the same authorization boundary.

Dispatch order is:

```text
validate request
    -> resolve effective Scope
    -> authorize
    -> constrain request to the grant
    -> invoke capability
    -> emit outcome event
```

A provider may impose additional restrictions. It cannot broaden a Core grant.

The v0.2 protected Provider actions are:

- `core.context.capabilities`, `core.context.provide`
- `core.memory.capabilities`, `core.memory.retrieve`, `core.memory.remember`,
  `core.memory.forget`, `core.memory.consolidate`
- `core.model.capabilities`, `core.model.generate`, `core.model.stream`

All actions use version `1`. Context AccessRequests identify Provider resources
and carry key and budget constraints. Model AccessRequests identify the
Provider/model pair and carry model, Tool exposure, and budget constraints. A
grant may narrow Scope, keys, Tools, and budgets. It cannot change a requested
model, add a key or Tool, increase a budget, or introduce an unknown constraint.
Such expansion is an invalid grant and is denied before Provider invocation.

Memory constraints are operation-specific. Retrieve grants preserve SchemaRef
and filter keys while only shrinking projection and limit. Remember grants
preserve SchemaRef while only shrinking metadata keys and maximum bytes. Forget
grants preserve Memory identity and expected version. Consolidate grants
preserve policy identity and selection keys. Unknown, malformed, or expanding
Memory constraints are invalid grants and prevent Provider invocation.

## 7. Cross-Scope Access

Cross-scope access requires an explicit grant that names source and destination
Scope. Parent relationship alone is insufficient.

Cross-scope grants are time-bounded where supported, carry a policy version,
and emit reliable audit events on grant, use, denial, expiration, and revocation.

## 8. RuntimeCallContext

RuntimeCallContext contains:

- run_id, root_run_id, and optional parent_run_id
- workspace_id and optional session_ref
- effective ScopeRef
- deadline and cancellation reference
- trace, correlation, and causation identity
- idempotency key where the operation can produce side effects

Callers cannot replace the effective Scope with a broader Scope. Child calls may
narrow Scope and deadline.

## 9. Failure and Privacy

Authorization failures return denied without revealing resource existence or
data outside the granted Scope. Policy unavailability returns indeterminate and
is denied by default.

Audit payloads include identity, action, resource class, Scope references,
decision, policy version, and redacted reason. Sensitive application payload is
excluded unless a separate grant permits it.

## 10. Conformance

A conforming implementation demonstrates:

- Denial when policy or grant is absent.
- Denial for unknown actions and broader requested Scope.
- No Tool, Provider, or MCP bypass path.
- Explicit cross-scope grants and audit.
- Indeterminate treated as denial.
- Scope narrowing in child calls.
- No built-in business scope or identity model.

## 11. Implemented Capability Coverage

The Implemented status covers the common AuthorizedDispatcher boundary and all
v0.2 capability paths that currently exist: ContextProvider capability and
provide calls, MemoryProvider capability and operation calls, ModelProvider
capability, generate, and stream calls, and EventSink dispatch. Their non-bypass,
default-deny, unknown-action, constraint, cross-scope audit, deadline,
cancellation, and audit-failure behavior is covered by contract or integration
tests.

Tool, Checkpoint, Storage, and MCP contracts are not implemented by this status.
Each future capability must register versioned actions and reuse this boundary
before its own delivery task may be marked Implemented.
