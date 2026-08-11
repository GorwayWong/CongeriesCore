# RFC-0002: Plugin SDK

- ID: RFC-0002
- Title: Plugin SDK
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-11
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0009](../adrs/ADR-0009-safe-plugin-unload.md)
- Supersedes: None

## 1. Scope

This RFC defines plugin packaging, manifest validation, capability registration,
execution leases, drain, and unload. It does not define business capability or
concrete plugin loading technology.

### 1.1 Plain-language overview

A Plugin is removable code that lends capabilities to Core. Core must be able to
say exactly what the Plugin offers, decide whether it is safe to use, and later
remove it without pulling resources out from under a running call.

The implementation follows six simple rules:

1. Check the Plugin's "identity card" before running loader code. A malformed
   manifest, unsupported Core version, unavailable dependency, or impossible
   permission request stops here and causes no side effect.
2. Resolve dependencies from one frozen catalog view. The same inputs always
   produce the same activation order or the same error, regardless of input
   order.
3. Publish all capabilities with one registry switch. Reviewers and callers can
   see either none of a Plugin's capabilities or all of them, never a half-loaded
   Plugin.
4. Treat every capability call as borrowing a lease. The lease exists only while
   the Plugin is ACTIVE and is returned even when the call fails, times out, or
   is cancelled.
5. Unload by closing the door first. DRAINING rejects new leases, waits for
   existing borrowers, unregisters capabilities, and only then releases loader
   resources.
6. Stop in the last safe state when work fails. A registration failure returns to
   LOADED, an activation failure stays REGISTERED, a drain timeout stays
   DRAINING, and an unload-hook failure stays UNREGISTERED so the command can be
   retried without repeating completed work.

```text
validate -> load privately -> publish atomically -> ACTIVE
                                               |
                                               v
                              reject new work in DRAINING
                                               |
                                      wait for leases = 0
                                               |
                                  unregister -> clean up
```

## 2. Package Layout

```text
plugin/
├── manifest.yaml
├── workflows/
├── skills/
├── tools/
├── providers/
└── mcp/
```

Directories are optional when the manifest declares no capability of that type.
Undeclared files do not create registered capability.

## 3. Manifest v1

Core validates the decoded JSON-compatible manifest value. Reading YAML,
resolving files, and importing code belong to a loader and must not add a
mandatory parser or import technology to Core.

The v1 manifest rejects unknown fields and contains:

| Field | Requirement |
| --- | --- |
| `contract_version` | Exact manifest contract version; v1 is `1` |
| `name` | Stable namespaced plugin identifier and Plugin identity |
| `version` | Plugin semantic version |
| `core_api` | Compatible Core contract version range |
| `entrypoint` | Loader-specific entry reference |
| `provides` | Versioned capability identifiers by type |
| `requires` | Plugin or Core capability dependencies |
| `permissions` | Requested actions and Scope patterns |
| `lifecycle` | Optional hook capability declarations |

Plugin versions use canonical SemVer 2.0. Version ranges support an exact version
or a comma-separated conjunction of `=`, `>`, `>=`, `<`, and `<=` comparators.
V1 rejects disjunction, caret, tilde, wildcard, optional dependencies, implicit
latest selection, and multiple candidate or active versions for one Plugin
identity.

Permission Scope patterns use `namespace:kind:id`. The identifier may be the
single wildcard `*`; partial wildcards are invalid. A permission matches the
effective Scope or one of its ancestors, so a Workspace declaration can bound a
child Run without granting access to another Workspace.

`provides` contains discriminated declarations with capability type, stable
identifier, contract version, loader-owned entry, and permissions. `requires`
contains either a Plugin identity and version range or a capability type,
identifier, and contract range. Lifecycle hooks are declared explicitly; loaders
must not discover hidden hooks.

Validation has three separate boundaries:

1. `ManifestValidator` is pure. It validates fields, identifiers, versions,
   ranges, local duplicates, capability types, permission shapes, and hook names.
   It does not call a loader, registry, policy, or event sink.
2. Plugin preflight checks Core compatibility, a fixed catalog snapshot,
   dependency resolution, permission representability, and registration
   collisions. It still performs no load or registration effect.
3. Lifecycle authorization decides whether the caller may perform load,
   activation, drain, cancel-drain, and unload effects.

Malformed manifests fail with `invalid_manifest`. Preflight failure produces no
load, registration, lifecycle, or event effect.

## 4. Dependency Resolution

Dependency resolution consumes candidate manifests and an immutable catalog
snapshot. It does not discover, download, or load Plugins. Direct and transitive
requirements must be compatible before registration.

The resolver uses a stable Kahn topological sort. Ready Plugins are ordered by
Plugin identity and canonical version; capability references are ordered by
type, identity, and contract version. Every permutation of the same input set
must return the same plan or the same structured error. Missing, incompatible,
ambiguous, and cyclic dependencies are distinct failures. Cycle references are
reported in stable order.

## 5. Capability Types and Atomic Registration

A plugin may provide:

- Workflows
- Skills
- Tools
- ContextProviders
- MemoryProviders
- ModelProviders
- StorageProviders
- MCP adapters

Each capability has a stable identifier, contract version, implementation entry,
and declared permissions. Registration collisions return a structured conflict.

Registration is published through one ownership-aware transaction boundary.
Readers observe only immutable committed snapshots. A transaction stages every
loaded handle, validates every collision and owner, and publishes the complete
set with one compare-and-set snapshot replacement. Preparation failure publishes
nothing. A stable registration receipt identifies the Plugin, registry version,
registration generation, and complete owned capability set. Rollback and
receipt-based unregistration are idempotent, reject ownership mismatch, and
cannot remove a later generation registered by the same owner. A caller must not
compose atomic Plugin registration by directly writing several independent typed
registries.

## 6. Lifecycle

The lifecycle states are:

```text
DISCOVERED -> VALIDATED -> LOADED -> REGISTERED -> ACTIVE
ACTIVE -> DRAINING -> UNREGISTERED -> UNLOADED
```

`Execute` is behavior while ACTIVE and is not a lifecycle state.

Every lifecycle record carries a monotonic state version. Coordinators use an
expected version compare-and-set when committing a transition; stale writers
fail without changing state.

| Transition | Required behavior |
| --- | --- |
| DISCOVERED -> VALIDATED | Parse manifest and verify compatibility, dependencies, schemas, and permission declarations |
| VALIDATED -> LOADED | Resolve the entrypoint without publishing capability |
| LOADED -> REGISTERED | Atomically publish capability registrations or publish none |
| REGISTERED -> ACTIVE | Allow new execution leases |
| ACTIVE -> DRAINING | Reject new leases and retain existing leases |
| DRAINING -> UNREGISTERED | Proceed only after the active lease count reaches zero |
| UNREGISTERED -> UNLOADED | Run disposal hooks and release plugin-owned resources |

Activation failure leaves the Plugin REGISTERED. It may retry activation or use
the recovery transition REGISTERED -> UNREGISTERED before cleanup. Registration
failure leaves the Plugin LOADED and the committed registry snapshot unchanged.

Invalid transitions return a lifecycle conflict and do not change state.

Every lifecycle operation has a stable operation identity, state version, and
recorded successful hook progress. A successful hook is not repeated. A failed
hook may be retried with the same idempotency identity. Hooks must be idempotent;
Core does not promise exactly-once external side effects.

## 7. Execution Leases

Every plugin capability invocation obtains a lease before dispatch. A lease:

- Identifies the Plugin, activation epoch, capability, Run, and invocation.
- Is created only while the plugin is ACTIVE.
- Is released after success, failure, timeout, or cancellation cleanup.
- Prevents unregistration and disposal while active.

Lease acquisition and release are idempotent for the same invocation identity.
Reusing an identity with different attributes is a conflict. Acquisition and
ACTIVE -> DRAINING are linearized at the same per-Plugin concurrency boundary:
either the lease is acquired first and drain waits for it, or drain wins and the
new acquisition fails with `plugin_draining`. Release is Core cleanup and must
not be skipped because the caller cancellation token is already cancelled.

## 8. Drain and Unload

Drain is initiated only from ACTIVE.

1. Atomically enter DRAINING.
2. Reject new lease acquisition with `plugin_draining`.
3. Wait for active leases to reach zero.
4. Atomically unregister all capabilities.
5. Run optional unload hooks.
6. Release resources and enter UNLOADED.

Unload is idempotent. Repeating unload after UNLOADED succeeds without running
disposal twice.

A drain timeout leaves the plugin in DRAINING. Core does not force disposal.
An operator may retry drain or explicitly cancel it. Cancel drain returns to
ACTIVE only if capabilities are still registered and disposal has not started.

An unload-hook failure leaves the plugin UNREGISTERED but not UNLOADED and
returns a structured cleanup error. Retrying unload resumes cleanup.

Deadline or cancellation while waiting for leases leaves the Plugin DRAINING.
`on_drain` failure also leaves it DRAINING. Cancel-drain returns to ACTIVE only
while capabilities remain registered and disposal has not started. Repeating a
successful unload returns success without rerunning unregister or cleanup.

V1 recovery covers registration rollback, drain deadline or cancellation, hook
failure, and repeated lifecycle commands. It does not claim that an active lease
survives process termination; that requires a durable lease store, stale-lease
reconciliation, and a later storage contract.

## 9. Lifecycle Hooks

Optional hooks are:

- `on_load`
- `on_activate`
- `on_drain`
- `on_unload`

Hooks receive RuntimeCallContext or a lifecycle-specific equivalent with Scope,
deadline, cancellation, trace, and audit correlation. Hook presence is declared
in the manifest; loaders do not discover hidden hooks.

## 10. Authorization and Events

Permission requests are evaluated before activation with a Plugin principal and
the activation context. Capability invocation is evaluated again with the caller
RuntimeCallContext; its ActionRef and effective Scope must match the capability's
permission declaration.
The v1 lifecycle action catalog is `core.plugin.load`, `core.plugin.activate`,
`core.plugin.drain`, `core.plugin.cancel_drain`, and `core.plugin.unload`, all at
action version `1`.

Scope may only narrow, deadline may only shorten, and Run, trace, cancellation,
correlation, and idempotency data propagate through lifecycle hooks. Deadline and
cancellation are checked before and after external awaits. Late hook results do
not advance lifecycle state.

Security-sensitive commands emit a reliable
`core.plugin.lifecycle_transition_requested` AUDIT event before their protected
transition. Committed state changes emit
`core.plugin.lifecycle_changed` OBSERVABILITY. Activation, drain, and unload
failures emit `core.plugin.lifecycle_failed` through reliable AUDIT delivery.
Authorization denial continues to use `core.authorization.denied`.

Plugin event payloads contain only Plugin and capability references, from and to
states, operation identity, active lease count, outcome, and safe error code.
They exclude entrypoints, implementation objects, secrets, raw permission
constraints, and exception text. Lifecycle state and registration receipts,
not event replay, remain authoritative.

## 11. Standard Errors

The SDK represents at least:

- `invalid_manifest`
- `incompatible_core_api`
- `dependency_unavailable`
- `dependency_ambiguous`
- `dependency_cycle`
- `registration_conflict`
- `registration_identity_conflict`
- `lifecycle_state_conflict`
- `permission_denied`
- `invalid_lifecycle_transition`
- `lease_identity_conflict`
- `lifecycle_hook_failed`
- `plugin_draining`
- `drain_timeout`
- `unload_failed`

Errors identify retryability and preserve the causing Plugin and capability.
`plugin_draining`, `drain_timeout`, and `unload_failed` are retryable.
Manifest, cycle, ambiguity, identity, and lifecycle conflicts are not retryable
until their input or state changes.

## 12. Conformance

A conforming implementation demonstrates:

- Atomic registration rollback.
- Rejection of new work during DRAINING.
- Preservation of active work until lease release.
- Idempotent unload and cleanup retry.
- Explicit return from DRAINING to ACTIVE.
- Version, dependency, permission, and collision validation.
- Permutation-independent dependency plans and errors.
- No partial registry visibility to concurrent readers.
- Lease cleanup after success, failure, timeout, cancellation, and cleanup error.
- Authorized lifecycle calls with narrowed Scope and reliable redacted audit.
