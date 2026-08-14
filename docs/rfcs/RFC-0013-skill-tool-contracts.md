# RFC-0013: Skill and Tool Contracts

- ID: RFC-0013
- Title: Skill and Tool Contracts
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-11
- Updated: 2026-08-12
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [RFC-0002](RFC-0002-plugin-sdk.md), [RFC-0008](RFC-0008-scope-authorization.md), [RFC-0010](RFC-0010-runtime-events.md), [RFC-0016](RFC-0016-tool-operation-log.md)
- Supersedes: None

## 1. Scope

This RFC defines immutable Skill v1 and Tool v1 contracts, their typed views over
the Plugin registry, progressive Skill resource loading, authorized Tool
execution, shared reference resolution, and AgentSpec compatibility. Concrete
instructions, scripts, Tool business behavior, Plugin loading technology, MCP,
Workflow scheduling, Agent Tool loops, and automatic context injection are outside
this RFC. RFC-0003 composes these gateways into SkillNode and ToolNode; RFC-0016
owns durable Tool operation state.

### 1.1 Plain-language overview

A Skill is a catalog with a sealed menu. Core can inspect the menu without
opening every file. When a caller asks for one named resource, Core checks that
the resource is on the menu, authorizes that exact read, borrows the owning
Plugin through one execution lease, enforces the byte limit, and returns the
content. Returning content is the end of the operation: Core does not silently
append it to an Agent prompt or context.

A Tool is a guarded function call. Core checks the input shape first, verifies
the caller and requested Scope, borrows the owning Plugin, executes under one
deadline and one stable operation identity, checks the output shape, and always
returns the lease. A retry is another attempt inside that same protected call;
it is not a new operation with a new identity.

AgentSpec stores references to these capabilities, not implementation objects.
AgentRuntime checks that every referenced Skill and Tool currently exists and is
compatible before changing Run state or calling Context or Model providers. It
still does not load Skill content or execute a Model's Tool proposal. MCP and
Workflow node composition remain separate boundaries.

## 2. Versioned Capability References

`CapabilityRef` contains `namespace`, `kind`, `id`, `owning_extension`, and an
exact `contract_version`. Skill and Tool v1 use wire version `1`; ranges,
implicit latest selection, and version negotiation are invalid. The matching
Plugin capability declaration uses canonical SemVer `1.0.0`. The typed facade
performs this explicit major-to-SemVer identity mapping and does not select a
different registered version.

The reference namespace is `core`, kind is `skill` or `tool`, identity is stable,
and `owning_extension` must equal the owning Plugin. A resolver rejects the
wrong kind, owner, version, Schema, Action, or permission, resolves only the
current immutable registry snapshot, and returns that registration's identity.
Registration generation and stale-receipt protection remain properties of the
Plugin registry receipt rather than fields on `CapabilityRef`.

## 3. Skill v1

`SkillDescriptor` contains an exact Skill reference, title, summary, and an
immutable tuple of `SkillResourceDescriptor` values. Resource descriptors contain
a unique resource identity, kind, normalized POSIX relative path, media type,
positive byte limit, and versioned read Action. Empty segments, `.`, `..`,
backslashes, absolute paths, duplicate identities, and duplicate paths are
invalid.

Skill metadata discovery never invokes `SkillResourceLoader`. A
`SkillResourceRequest` names exactly one declared resource and a positive byte
budget. The loader receives only that descriptor and `RuntimeCallContext`; it
cannot enumerate paths or return additional resources. The result is one
`ContentBlock`. Text and references use actual UTF-8 byte length. JSON uses
canonical UTF-8 JSON with sorted keys and compact separators.

The gateway validates the reference, declaration, resource identity, and
requested byte budget before authorization. It authorizes
`core.skill.resource.read` against a resource-specific `ResourceRef`. Grant
constraints preserve resource identity, path, and media type and may only lower
`max_bytes`. The owning Plugin lease covers loader use, media validation, and
the final size check. DRAINING rejects new reads while an existing leased read
may complete. Results are returned to the caller and are never inserted into an
Agent context implicitly.

Lease generation is distinct from stable operation identity. A completed Skill
read may be replayed sequentially with the same logical identity, but overlapping
duplicate reads still conflict. This permits at-least-once Workflow recovery
without allowing concurrent duplicate Plugin work.

## 4. Tool v1

`ToolDescriptor` contains an exact Tool reference, title, summary, input and
output `SchemaRef`, declared Action, `ToolExecutionPolicy`, side-effect class,
and idempotency mode. Policy defines a whole-invocation `timeout_ms` and positive
`max_attempts`; v1 has no backoff contract. Side-effect-free Tools use
`not_applicable`. External side-effecting Tools require `caller_key`.

The Tool-specific timeout is anchored when `ToolGateway.execute` begins, so
resolution, input validation, authorization, lease acquisition, executor work,
retry, and output validation all consume the same budget. A descriptor timeout
of `None` means that the descriptor adds no Tool-specific deadline; the caller's
existing deadline still applies, and an authorization grant may add a finite
deadline as a restriction. A grant may never remove or extend a finite
descriptor timeout.

Every Plugin-backed call requires `RuntimeCallContext.idempotency_key` for its
lease identity. `ToolCall` contains the exact Tool reference and JSON-compatible
input. `ToolResult` contains normalized JSON output, attempts, and the stable
operation identity.

The gateway order is:

```text
resolve registration and descriptor
    -> validate call shape and input Schema
    -> authorize Action/resource and narrow Scope/constraints
    -> acquire one owning Plugin lease
    -> invoke the optional durable execution guard
    -> execute all attempts with one operation identity and whole-call deadline
    -> validate output Schema
    -> release the lease in finally
```

Grant constraints preserve Action, input/output Schema, side-effect class, and
idempotency mode. They may only shorten timeout or lower attempts. Unknown,
malformed, identity-changing, or expanding constraints are denied. Only
structured errors marked `retryable=True` and normalized executor failures are
retried. Denied, invalid grant, input/output Schema, and protocol failures are
not retried. Ordinary executor exceptions become redacted retryable unavailable
errors. A released lease identity cannot silently start another side effect;
reuse returns `lease_identity_conflict`. Side-effect-free Tools may replay
sequentially with the same logical identity; external side-effecting Tools may not.

When supplied, `ToolExecutionGuard` runs after resolution, input validation,
authorization, grant validation, and lease acquisition, but before the first
executor attempt. It runs once for the whole invocation. Guard failure enters no
executor. RFC-0016 uses this hook to make `dispatching` durable before external
execution; retries retain the same operation identity and do not rerun the guard.

## 5. Registration and Implementation Access

`SkillRegistry` and `ToolRegistry` are read-only typed facades over one
`CapabilityRegistry` immutable snapshot. They expose no independent register or
unregister operation. Publication, rollback, owner receipt, registration
generation, atomic visibility, and stale-receipt protection remain owned by
RFC-0002.

`SkillImplementation` and `ToolImplementation` freeze descriptor/implementation
pairs inside Plugin registrations. Resolved public values expose only descriptor,
owner, and registration identity. Loader and executor objects are handed only to
the callback owned by `PluginCapabilityInvoker`, after authorization and lease
acquisition, and never escape that callback.

## 6. AgentSpec and Shared Resolution

Legacy AgentSpec v1 has no top-level version and encodes Skill/Tool references as
`ResourceRef`. It remains readable and byte-exact when reserialized. AgentSpec v2
has top-level `contract_version: "2"` and versioned `CapabilityRef` values.
`upgrade_v2()` performs the explicit wire migration without loading a capability;
an unowned legacy reference remains readable but cannot be upgraded or resolved
until an owning extension is supplied.

`SkillToolResolver` is shared by AgentRuntime and Workflow adapters.
AgentRuntime preflights every Skill and Tool reference before Run transition,
Context resolution, or Model invocation. The current Model request receives only
resource identities. AgentRuntime does not load Skill resources and does not
execute Tool proposals. Workflow SkillNode and ToolNode execution is defined by
RFC-0003 and remains outside AgentRuntime.

## 7. Authorization, Events, and Errors

Skill and Tool gateways reuse `AuthorizedDispatcher`, `RuntimeCallContext`, Scope
narrowing, audit failure handling, deadline/cancellation control, and Plugin
leases. There is no alternate direct execution path.

Observability events are Skill resource load and Tool invocation
started/completed/failed. Payloads may contain capability/resource identity,
Schema identity, byte count, attempts, latency, result class, and redacted error
code. They never contain Skill content, script data, Tool input/output, exception
text, or secrets. Authorization denial and cross-Scope audit remain reliable
RFC-0008 events; observability delivery is best effort.

Validation uses structured categories including invalid request, denied,
unavailable, conflict, version mismatch, timeout, cancelled, and protocol
failure. Failures before the lease produce no loader or executor effect. Every
failure after acquisition releases the lease.

## 8. Conformance

A conforming implementation demonstrates exact fixture round trips; two
independent loader and executor implementations; lazy Skill discovery; strict
paths, versions, owner generations, Actions, permissions, and Schemas; default
denial and invalid-grant non-bypass; one-lease stable-identity Tool retry; output
validation before release; timeout, cancellation, drain, unload, and replay
behavior; and Agent preflight before external effects.
