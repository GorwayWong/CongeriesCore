# Storage v1 Code Review Guide

- Reviewed Slice: Task 6.2 Storage Abstractions and Task 6.3 compatibility work
- Contract: [RFC-0015](../rfcs/RFC-0015-storage-artifact-contracts.md)
- Status: Implemented in 0.2.0
- Updated: 2026-08-12

## 1. What changed in one sentence

Core now has one authorized, provider-neutral contract for versioned Workspace
state and immutable Artifact bytes, with matching in-memory and SQLite
implementations and exact public wire fixtures.

No Workflow node, Run repository, Session repository, Checkpoint behavior, or
business model changed in this slice.

## 2. Mental model before reading code

| Concept | Plain-language meaning | Safety rule |
| --- | --- | --- |
| WorkspaceState | A shared notebook with a revision number | A writer may replace only the exact version it read |
| ArtifactRecord | The immutable label on a sealed package | ID, owner, byte count, digest, and creation time never change |
| ArtifactValue | The label plus exact bytes | Length and SHA-256 must match before storage |
| ArtifactReference | A durable claim ticket | Safe for Checkpoints because it excludes bytes and metadata |
| ArtifactCursor | A bookmark in one exact scoped list | It cannot be reused for another Provider, Workspace, Scope, or limit |
| StorageGateway | The guarded front desk | Every action is authorized and every Provider result is checked |
| StorageProvider | A replaceable filing system | Backend types and exceptions stop at the adapter boundary |

The key ordering rule is:

```text
validate caller-owned identity
    -> authorize action and Scope
    -> validate grant constraints
    -> invoke Provider with effective RuntimeCallContext
    -> validate Provider result
    -> emit redacted outcome
```

Review any path that changes this order as a security- or protocol-sensitive
change.

## 3. Recommended review order

### 3.1 Public state and wire boundary

Start with [`WorkspaceState`](../../src/congeries_core/state/workspace.py).

Review:

1. `CONTRACT_VERSION` is the string `"1"`.
2. `to_data()` emits every field in one stable shape.
3. `from_data()` rejects missing, extra, and unsupported-version fields.
4. `update()` requires the caller's expected version and increments exactly once.
5. `artifact_refs` contains identities, never Artifact bytes.

Then read these values in
[`provider/storage.py`](../../src/congeries_core/provider/storage.py):

- `StorageCapabilities`
- `ArtifactRecord`
- `ArtifactValue`
- `ArtifactReference`
- `ArtifactCursor`
- `ArtifactQuery`
- `ArtifactPage`

The important distinction is that `ArtifactRecord` and list pages are metadata,
while only `ArtifactValue` carries bytes. Its decoder validates base64 syntax,
canonical encoding, byte length, and SHA-256 before a Provider sees the value.

### 3.2 Interfaces and dependency injection

Continue in `provider/storage.py` with:

- `WorkspaceRepository`
- `ArtifactRepository`
- `StorageProvider`
- `StorageProviderRegistry`

These are intentionally small protocols. Registry lookup is dependency
injection, not database discovery. Duplicate registration is conflict; a missing
Provider is retryable unavailable. No SQLite or application type appears here.

### 3.3 Authorization and protocol boundary

Read `StorageGateway` before either implementation. It is the semantic center of
the slice.

For every method, verify four things:

1. The correct version 1 ActionRef is used.
2. AccessRequest names the selected StorageProvider and requested Scope.
3. Identity-bearing constraints cannot be changed by the grant.
4. The returned type and identity are validated before the caller receives it.

The seven actions are:

| Operation | Action | Immutable constraints |
| --- | --- | --- |
| capabilities | `core.storage.capabilities` | ProviderId |
| Workspace create | `core.storage.workspace.create` | WorkspaceId, initial version |
| Workspace get | `core.storage.workspace.get` | WorkspaceId |
| Workspace CAS | `core.storage.workspace.compare_and_set` | WorkspaceId, expected version, new version |
| Artifact put | `core.storage.artifact.put` | WorkspaceId, ArtifactId, SHA-256, byte length |
| Artifact get | `core.storage.artifact.get` | WorkspaceId, ArtifactId |
| Artifact list | `core.storage.artifact.list` | WorkspaceId; limit may only shrink on the first page |

`_dispatch()` handles operations whose grant constraints must remain exactly
equal. `list_artifacts()` is separate because list limit is the one permitted
narrowing. `_constrain_query()` refuses unknown fields, Workspace changes,
limit expansion, and limit drift once a cursor exists.

`_invoke()` wraps unknown Provider exceptions as retryable unavailable errors.
CoreError passes through unchanged. `await_provider()` supplies pre/post deadline
and cancellation checks and waits for Provider task cleanup, preventing a late
result from becoming a successful Core result.

### 3.4 In-memory semantic reference

Read `InMemoryStorageProvider` next. This is the easiest implementation against
which to judge the SQLite adapter.

Workspace rules:

- Create accepts only version 0 and never upserts.
- Get requires exact stored Scope.
- CAS checks identity, Scope, stored expected version, and an exact `+1` new
  version inside one lock.
- Competing writers using the same expected version have one winner.

Artifact rules:

- The owning Workspace must exist.
- Artifact Scope must equal or descend from Workspace Scope.
- First put stores the value.
- Repeating the exact same value is idempotent success.
- Reusing the ID for any different record or bytes is conflict.
- Get requires exact Workspace and Artifact Scope.
- List returns records only and filters to one exact Scope.

Pagination sorts by `(created_at DESC, artifact_id DESC)`. ArtifactId is the
tie-breaker, so equal timestamps still have a deterministic total order. The
cursor records the last returned pair; the next page uses values strictly below
that pair.

### 3.5 SQLite durable reference

Read
[`adapter/sqlite_storage.py`](../../src/congeries_core/adapter/sqlite_storage.py)
after the in-memory implementation.

The public async methods validate the same caller-owned values, lazily initialize
the schema, and move synchronous sqlite3 work to a worker thread. Each operation
opens its own connection with:

- WAL for concurrent readers and one writer
- foreign keys for Workspace ownership
- a bounded busy timeout for lock contention
- explicit transaction control

Review the transaction boundaries carefully:

#### Workspace CAS

```text
BEGIN IMMEDIATE
    -> read stored Scope and version
    -> reject stale or invalid next version
    -> UPDATE ... WHERE state_version = expected
    -> require rowcount == 1
    -> COMMIT
```

The early read gives a precise error. The conditional UPDATE is the final race
check. Any rejection rolls back and preserves the previous version.

#### Artifact put

```text
BEGIN IMMEDIATE
    -> require owning Workspace
    -> require Artifact Scope within Workspace Scope
    -> read existing ArtifactId
       -> exact value: idempotent COMMIT
       -> different value: ROLLBACK + conflict
    -> insert metadata and bytes together
    -> COMMIT
```

The BLOB and record become visible together. There is no update or delete SQL in
version 1.

#### Artifact list

The composite index matches Workspace, Scope, creation time descending, and
ArtifactId descending. The query requests `limit + 1` rows: the extra row says a
next cursor is needed without an offset scan or count query. Database reopen does
not change ordering.

SQLite errors are normalized as `storage_backend_unavailable`; SQL text,
database paths, and exception text do not cross the adapter boundary.

### 3.6 Events, exports, and compatibility

Review:

- [`event/model.py`](../../src/congeries_core/event/model.py) for the three event
  names.
- [`event/schema.py`](../../src/congeries_core/event/schema.py) for safe payload
  fields.
- [`provider/__init__.py`](../../src/congeries_core/provider/__init__.py) and
  [`adapter/__init__.py`](../../src/congeries_core/adapter/__init__.py) for public
  exports.
- [`storage.json`](../../tests/fixtures/v0.2/storage.json) and
  [`storage_actions.json`](../../tests/fixtures/v0.2/storage_actions.json) for the
  exact version 1 wire surface.

Storage events may contain operation, Provider reference, counts, byte count,
latency, and safe error code/category. They must not contain Artifact bytes,
base64, metadata, Workspace values, SQL, backend path, credentials, or exception
text. Event sink failure is non-blocking because these are observability events,
not required authorization audit acknowledgements.

## 4. End-to-end operation traces

### 4.1 Workspace compare-and-set

```text
caller supplies Workspace(version N+1) and expected N
    -> Gateway checks RuntimeCallContext Workspace and Scope
    -> AuthorizedDispatcher evaluates exact Provider/action/resource/constraints
    -> grant may narrow Scope but may not rewrite versions or WorkspaceId
    -> Provider atomically compares stored N and writes N+1
    -> Gateway verifies the Provider returned the exact candidate Workspace
    -> redacted completed event
```

Reviewer question: can any path write a version without comparing the caller's
expected version? The answer should remain no.

### 4.2 Artifact put and replay

```text
caller builds record + bytes
    -> ArtifactValue checks length and SHA-256
    -> Gateway freezes WorkspaceId, ArtifactId, digest, and byte length in grant
    -> Provider checks Workspace ownership and Scope
    -> no existing ID: atomically store
    -> exact existing value: return success
    -> different existing value: conflict
    -> Gateway requires the exact record back
```

Reviewer question: can the same ArtifactId silently become different bytes? The
answer should remain no.

### 4.3 Artifact list and cursor

```text
caller requests Provider + Workspace + Scope + limit
    -> policy may lower first-page limit
    -> Provider returns records in strict descending key order
    -> Gateway verifies count, identity, Scope, order, and cursor
    -> next request must carry the same bound query and limit
```

Reviewer question: can a cursor from one Scope or limit be reused in another?
The answer should remain no.

## 5. Failure and privacy review map

| Risk | Guard |
| --- | --- |
| Lost Workspace update | versioned CAS under lock/transaction plus conditional UPDATE |
| Duplicate side effect on replay | exact ArtifactValue equality under stable ArtifactId |
| Artifact ID overwrite | `artifact_identity_conflict`; no update API or SQL |
| Scope escape | RuntimeCallContext check, AuthorizedDispatcher, Provider ownership check, page validation |
| Grant rewrites identity | exact constraint comparison; list has a dedicated narrowing validator |
| Cursor reused elsewhere | Provider/Workspace/Scope/limit/fingerprint validation |
| Malformed Provider result | Gateway type, identity, order, and cardinality validation |
| Event leaks content | fixed safe payload schemas and redacted failure fields |
| SQLite partial write | explicit transaction rollback before visibility |
| Backend lock or failure | bounded wait and normalized retryable unavailable error |

## 6. Test cross-reference

The primary suite is
[`test_provider_storage.py`](../../tests/test_provider_storage.py).

| Test | What it proves |
| --- | --- |
| `test_storage_models_round_trip_and_validate_strictly` | versions, canonical base64, length/digest, actions, cursor/page codecs |
| `test_two_storage_providers_pass_workspace_and_artifact_contract` | the same Workspace/Artifact semantics for in-memory and SQLite |
| `test_storage_compare_and_set_has_one_concurrent_winner` | one CAS winner under competing writers |
| `test_sqlite_storage_persists_across_provider_restart_and_rolls_back` | durable reopen and no orphan write after failure |
| `test_storage_gateway_authorizes_narrows_and_emits_redacted_events` | allowed dispatch, list-limit narrowing, and safe events |
| `test_storage_gateway_rejects_invalid_grant_before_provider_read` | grant identity rewriting stops before Provider access |
| `test_storage_default_denial_unknown_action_and_pre_cancel_have_no_call` | default denial and zero Provider effect before dispatch |
| `test_storage_running_cancellation_deadline_and_protocol_failure` | active cleanup, timeout, cancellation, and malformed result rejection |
| `test_storage_provider_registry_conflicts_and_missing_provider` | deterministic registration failures |

[`test_compatibility_fixtures.py`](../../tests/test_compatibility_fixtures.py)
checks byte-exact decode/equality/re-encode behavior for Storage contracts, action
catalogs, and Core event names.

## 7. Deliberate exclusions

Do not request these as fixes for this review; each needs a separate contract:

- Artifact update, deletion, garbage collection, or retention
- automatic mutation of Workspace `artifact_refs` during Artifact put
- a mandatory database or migration framework
- direct Workflow use of SQLite or StorageProvider
- Workflow ContextNode, SkillNode, or ToolNode execution
- parallel Workflow scheduling or external engine adapters

The absence of automatic `artifact_refs` mutation is intentional. Artifact put
and Workspace publication are separate visible operations so a caller chooses
the CAS boundary and handles conflicts explicitly.

## 8. Reviewer checklist

- [ ] Public values reject unsupported contract versions and malformed integrity
      fields before Provider invocation.
- [ ] Artifact bytes appear only in ArtifactValue and SQLite BLOB storage, never
      in list pages, references, events, or Checkpoints.
- [ ] Every Gateway operation uses the intended versioned action.
- [ ] Grants cannot rewrite identity, digest, byte length, or Workspace versions.
- [ ] Scope can narrow but cannot broaden or cross without the shared audited
      authorization path.
- [ ] In-memory and SQLite implementations have equivalent observable behavior.
- [ ] Workspace CAS admits one writer for one expected version.
- [ ] Exact Artifact replay succeeds; changed content under the ID conflicts.
- [ ] Pagination is deterministic and cursor-bound across restart.
- [ ] SQLite writes are transactional and backend errors are normalized.
- [ ] Provider results are checked before events or caller success.
- [ ] Events contain no content, metadata, Workspace values, SQL, paths, or raw
      exception detail.
- [ ] Existing Run, Session, Checkpoint, and Workflow behavior is unchanged.
- [ ] Full pytest coverage, Ruff, Pyright, and whitespace gates are green.

## 9. Recommended next milestone

Implement Workflow ContextNode next. It is the lowest-side-effect remaining Core
node and can compose the existing authorized ContextResolver. Freeze its typed
config and result codec first, validate exact Provider permissions before
execution, persist the resolved value before the stable Checkpoint, and prove
recovery skips committed resolution.

SkillNode and ToolNode should follow only after ContextNode demonstrates that the
Workflow output-persistence and recovery boundary works for a non-Agent value.
Agent Tool loops, automatic Skill injection, parallel scheduling, and external
Workflow engines remain separate proposals.
