# RFC-0015: Storage and Artifact Contracts

- ID: RFC-0015
- Title: Storage and Artifact Contracts
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-12
- Updated: 2026-08-12
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0005](../adrs/ADR-0005-interface-first.md), [ADR-0007](../adrs/ADR-0007-default-deny-scope.md), [ADR-0008](../adrs/ADR-0008-at-least-once-recovery.md), [RFC-0008](RFC-0008-scope-authorization.md), [RFC-0010](RFC-0010-runtime-events.md), [RFC-0011](RFC-0011-checkpoint-recovery.md)
- Supersedes: None

## 1. Scope

This RFC defines the public version 1 contracts for durable Workspace state and
immutable Artifact content. It defines repository operations, Provider
registration, authorized dispatch, compare-and-set, pagination, errors, wire
compatibility, and the in-memory and SQLite reference adapters.

The contract does not select a mandatory database. It does not change existing
RunRepository, SessionRepository, CheckpointStore, or Workflow behavior. Artifact
updates, deletion, garbage collection, retention, and migration are outside
version 1 because Checkpoints and Workflows may hold durable Artifact references.

## 2. Ownership and Boundaries

Core owns transport-neutral values, repository protocols, StorageProvider,
StorageProviderRegistry, StorageGateway, and normalized errors. Adapters own
transactions, connection management, database schemas, and infrastructure error
translation. Plugins may supply StorageProvider implementations.

All operations are asynchronous and receive RuntimeCallContext. StorageGateway
is the public dispatch boundary and composes StorageProviderRegistry with the
shared AuthorizedDispatcher. A database client or adapter-native type cannot
appear in a public contract.

## 3. Identity and Versioning

The following identities remain opaque typed values:

- ProviderId identifies one registered StorageProvider.
- WorkspaceId identifies one Workspace within a Provider.
- ArtifactId is supplied by the caller and is stable within a Provider.
- ScopeRef defines the authorization and isolation boundary.

Every public wire value has string `contract_version: "1"`. Decoders reject
unknown fields, missing fields, wrong scalar types, and unsupported versions.
Version numbers are strings so JSON number coercion cannot hide a contract
change.

## 4. Workspace Contract

WorkspaceState contains:

| Field | Meaning |
| --- | --- |
| `workspace_id` | Stable Workspace identity |
| `scope` | Owning ScopeRef |
| `state_version` | Non-negative compare-and-set version |
| `values` | Immutable JSON object controlled by the caller |
| `artifact_refs` | Ordered immutable ArtifactId references |

WorkspaceState uses a strict version 1 wire codec. Create requires
`state_version == 0`. WorkspaceRepository exposes:

```text
create_workspace(workspace, context) -> WorkspaceState
get_workspace(workspace_id, scope, context) -> WorkspaceState
compare_and_set_workspace(workspace, expected_version, context) -> WorkspaceState
```

Compare-and-set succeeds only when the stored version equals
`expected_version`, the replacement version is exactly
`expected_version + 1`, and Workspace identity and Scope remain unchanged. A
stale comparison is retryable conflict. Create is not an upsert.

Artifact writes do not mutate WorkspaceState implicitly. A caller that wants to
publish an ArtifactId in `artifact_refs` performs an explicit Workspace CAS so
that state ownership and concurrency remain visible.

## 5. Artifact Contract

### 5.1 ArtifactRecord and ArtifactValue

ArtifactRecord contains ProviderId, ArtifactId, WorkspaceId, ScopeRef, media
type, byte length, lowercase SHA-256, UTC creation time, optional name, and JSON
metadata. ArtifactValue combines the record with exact bytes. Its wire codec
uses canonical base64 in `content_base64`; invalid base64, non-canonical
encodings, length mismatch, and digest mismatch are rejected.

ArtifactReference contains only contract version, ProviderId, ArtifactId,
WorkspaceId, ScopeRef, and SHA-256. It can be stored safely in Checkpoints or
Workflow output references without embedding content or business metadata.

Artifact content is immutable in version 1:

- The caller supplies a stable ArtifactId.
- Repeating `put` with the same complete record identity and digest is
  idempotent and returns the existing record.
- Reusing the ArtifactId for different content or record identity is conflict.
- Content update, deletion, and garbage collection do not exist.

### 5.2 Repository Operations

ArtifactRepository exposes:

```text
put_artifact(value, context) -> ArtifactRecord
get_artifact(artifact_id, workspace_id, scope, context) -> ArtifactValue
list_artifacts(query, context) -> ArtifactPage
```

`put` requires an existing Workspace whose Scope contains the Artifact Scope,
verifies Provider identity, byte length, digest, and Provider byte limit, and
commits record and content atomically.

`get` requires exact Artifact, Workspace, and Scope identity. Not-found responses
do not disclose data from another Scope.

### 5.3 Query, Cursor, and Page

ArtifactQuery contains ProviderId, WorkspaceId, ScopeRef, positive limit, and an
optional ArtifactCursor. Results are ordered by `(created_at DESC,
artifact_id DESC)`. This total order is stable across Provider restart.

ArtifactCursor binds Provider, Workspace, Scope, limit, query fingerprint, and
the last `(created_at, artifact_id)` boundary. Reusing it with any changed query
component is `artifact_cursor_drift`. A page contains ArtifactRecord values and
an optional next cursor; Artifact bytes are never included in list results.

Version 1 pagination is snapshot-independent keyset pagination. Concurrent
inserts newer than a cursor do not reorder already traversed results. This RFC
does not promise a database snapshot spanning requests.

## 6. StorageProvider and Capabilities

StorageProvider composes WorkspaceRepository and ArtifactRepository and adds:

```text
capabilities(context) -> StorageCapabilities
```

StorageCapabilities identifies the Provider, its maximum Artifact byte count,
required Workspace CAS support, required Artifact pagination support, and
contract version. Version 1 requires both capabilities.

StorageProviderRegistry rejects duplicate ProviderId registration and returns a
retryable unavailable error for missing Providers. Registration is dependency
injection; Core does not discover databases or instantiate application storage.

## 7. Authorization

The version 1 action catalog is:

- `core.storage.capabilities`
- `core.storage.workspace.create`
- `core.storage.workspace.get`
- `core.storage.workspace.compare_and_set`
- `core.storage.artifact.put`
- `core.storage.artifact.get`
- `core.storage.artifact.list`

All actions use version `1` and pass through AuthorizedDispatcher. The resource
is the selected StorageProvider and every request carries the requested Scope.
Missing policy, missing grant, indeterminate policy, unknown action, expired
grant, and invalid grant are denied before Provider invocation.

Grants cannot rewrite ProviderId, WorkspaceId, ArtifactId, digest, byte length,
expected Workspace version, or new Workspace version. An Artifact list grant may
only reduce `limit`; it cannot change Workspace identity or reuse a cursor at a
different limit. Effective Scope must equal or narrow requested Scope. Any
cross-Scope use requires the explicit audited grant defined by RFC-0008.

Provider implementations enforce RuntimeCallContext Workspace identity and
effective Scope again as defense in depth. They may narrow access but cannot
broaden the Core grant.

## 8. Deadline, Cancellation, and Idempotency

StorageGateway checks deadline and cancellation before dispatch, actively
cancels outstanding Provider work, and checks them again after await. A late
Provider result is discarded and cannot be converted to success.

Workspace create, Workspace CAS, and Artifact put have deterministic
idempotency semantics at the repository boundary. ArtifactId plus complete
record identity and digest protects at-least-once replay. CAS serializes
competing writers and admits one winner for one expected version. Adapters must
roll back the whole operation on error.

## 9. Errors

Implementations map failures to CoreError and use at least these stable codes:

| Category | Codes |
| --- | --- |
| invalid request | `workspace_not_found`, `artifact_not_found`, `storage_provider_mismatch`, `artifact_too_large` |
| denied | `workspace_context_mismatch`, `workspace_scope_mismatch`, `artifact_scope_mismatch`, `artifact_identity_mismatch`, `invalid_grant` |
| conflict | `storage_provider_already_registered`, `invalid_initial_workspace_version`, `workspace_already_exists`, `stale_state_version`, `invalid_next_state_version`, `artifact_identity_conflict`, `artifact_cursor_drift` |
| unavailable | `storage_provider_not_registered`, `storage_provider_failure`, `storage_backend_unavailable` |
| protocol failure | `storage_protocol_failure` |

Database exception text, SQL, paths, credentials, Artifact bytes, and metadata do
not cross the normalized error boundary.

## 10. Events and Privacy

StorageGateway emits non-blocking observability events:

- `core.storage.operation_started`
- `core.storage.operation_completed`
- `core.storage.operation_failed`

Payloads contain only operation, Provider reference, record count, byte count,
latency, outcome, and safe structured error code/category. They never contain
Artifact bytes, base64, business metadata, Workspace values, database paths,
SQL, or raw exception text. Durable storage state remains authoritative; these
events are not Event Sourcing.

## 11. Reference Adapters

The thread-safe in-memory adapter uses a process-local lock and deterministic
ordering. It is a reference and test implementation, not durable storage.

The SQLite reference adapter uses only Python's standard `sqlite3` module. It
enables WAL, foreign keys, a busy timeout, and explicit transactions. Workspace
CAS and Artifact put use transactional writes. Reopening the database preserves
Workspace state, Artifact bytes, integrity fields, and pagination order.

SQLite remains optional. A conforming Provider may use another database or
service if it passes the same public contract suite and exposes no vendor type.

## 12. Compatibility and Migration

Exact version 1 fixtures cover Storage actions, WorkspaceState,
StorageCapabilities, ArtifactValue, ArtifactReference, ArtifactQuery,
ArtifactCursor through its containing query/page, ArtifactPage, the Provider
action catalog, and the Core event catalog.

This is the first public Storage wire contract, so no historical migration
fixture is invented. A future version 2 must add explicit migration guidance and
fixtures before changing any version 1 decoding or persistence behavior. A
future retention or deletion contract must define how durable Checkpoint and
Workflow references remain valid or migrate.

## 13. Conformance

A conforming implementation demonstrates:

- Strict versioned wire decoding, canonical base64, length, and SHA-256 checks.
- Workspace create/get/CAS and one winner under concurrent stale writers.
- Immutable idempotent Artifact writes and conflicting-content rejection.
- Deterministic keyset pagination, cursor drift rejection, and Scope isolation.
- Default denial, unknown action denial, grant narrowing, and no Provider bypass.
- Deadline, cancellation, late-result discard, and Provider protocol validation.
- Transaction rollback and durable reopen behavior for the SQLite adapter.
- Redacted events without content, metadata, Workspace values, or backend detail.
- One parameterized contract suite over in-memory and SQLite implementations.
- Exact compatibility fixtures and green pytest, coverage, Ruff, Pyright, and
  whitespace gates.

## 14. Implemented Coverage

The implementation includes strict public models and WorkspaceState codec,
StorageProviderRegistry, StorageGateway, the in-memory Provider, the SQLite
reference Provider, normalized events and schemas, a shared dual-adapter suite,
authorization and failure-path tests, restart and transaction checks, and exact
version 1 compatibility fixtures. Workflow node execution is unchanged.
