# RFC-0014: MCP Adapter

- ID: RFC-0014
- Title: MCP Adapter
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-12
- Updated: 2026-08-12
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [RFC-0002](RFC-0002-plugin-sdk.md), [RFC-0008](RFC-0008-scope-authorization.md), [RFC-0010](RFC-0010-runtime-events.md), [RFC-0013](RFC-0013-skill-tool-contracts.md)
- Supersedes: None

## 1. Scope

This RFC defines a client-side, transport-neutral adapter for consuming remote
Model Context Protocol capabilities. MCP Tools are explicitly bound to local
Tool v1 capabilities. Exact MCP resource URIs are explicitly bound to local
ContextProvider capabilities. The adapter composes the existing Tool,
ContextProvider, authorization, Schema, Plugin lifecycle, deadline,
cancellation, idempotency, and event contracts; it does not create a second
public invocation path.

Version 1 supports only MCP protocol revision `2026-07-28`. Legacy initialize,
session, SSE compatibility, and negotiation for revisions through `2025-11-25`
are unsupported. The adapter consumes remote capabilities only. It does not
publish local capabilities to a remote peer.

Real wire transports, MCP SDKs, OAuth, prompts, sampling, roots, resource
templates, dynamic capability registration, generic resource enumeration,
Workflow SkillNode/ToolNode scheduling, Agent Tool loops, automatic Skill
injection, and raw database, table, filesystem, or CRUD exposure are outside
this RFC.

## 2. Capability and Service Identity

`McpAdapterDescriptor` is an immutable version 1 contract. Its `CapabilityRef`
uses namespace `core`, kind `mcp_adapter`, exact wire contract version `1`, a
stable adapter identity, and the owning Plugin. The corresponding Plugin
capability declaration uses type `mcp_adapter` and SemVer `1.0.0`.

The descriptor contains a stable `service_id`, the exact protocol revision
`2026-07-28`, and non-empty immutable Tool or Context bindings. Adapter,
service, local capability, remote Tool name, provider, resource URI, and
Context key identities are unique within one descriptor. Identity matching is
exact; aliases, ranges, implicit latest selection, and case folding are invalid.

The owning Plugin publishes the adapter and every mapped `tool` and
`context_provider` declaration in one RFC-0002 registry transaction. Discovery
never mutates the registry. A remote capability is usable only through a local
declaration already visible in the same committed Plugin generation.

## 3. Explicit Bindings

`McpToolBinding` contains an exact local Tool v1 reference, a remote Tool name,
the local input and output `SchemaRef` values, and the expected canonical
SHA-256 digests of the remote input and output JSON Schema documents. The
binding repeats the local Schema identities so descriptor validation can reject
a mismatch before transport acquisition. The local Tool descriptor remains
authoritative for Action, Scope, side-effect class, idempotency, timeout, and
retry policy.

`McpContextBinding` contains one local `ProviderId`, one exact remote resource
URI, a non-empty immutable tuple of supported `ContextRequirement` values, and
the expected canonical SHA-256 digest of the remote resource Schema. Every
returned Context entry uses a bound key and its local `SchemaRef`. Version 1
does not support URI templates, wildcard URIs, arbitrary resource enumeration,
or a direct resource-read gateway.

Schema digests use UTF-8 JSON encoded with sorted object keys, compact
separators, and no ASCII escaping. Boolean values, numbers, arrays, and objects
retain their JSON meaning; non-JSON values are invalid. A digest is lowercase
hexadecimal SHA-256. Remote Schema documents are evidence for matching a frozen
binding only: they are never installed into or substituted for the local
`SchemaRegistry`.

## 4. Discovery Snapshot and Mapping

`McpDiscoverySnapshot` contains the exact protocol revision, stable server
identity and metadata, one discovery identity, immutable remote Tool
descriptors, and immutable exact-URI resource descriptors. Discovery records
are transport-neutral and contain no JSON-RPC, HTTP, stdio, SDK, credential, or
header types.

Descriptor validation is pure and precedes transport acquisition. It verifies
contract versions, kinds, owners, local reference and Schema agreement,
identity uniqueness, exact resource URIs, and canonical digests. Failure is
`INVALID_REQUEST` and produces no transport effect.

After acquisition, a pure mapper validates the discovery snapshot before any
remote business call or local publication. The protocol revision and service
identity must match the descriptor. Every bound Tool or resource must exist
exactly once with the expected Schema digest. Unknown well-formed remote Tools
and resources are ignored. A missing or changed bound capability fails the
snapshot. Malformed discovery is `PROTOCOL_FAILURE`; a supported record with an
unsupported protocol, kind, or Schema is `VERSION_MISMATCH` or
`UNSUPPORTED_CAPABILITY`.

## 5. Transport-neutral Interface

`McpTransport` exposes only asynchronous `discover`, `call_tool`, and
`read_resource` operations. Each operation receives `RuntimeCallContext` and
transport-neutral immutable request and response values. The transport exposes
a stable safe `kind` for observability. It must not retry, authorize, select a
Scope, register a Schema, mutate a registry, or leak wire and SDK objects.

`McpAdapterImplementation` freezes one descriptor and one injected transport.
The Plugin loader owns construction and cleanup of the transport; Plugin unload
must not dispose it before all adapter-backed leases reach zero. A transport
may implement stdio, stateless Streamable HTTP, an in-process fake, or another
wire mechanism without changing the Core contract, provided it preserves the
same observable behavior.

## 6. Tool Composition

An MCP-backed Tool is registered as an ordinary `ToolImplementation`. Its
executor is an internal facade over the adapter and is reachable only through
`ToolGateway`:

```text
ToolGateway
    -> local input Schema validation
    -> AuthorizedDispatcher and grant narrowing
    -> PluginCapabilityInvoker and one Tool lease
    -> MCP-backed ToolExecutor
    -> validated discovery and McpTransport.call_tool
    -> local output Schema validation
    -> release the lease
```

The transport receives the narrowed `RuntimeCallContext`. Every attempt uses
the same idempotency identity. Only `ToolGateway` decides whether to retry;
transport implementations and the MCP facade perform one remote operation per
attempt. Local reference, input Schema, Action, permission, Scope, grant, or
identity failure occurs before transport acquisition or remote side effect.

## 7. Context Composition

An MCP-backed resource is registered as an ordinary `ContextProvider` facade
and is reachable only through `ContextResolver`:

```text
ContextResolver
    -> AuthorizedDispatcher and Context grant narrowing
    -> MCP-backed ContextProvider facade
    -> PluginCapabilityInvoker and one provider lease
    -> validated discovery and McpTransport.read_resource
    -> existing local Schema, budget, completeness, and provenance validation
    -> release the lease
```

Capabilities expose only the binding's local Context requirements. A request
may select only bound requirements and their exact resource URI. The facade
turns a well-formed remote resource value into existing `ContextResult` and
`ContextEntry` contracts; it cannot return unrequested keys. `ContextResolver`
remains authoritative for provider selection, grant narrowing, local Schema
validation, byte and token budgets, completeness, merge behavior, and events.

## 8. Authorization and Constraint Authority

All discovery and invocation effects occur beneath an existing
`AuthorizedDispatcher` operation and within the required Plugin lease. There is
no public MCP call gateway. The local Action, resource, source and effective
Scope, Schema, permission, side-effect class, idempotency identity, and deadline
remain authoritative. Remote metadata or constraints may only narrow an
already authorized call and may never replace, remove, or broaden those values.

Default denial, indeterminate denial, grant identity validation, cross-Scope
audit, and audit failure behavior are inherited from RFC-0008. Adapter-specific
logic never interprets remote authentication as a local authorization grant.

## 9. Deadline, Cancellation, Retry, and Recovery

The same narrowed `RuntimeCallContext` reaches transport acquisition,
discovery, and the remote operation. Deadline expiry and cancellation cancel
the active transport task and await its teardown. A result that arrives after
timeout or cancellation is discarded and cannot be published, validated as
success, or used to mutate Core state.

Tool attempts preserve the caller's stable operation identity. A transport
disconnect may be retryable `UNAVAILABLE`, but only the enclosing Tool policy
may start another attempt. Recovery reuses the original idempotency identity;
it never creates a new side effect identity silently. Context reads do not add
an adapter-owned retry policy.

## 10. Plugin Lifecycle

Discovery and every remote operation borrow the owning Plugin through the
existing RFC-0002 lifecycle controller. The lease begins before the transport
can be acquired and ends only after response shape and local output validation
have completed or cleanup has completed following failure.

DRAINING rejects new discovery and invocation leases. An in-flight call may
finish or be cancelled under its original context. Unload waits for zero active
leases, does not force disposal on timeout, and remains recoverable. Lease
release is required after success, denial after acquisition, malformed response,
disconnect, timeout, cancellation, late completion, or cleanup failure.

## 11. Errors

Adapter errors use the shared structured failure model:

- Unsupported protocol or a changed required Schema is `VERSION_MISMATCH`.
- Unsupported capability kind or required remote feature is
  `UNSUPPORTED_CAPABILITY`.
- Malformed discovery, request, or response shape is `PROTOCOL_FAILURE`.
- Disconnect or unavailable transport is retryable `UNAVAILABLE`.
- Deadline and cancellation are `TIMEOUT` and `CANCELLED`.
- Local descriptor, binding, or call shape failure is `INVALID_REQUEST`.
- Authorization, permission, Scope, or grant failure is `DENIED`.

Safe error codes are stable. Transport exceptions are normalized without
exposing exception text, frames, credentials, headers, or remote payloads.

## 12. Events and Redaction

The adapter emits discovery and invocation started, completed, and failed
observability events. Payloads may contain adapter, service, local capability,
provider, or bound resource references; transport kind; discovery, Tool, or
resource counts; attempt; latency; outcome; and a safe error code and category.

Payloads never contain Tool arguments or results, resource content, Context
values, credentials, authorization headers, environment variables, raw frames,
remote exception text, or arbitrary server metadata. Observability delivery is
best effort and cannot change operation outcome. Authorization and cross-Scope
events remain the reliable audit path defined by RFC-0008.

## 13. Compatibility and Migration

This is a new version 1 capability and does not change Skill v1, Tool v1,
ContextProvider, Plugin, or legacy AgentSpec wire formats. Existing Plugins need
no migration. A Plugin opting in must declare the adapter and every local mapped
capability atomically and supply explicit bindings.

Peers that require initialize, sessions, resumable SSE, or any revision other
than `2026-07-28` are rejected. Supporting a legacy or negotiated protocol needs
a separate optional adapter contract and cannot weaken version 1 validation.

## 14. Conformance

A conforming implementation demonstrates exact fixture round trips for
descriptors, bindings, discovery, actions, errors, and event catalogs. One
shared suite runs against two independent fake transports representing stdio
and stateless Streamable HTTP without starting a process or network listener.

The suite covers protocol and Schema matching; unknown capability filtering;
missing bindings; default denial; invalid grants; Scope escape; zero transport
effect for local validation or authorization failure; malformed output; local
output Schema failure; disconnect; timeout; cancellation; late response and
task cleanup; stable Tool attempt and recovery identity; no implicit transport
retry; acquire/drain linearization; unload during use; lease release; cleanup
failure; and recovery after transport failure.

