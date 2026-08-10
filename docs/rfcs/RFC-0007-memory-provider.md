# RFC-0007: MemoryProvider

- ID: RFC-0007
- Title: MemoryProvider
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0002](../adrs/ADR-0002-memory-provider.md), [RFC-0008](RFC-0008-scope-authorization.md)
- Supersedes: Memory portions of the legacy RFC-0006 draft

## 1. Scope

This RFC defines persistent knowledge operations available across Runs. It does
not define conversation history, memory schemas, ranking, embeddings, or storage
engines.

## 2. Protocol

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

## 3. Types

### MemoryQuery

MemoryQuery contains ScopeRef, query content, namespaced filters, result limit,
optional cursor, and requested projection. Query content is provider-defined but
its schema is declared by capabilities.

### MemoryPage

MemoryPage contains ordered MemoryRecords, an optional next cursor, provenance,
and completeness. Cursors are opaque provider values scoped to the query and
contract version.

### MemoryItem and MemoryRef

MemoryItem contains ScopeRef, typed content, namespaced metadata, provenance,
retention hints, and idempotency identity. MemoryRef contains provider identity,
memory identity, ScopeRef, and version.

### ForgetRequest

ForgetRequest contains MemoryRef or memory identity, the expected ScopeRef, and
an optional expected version. Provider validates identity and Scope together.

### ConsolidateRequest

ConsolidateRequest contains ScopeRef, a provider-supported policy identifier,
deadline, and optional selection criteria. ConsolidationReport records affected
references, skipped records, warnings, and outcome without exposing hidden
provider state.

## 4. Authorization and Isolation

Every operation is authorized under RuntimeCallContext before dispatch. Scope in
the operation must be equal to or narrower than the granted Scope.

Provider-internal authorization may restrict access but cannot broaden it.
Cross-scope retrieval, mutation, deletion, or consolidation is denied and
audited.

Application scope names such as user or organization remain plugin-defined.

## 5. Idempotency and Concurrency

Remember is idempotent for the same provider, ScopeRef, and idempotency key. A
repeated request returns the same logical MemoryRef or a conflict when the
payload does not match.

Forget is idempotent after successful deletion and reports `already_absent`
without disclosing existence outside the authorized Scope.

Expected version prevents lost updates where a provider supports versioned
memory. Version conflict is not silently retried.

## 6. Pagination and Ordering

Provider declares supported ordering and cursor semantics. Core treats cursor as
opaque. Pagination does not allow a caller to escape the original Scope, filter,
or authorization grant.

## 7. Failure Semantics

Outcomes include invalid request, denied, unavailable, timeout, cancelled,
conflict, version mismatch, partial result, and unsupported capability.

Partial retrieval includes returned records and a structured reason. Partial
mutation is not success; the provider reports affected references so policy can
decide compensation or retry.

## 8. Storage Boundary

Plugins may implement memory with relational, vector, graph, object, or other
storage. Core depends only on MemoryProvider. Provider-specific schemas,
transactions, ranking, and embedding behavior do not enter this contract.

## 9. Conformance

A conforming implementation demonstrates:

- The four defined operation names and optional consolidate capability.
- Capability discovery receives RuntimeCallContext and cannot bypass
  authorization.
- Scope validation for every operation.
- Paginated retrieval without Scope drift.
- Idempotent remember and forget behavior.
- Version conflict behavior.
- Structured partial and failure outcomes.
- No conversation-history or concrete storage ownership in Core.
