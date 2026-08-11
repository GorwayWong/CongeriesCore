# Task 5.3 MCP Adapter Code Review Guide

Status: Non-Normative reviewer aid  
Target Version: 0.2.0  
Normative contract: [RFC-0014](../rfcs/RFC-0014-mcp-adapter.md)  
Implementation range: `9c56fb0..94a2125`

## 1. What This Change Does

This change lets a Plugin satisfy an ordinary local Tool or ContextProvider by
calling a remote MCP service. It deliberately does not add a public `call_mcp`
API.

In plain language, the Plugin must declare the answer to all important questions
before it becomes active:

- Which remote service is trusted?
- Which exact remote Tool names and resource URIs may be used?
- Which local Tool or ContextProvider does each remote capability implement?
- Which remote Schema fingerprint is expected?
- Which local Action, Scope, permission, timeout, retry, and idempotency rules
  remain authoritative?

Discovery only checks that the remote server still matches this frozen list.
Unknown remote capabilities are ignored. A missing capability or changed Schema
stops the operation; it never modifies a registry or installs a remote Schema.

## 2. Review Scope and Commit Order

Review the four product commits separately. Each one has one main question:

| Commit | Question to answer |
| --- | --- |
| `a80f287` | Are the existing Tool and Plugin boundaries safe enough to host a remote implementation? |
| `477becb` | Does RFC-0014 freeze the intended boundary without expanding the product scope? |
| `a48ba2b` | Does the adapter compose existing boundaries instead of bypassing them? |
| `94a2125` | Do two independent transports prove the same contract and its failure behavior? |

The later `3a12ae8` commit contains only `.memsearch` runtime data and is not part
of the Task 5.3 product review. Review source, documentation, tests, fixtures,
and configuration against `94a2125`.

Recommended reading order:

1. Read [RFC-0014](../rfcs/RFC-0014-mcp-adapter.md), especially the plain-language
   model, explicit exclusions, and conformance section.
2. Read `McpAdapterDescriptor`, `McpToolBinding`, `McpContextBinding`, and
   `McpTransport` in
   [`mcp/model.py`](../../src/congeries_core/mcp/model.py).
3. Read the pure checks in
   [`mcp/mapping.py`](../../src/congeries_core/mcp/mapping.py) and
   [`mcp/composition.py`](../../src/congeries_core/mcp/composition.py).
4. Follow the Tool and Context paths through
   [`mcp/integration.py`](../../src/congeries_core/mcp/integration.py).
5. Check the shared safety changes in
   [`plugin/invocation.py`](../../src/congeries_core/plugin/invocation.py),
   [`plugin/lifecycle.py`](../../src/congeries_core/plugin/lifecycle.py),
   [`provider/_control.py`](../../src/congeries_core/provider/_control.py), and
   [`tool/gateway.py`](../../src/congeries_core/tool/gateway.py).
6. Use [`test_mcp_contract.py`](../../tests/test_mcp_contract.py) as executable
   evidence, then inspect the exact v0.2 fixtures.

## 3. The Two Execution Paths

### 3.1 Tool path

```text
caller
  -> ToolGateway resolves one registered local Tool
  -> local input Schema validation
  -> whole-call deadline is anchored
  -> AuthorizedDispatcher validates the local grant and Scope
  -> PluginCapabilityInvoker reserves the operation identity
  -> one Plugin execution lease is acquired
  -> McpToolExecutor validates discovery and performs one transport attempt
  -> local output Schema validation
  -> lease and invocation reservation are released
```

Important consequence: the transport does not own retry. If the transport reports
a retryable disconnect, `ToolGateway` may begin another attempt while preserving
the original idempotency identity and the same Plugin lease.

### 3.2 Context path

```text
caller
  -> ContextResolver selects and authorizes one local ContextProvider
  -> MCP Context facade enters PluginCapabilityInvoker
  -> provider declaration, permission, grant, and Scope are checked
  -> one provider Plugin lease is acquired
  -> discovery validates the exact bound resource URI and Schema digest
  -> transport reads only the requested bound keys
  -> local Schema, byte budget, completeness, and provenance checks
  -> lease is released
  -> ContextResolver performs its normal merge and final validation
```

The apparent two layers are intentional. `ContextResolver` owns the public
Context policy. The facade's nested invocation protects the Plugin capability
and its lifecycle. The inner grant may narrow keys or budgets but cannot broaden
the request already authorized by the resolver.

## 4. File-by-file Annotations

### 4.1 Baseline hardening

#### `harness/agent.py` — `AgentSpec.from_data`

The v2 wire `contract_version` is now required to be the exact string `"2"`.
Previously, coercing a numeric `2` to text accepted malformed JSON and then
silently rewrote it during serialization. Review this as a compatibility
strictness fix, not as a new Agent feature.

#### `skill/model.py` and `tool/model.py`

Integer policy fields now reject booleans and floats. Python treats `True` as an
integer, so a simple positive-number comparison was not enough for an exact wire
contract. This matters for byte budgets, attempts, and timeouts because their
values participate in authorization and deadline behavior.

#### `tool/gateway.py` — `ToolGateway.execute`

The Tool-specific deadline is anchored at gateway entry, before authorization
and lease acquisition. A slow policy decision or lifecycle operation therefore
spends the same budget as remote execution and output validation.

The private `_AttemptAwareExecutor` protocol gives MCP the attempt number without
changing the public Tool executor contract. Ordinary Tool implementations still
receive `execute`; the MCP facade receives `execute_attempt`. Review that this is
used only for observability and stable remote request identity, never to let the
executor choose its own retry.

#### `plugin/lifecycle.py` — `reserve_invocation`

An invocation reservation prevents two concurrent calls with the same Plugin and
idempotency identity from both entering authorization and producing duplicate
side effects. It is deliberately separate from an execution lease:

- the reservation serializes one operation identity across the whole boundary;
- the lease protects an active Plugin implementation from drain and unload.

The reservation must be released on denial, timeout, cancellation, and every
other exit path even when no lease was acquired.

#### `plugin/invocation.py` — `PluginCapabilityInvoker.invoke`

This is the only function that exposes an opaque Plugin implementation to an
operation callback. Its order is security-sensitive:

1. Resolve and bind the exact registration.
2. Verify owner, resource, declared Action, and declared Scope.
3. Reserve the idempotency identity.
4. Run authorization under deadline and cancellation control.
5. Acquire one Plugin lease only after authorization succeeds.
6. Invoke and await cleanup.
7. Release the lease, then release the reservation.

Moving transport acquisition or implementation access before these checks would
be a P0/P1 regression.

#### `provider/_control.py` — `await_provider`

Cancelling the wrapper now cancels and awaits the child task before propagating.
This is what turns "ignore the late result" into an enforceable lifecycle rule:
the transport cannot finish later outside the Plugin lease that originally
protected it.

### 4.2 Transport-neutral public contracts

#### `mcp/model.py`

`McpAdapterDescriptor` is the local allowlist. Its bindings are immutable,
owner-bound, unique, exact-versioned, and non-empty.

`McpToolBinding` joins one local Tool to one remote name and pins both remote
Schema digests. The local Tool descriptor still owns Action, permissions,
side-effect class, timeout, retries, idempotency, and local Schema references.

`McpContextBinding` joins one local ContextProvider to one exact URI. It rejects
templates and wildcards and lists the only Context requirements that may be
returned.

`McpDiscoverySnapshot`, request, and response records contain no JSON-RPC, HTTP,
stdio, header, credential, or SDK values. `McpTransport` exposes only
`discover`, `call_tool`, and `read_resource`, each with `RuntimeCallContext`.

`McpAdapterImplementation` freezes one descriptor with one injected transport.
Construction and cleanup belong to the Plugin loader; Core does not invent a
second transport lifecycle.

Reviewer checkpoint: every `from_data` method requires an exact field set. This
is intentional fixture and protocol strictness, not forward-compatible unknown
field preservation.

### 4.3 Pure binding and discovery checks

#### `mcp/mapping.py`

`canonical_schema_digest` serializes JSON with sorted keys, compact separators,
UTF-8, and no NaN before computing SHA-256. This is a fingerprint comparison,
not a JSON Schema validator.

`validate_tool_binding` and `validate_context_binding` fail locally before a
transport can be acquired. `validate_discovery` runs after acquisition but before
a remote Tool or resource operation. It returns a filtered snapshot containing
only bound capabilities, which is how unknown advertisements are ignored without
becoming usable.

Reviewer checkpoint: a remote Schema match is evidence that the frozen binding
still matches. It must never call `SchemaRegistry.register` or replace the local
Schema reference.

#### `mcp/composition.py`

`validate_mcp_composition` verifies that the adapter and all mapped Tool and
ContextProvider implementations appear in one prepared Plugin capability set.
Identity checks use object identity for the shared adapter instance so a mapped
facade cannot quietly point at another transport.

The Plugin manager runs this validation before registry commit. A failure cleans
up the prepared Plugin and publishes none of its capabilities.

### 4.4 MCP facade implementation

#### `mcp/integration.py` — `_McpClient`

This private class is the only bridge from local facade values to `McpTransport`.
Every business call performs validated discovery first. The transport call is
wrapped by `await_provider`, and non-Core transport exceptions become a safe,
retryable `UNAVAILABLE` error without leaking exception text.

Events carry only adapter/service references, safe transport kind, operation,
counts, attempt, latency, category, and safe code. Tool arguments, outputs,
resource contents, remote metadata, credentials, frames, and exception messages
must never be added to these payloads.

#### `McpToolExecutor`

This is an internal implementation of an ordinary local Tool. Constructor-time
validation proves that its Tool descriptor agrees with its MCP binding. Each
attempt performs exactly one remote Tool call; all retry decisions remain in
`ToolGateway`.

#### `McpContextProviderImplementation`

This is the lease-protected Plugin implementation. It exposes only the frozen
requirements, rejects unbound keys, checks response URI/media type and exact key
set, validates every value against the local `SchemaRegistry`, and enforces the
effective byte budget before returning a normal `ContextResult`.

Token limits are rejected because MCP v1 does not report token usage. Treating
unknown token usage as zero would accidentally broaden the caller's budget.

#### `McpContextProviderFacade`

This is the object registered with `ContextResolver`. It creates a child
idempotency identity for the nested Plugin operation, invokes the same
`PluginCapabilityInvoker` used by local capabilities, and applies only narrowing
grant constraints. It never exposes a direct resource-read method to callers.

### 4.5 Plugin composition hook

`CompositeCapabilityImplementation` in
[`plugin/loader.py`](../../src/congeries_core/plugin/loader.py) is structural and
small on purpose. The Plugin manager invokes it only for a declared
`mcp_adapter`, before publication. Other capability families do not gain a new
general-purpose hook or registration path.

## 5. Test Evidence Map

The shared transport suite parameterizes the same behavior over
`FakeStdioTransport` and `FakeStreamableHttpTransport`. They are independent fake
implementations; neither starts a process, socket, or MCP SDK.

| Evidence | Test location |
| --- | --- |
| Shared Tool, Context, and redacted event behavior | `test_shared_transport_contract_tool_context_and_redacted_events` |
| Strict records and unknown capability filtering | `test_models_round_trip_and_discovery_filters_unknown_capabilities` |
| Atomic Plugin declaration/implementation composition | `test_atomic_composition_rejects_missing_mapped_capability`, `test_plugin_manager_rejects_non_atomic_mcp_composition_and_cleans_up` |
| Missing/changed discovery before business effect | `test_discovery_failures_precede_remote_business_effect` |
| Local validation and default denial with zero transport effect | `test_local_validation_and_default_denial_have_zero_transport_effect` |
| Stable identity and no transport-owned retry | `test_retry_uses_stable_identity_and_transport_never_retries_implicitly` |
| Malformed remote response and local output Schema failure | `test_malformed_and_local_output_failures_are_normalized` |
| Timeout, cancellation, late result cleanup, and drain | `test_timeout_cancellation_late_result_cleanup_and_drain` |
| Context Schema failure releases provider lease | `test_context_schema_failure_releases_provider_lease` |
| Invalid and broadening grants have zero transport effect | `test_invalid_grant_stops_before_transport_acquisition`, `test_scope_broadening_grant_stops_before_transport_acquisition` |

Exact fixture round trips live under [`tests/fixtures/v0.2`](../../tests/fixtures/v0.2)
for the descriptor, discovery records, calls, errors, Actions, and Core events.
The implementation gate recorded by Task 5.3 is 260 tests, 91.22% coverage,
Ruff green, Pyright green, and `git diff --check` green.

## 6. High-risk Review Checklist

Treat a failed item as blocking unless the owning RFC is changed first.

- [ ] No public function invokes `McpTransport` directly; Tool enters through
  `ToolGateway` and Context enters through `ContextResolver`.
- [ ] Descriptor and local binding failures happen before any transport effect.
- [ ] Discovery cannot mutate Plugin, Tool, ContextProvider, or Schema registries.
- [ ] Unknown remote capabilities are ignored and cannot be called.
- [ ] Missing or changed bound capabilities fail before the remote business call.
- [ ] Local Action, permission, Scope, Schema, side-effect class, deadline,
  idempotency, and retry policy remain authoritative.
- [ ] Grant constraints only narrow keys, budgets, timeout, or attempts.
- [ ] Authorization denial and invalid grants cause zero transport effect.
- [ ] One Plugin lease covers discovery, invocation, response normalization, local
  Schema validation, and failure cleanup.
- [ ] Concurrent reuse of one operation identity cannot produce two side effects.
- [ ] Only `ToolGateway` retries, and every attempt keeps the same operation
  identity while exposing an increasing attempt number.
- [ ] Timeout or cancellation cancels and awaits the active transport task; late
  results cannot escape the lease.
- [ ] DRAINING rejects new calls and unload waits for in-flight cleanup.
- [ ] Event payloads contain no arguments, results, resource values, credentials,
  headers, frames, arbitrary server metadata, or exception text.
- [ ] No MCP SDK, HTTP framework, JSON Schema engine, or other production
  dependency was added.

## 7. Explicitly Unimplemented Behavior

Do not request these features as omissions in this review; they are deliberate
scope boundaries:

- Real stdio or Streamable HTTP wire conformance
- MCP SDK integration, OAuth, initialize/session negotiation, or legacy protocol
  revisions
- Prompts, sampling, roots, resource templates, or generic enumeration
- Dynamic publication of discovered remote capabilities
- Server-side publication of local capabilities
- Raw database, table, filesystem, or generic CRUD access
- Agent Tool loops, automatic Skill injection, or Workflow SkillNode/ToolNode
  scheduling
- Parallel or external Workflow engine execution

## 8. Known Follow-up Risks

These are not Task 5.3 contract failures, but they should shape later work:

1. Discovery currently runs for each capability operation. Caching may improve
   performance, but a future design must define snapshot identity, expiry,
   invalidation, revalidation, and drain behavior without dynamic registry
   mutation.
2. Fake transports prove transport neutrality, not wire interoperability. A real
   transport package needs independent framing, header, disconnect, and protocol
   conformance tests.
3. Only protocol revision `2026-07-28` is accepted. Legacy initialize/session
   support needs a separate optional adapter rather than conditionals that weaken
   this v1 contract.
4. Authentication of a concrete remote transport is outside Core authorization.
   A future transport package must keep credentials and headers out of public
   contracts and redacted events.

## 9. Completed Follow-up and Next Work

Tasks 6.2 and 6.3 are complete through RFC-0015, authorized
Workspace/Artifact/StorageProvider boundaries, one shared in-memory and SQLite
contract suite, and exact compatibility fixtures.

The next product slice is Task 4.1 ContextNode. SkillNode and ToolNode follow in
increasing side-effect risk. Agent Tool loops, automatic Skill injection,
parallel scheduling, and external Workflow engines remain separate proposals
with their own review gates.
