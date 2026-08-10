# RFC-0007: MemoryProvider

- ID: RFC-0007
- Title: MemoryProvider
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0002](../adrs/ADR-0002-memory-provider.md), [RFC-0008](RFC-0008-scope-authorization.md), [RFC-0010](RFC-0010-runtime-events.md)
- Supersedes: Memory portions of the legacy RFC-0006 draft

## 1. Scope

This RFC defines persistent knowledge operations available across Runs. It does
not define conversation history, memory schemas, ranking, embeddings, storage
engines, automatic retrieval, or automatic persistence. AgentRuntime does not
implicitly invoke this contract.

## 2. Protocol and Registry

MemoryProvider exposes:

```text
retrieve(MemoryQuery, RuntimeCallContext) -> MemoryPage
remember(MemoryItem, RuntimeCallContext) -> MemoryRef
forget(ForgetRequest, RuntimeCallContext) -> ForgetResult
consolidate(ConsolidateRequest, RuntimeCallContext) -> ConsolidationReport
capabilities(RuntimeCallContext) -> MemoryCapabilities
```

`consolidate` is optional and is advertised through capabilities. There is no
MemoryProvider operation named `store`.

MemoryProviderRegistry owns implementation objects and resolves one exact
ProviderId. MemoryGateway is the only Core invocation path. Public query,
request, item, and result values never contain Provider implementations or
storage clients.

Every business operation performs authorized capability discovery before the
operation is validated and dispatched. Capability checks cover supported
operation, contract version, ContentBlock kind, SchemaRef, result limit, and
projection. The gateway uses SchemaRegistry to validate query and item content;
Core does not interpret the content's business meaning.

## 3. Public Types

All public values are immutable and JSON-serializable.

### 3.1 Query and Pagination

MemoryQuery contains ScopeRef, one ContentBlock, one SchemaRef, namespaced JSON
filters, a positive result limit, an ordered projection, and an optional
MemoryCursor.

MemoryCursor contains ProviderId, provider contract version, an opaque cursor
value, and a stable fingerprint of the constrained query. The fingerprint binds
Scope, query content, schema, filters, projection, and limit. A next-page call
must use the same Provider, contract version, Scope, query, schema, filters,
projection, and limit. Drift is an invalid request rejected before Provider
invocation.

MemoryPage contains ProviderId, contract version, ordered MemoryRecords, an
optional next cursor, completeness, warnings, and provenance. A PARTIAL page
preserves returned records and warnings.

### 3.2 Records and Mutation

MemoryRecord contains MemoryRef, one ContentBlock, one SchemaRef, namespaced JSON
metadata, and provenance. MemoryItem contains ScopeRef, one ContentBlock, one
SchemaRef, namespaced JSON metadata, provenance, optional retention references,
and a required idempotency key. The item's key must equal
RuntimeCallContext.idempotency_key.

MemoryRef contains ProviderId, MemoryId, ScopeRef, and a positive version.
ForgetRequest contains MemoryRef, expected ScopeRef, and expected version.
ForgetResult reports `deleted` or `already_absent` without disclosing existence
outside the authorized Scope.

ConsolidateRequest contains ScopeRef, a provider-supported policy ResourceRef,
and namespaced JSON selection. ConsolidationReport contains affected and skipped
MemoryRefs, warnings, and outcome. A PARTIAL report preserves those references
but its `success` property is false.

MemoryCapabilities declares Provider identity, contract version, supported
MemoryOperations, query and item schemas, ContentBlock kinds, maximum result
limit, projections, version support, and consolidation policies.

## 4. Authorization and Constraints

The v0.2 actions, all at version `1`, are:

- `core.memory.capabilities`
- `core.memory.retrieve`
- `core.memory.remember`
- `core.memory.forget`
- `core.memory.consolidate`

Every call passes through AuthorizedDispatcher and the cancellable Provider wait
boundary. The operation Scope must be equal to or narrower than the incoming
RuntimeCallContext Scope. AccessRequest uses the actual operation Scope, so an
explicit narrower-Scope call follows the existing cross-Scope audit contract.

AccessRequest constraints and valid grant narrowing are operation-specific:

- retrieve: SchemaRef is unchanged, filter keys are unchanged, projection may
  only shrink, and limit may only decrease;
- remember: SchemaRef is unchanged, metadata keys may only shrink, and maximum
  content bytes may only decrease;
- forget: MemoryId and expected version are unchanged;
- consolidate: policy ResourceRef and selection keys are unchanged.

Capability discovery constrains no business payload. Unknown, malformed, or
expanding grant constraints are `invalid_grant` and prevent Provider invocation.
Provider-internal authorization may further restrict access but cannot broaden a
Core grant.

## 5. Idempotency and Concurrency

Remember is idempotent for the same Provider, ScopeRef, idempotency key, and
payload. A repeated matching request returns the same logical MemoryRef. Reuse
of that key with a different payload is a conflict.

Forget validates Memory identity, expected Scope, and expected version together.
Repeated deletion returns `already_absent` and does not reveal whether an
identity exists in another Scope. Version conflict is not silently retried.

## 6. Cancellation, Failures, and Results

Core checks deadline and cancellation before and after every Provider await.
Cancellation or deadline closes and cancels the outstanding Provider task; late
results are discarded.

Provider exceptions are normalized as unavailable. Provider identity, Scope,
SchemaRef, cursor, contract version, or result-structure mismatch is a protocol
failure. Other outcomes include invalid request, denied, timeout, cancelled,
conflict, version mismatch, partial result, and unsupported capability.

PARTIAL retrieval remains a typed MemoryPage. PARTIAL consolidation remains a
typed report with `success` false. Remember or forget cannot report partial
success; a partial mutation is returned as a `partial_result` failure with the
safe affected references needed for compensation or retry.

## 7. Runtime Events and Privacy

MemoryGateway emits these OBSERVABILITY event types:

- `core.memory.operation_started`
- `core.memory.operation_completed`
- `core.memory.operation_failed`

Payloads may contain operation, Provider reference, record or affected count,
completeness or outcome, latency, and a safe error code. They never contain the
query, Memory content, metadata, cursor, or provenance content. Observability
delivery failure does not change operation outcome. Authorization denial and
cross-Scope authorization continue to use reliable AUDIT events.

## 8. Storage Boundary

Plugins may implement memory with relational, vector, graph, object, or other
storage. Core depends only on MemoryProvider. Provider-specific schemas,
transactions, ranking, embeddings, and consolidation algorithms do not enter
this contract.

## 9. Conformance

A conforming implementation demonstrates:

- The four defined operation names, optional consolidate capability, and no
  operation named `store`.
- Two independent fake Providers pass the same contract suite.
- Capability discovery and every operation receive RuntimeCallContext and cannot
  bypass authorization.
- Grant narrowing and invalid-grant rejection before Provider invocation.
- Scope validation, cursor drift rejection, and ordered pagination.
- Schema, ContentBlock kind, projection, limit, Provider identity, and version
  validation.
- Idempotent remember and forget behavior and explicit version conflicts.
- Structured partial retrieval and consolidation outcomes.
- Active cancellation, deadline cleanup, and late-result rejection.
- Redacted operation events whose delivery cannot change the operation result.
- Stable v0.2 serialization fixtures and no concrete storage ownership in Core.
