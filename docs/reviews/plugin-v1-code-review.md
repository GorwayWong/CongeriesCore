# Plugin v1 Code Review Guide

Status: Non-normative reviewer aid  
Reviewed baseline: CongeriesCore 0.2.0, RFC-0002

## Purpose

This guide explains the Plugin v1 implementation in a practical review order. It
does not replace [RFC-0002](../rfcs/RFC-0002-plugin-sdk.md); when this guide and
the RFC disagree, the RFC wins.

The central rule is simple: Core may expose a Plugin only after it has validated
the complete declaration, and Core may dispose Plugin resources only after no
execution lease can still be using them.

## One-minute mental model

Think of a Plugin as a small shop inside Core:

- The manifest is its license. Core checks the license before opening the door.
- Dependency resolution checks that every required supplier exists.
- Registration flips one sign from "closed" to "all capabilities available".
- Each invocation takes a numbered coat-check ticket: the execution lease.
- DRAINING locks the front door but lets customers already inside finish.
- Unregister removes the sign; cleanup releases the shop's private resources.
- If cleanup fails, Core records how far it got and retries from that safe point.

```text
JSON mapping
    |
    v
pure ManifestValidator -- invalid --------------------------> no effects
    |
    v
preflight + dependency plan -- unavailable/conflict -------> no load
    |
    v
loader.prepare (private handles)
    |
    v
registry snapshot swap -> REGISTERED -> permission checks -> ACTIVE
                                                        |
                                                acquire/release leases
                                                        |
                                                        v
                reject new leases <- DRAINING <- drain/unload request
                                        |
                                 active lease count = 0
                                        |
                              unregister -> cleanup -> UNLOADED
```

## Recommended review order

### 1. Exact contracts and pure validation

Start with:

- `src/congeries_core/plugin/model.py`
- `src/congeries_core/plugin/manifest.py`
- `tests/fixtures/v0.2/plugin_manifest.json`

Check that public wire values reject unknown fields and that constructors make
invalid states difficult to represent. In particular:

- Plugin and capability identities are stable, lowercase, namespaced values;
- SemVer and ranges use only the v1 exact/AND comparator grammar;
- capability declarations have a stable type, identity, contract version,
  implementation entry, and permissions;
- Plugin and capability permissions do not duplicate an Action;
- Scope patterns have the exact `namespace:kind:id` shape, with only a whole-ID
  `*` wildcard;
- lifecycle hooks are explicitly declared; and
- `ManifestValidator` catches parsing/value errors and returns `invalid_manifest`
  without calling policy, loader, registry, lifecycle, or events.

`PluginPreflight` is deliberately separate. It checks Core API compatibility and
whether the active policy model can represent requested permissions, but it does
not authorize activation. Actual permission decisions happen later with runtime
context.

### 2. Deterministic dependency planning

Review `src/congeries_core/plugin/dependency.py`.

The resolver consumes candidate manifests plus one immutable catalog snapshot.
Provider-to-consumer edges allow a Kahn topological walk to produce activation
order. Every input collection and ready set is sorted so input permutation cannot
change the plan.

Review these distinctions carefully:

- no matching identity is `dependency_unavailable`;
- matching identities with no compatible version are incompatible;
- more than one compatible provider is `dependency_ambiguous`; and
- residual incoming edges after traversal are `dependency_cycle`.

The resolver never downloads, imports, loads, or chooses an arbitrary winner.
Ambiguity is an error because a hidden tie-break would make ownership depend on
discovery order.

### 3. Loader staging and atomic registration

Review together:

- `src/congeries_core/plugin/loader.py`
- `src/congeries_core/plugin/registry.py`
- `PluginManager._load_effect` in `src/congeries_core/plugin/manager.py`

The loader returns a `PreparedPlugin`: private implementation handles plus exact
declarations and hooks. `_validate_prepared` requires the returned manifest,
capability set, and hook set to match the validated manifest exactly. Until that
passes, nothing enters the public registry.

`CapabilityRegistry.commit` performs four operations under one lock:

1. compare the caller's expected snapshot version;
2. validate every capability key and collision;
3. build a new complete immutable mapping; and
4. replace the snapshot once.

Readers therefore observe the old snapshot or the complete new snapshot. The
receipt includes the committed registry generation. Unregister verifies every
key against that exact generation before deleting any key. This is why an old
receipt cannot remove a later registration owned by the same Plugin.

Registration and lifecycle state are two coordinated boundaries, not one storage
transaction. The manager publishes the registry receipt, then moves the Plugin
to REGISTERED. If that lifecycle CAS fails, it immediately rolls back the exact
receipt. Reviewers should preserve this compensation path whenever either side
changes.

### 4. Lifecycle state and execution leases

Review `src/congeries_core/plugin/lifecycle.py`.

Each Plugin owns one `asyncio.Condition`; it is both the state-machine lock and
the drain-wakeup channel. `state_version` rejects stale writers. Entering ACTIVE
increments `activation_epoch`, so lease identity cannot silently cross a
reactivation boundary.

Lease acquisition and beginning DRAINING use the same condition. Their race has
only two legal outcomes:

- acquire wins first: the lease exists and drain must wait for it; or
- DRAINING wins first: the new acquisition receives `plugin_draining`.

An invocation identity may return its existing live lease only for the same Run
and capability. A mismatch is `lease_identity_conflict`. Released lease IDs are
retained so a completed invocation cannot recreate an old lease through an ABA
replay. Release is idempotent, validates the exact recorded lease, updates the
active set, and then wakes drain waiters.

`wait_for_zero` rechecks the lease predicate under the condition and wraps the
wait with runtime deadline/cancellation control. A timeout or cancellation stops
the wait but does not release someone else's lease and does not dispose Plugin
resources.

### 5. Authorization and invocation

Review these manager paths:

- `PluginManager.load`, `activate`, `drain`, `cancel_drain`, and `unload`;
- `PluginManager._authorize_activation_permissions`; and
- `PluginManager.invoke`.

Every lifecycle command goes through its fixed `core.plugin.*` Action before its
effect. The required transition-request AUDIT event is then published before the
protected state change. A failed required audit therefore prevents the effect.

Activation evaluates declared permissions as a Plugin principal. Invocation is a
different decision: it uses the runtime caller, requires the supplied Action to
exist in that capability's declaration, and checks the granted effective Scope
against the declared Scope pattern. Only after both policy and declaration checks
pass does the manager acquire a lease.

The invocation body uses `await_provider`, so deadline and cancellation actively
cancel external work and late success is discarded. Lease release is in
`finally`; do not move it into success-only code.

### 6. Drain, unload, and retry recovery

Review `PluginManager._drain_effect`, `_unload_effect`, and `_ensure_draining`.

The important order is:

1. publish the reliable requested event;
2. atomically enter DRAINING;
3. run the declared drain hook once successfully;
4. wait for the active lease count to reach zero;
5. unregister the exact receipt;
6. enter UNREGISTERED;
7. run the unload hook once successfully;
8. ask the loader to clean up; and
9. enter UNLOADED and forget prepared handles.

`successful_hooks` is durable progress within the in-process lifecycle record.
On retry, completed hooks are skipped and failed hooks run again. A drain timeout
or cancellation leaves DRAINING with registrations and resources intact.
`cancel-drain` may return to ACTIVE only while the registration receipt still
exists. An unload hook or loader cleanup failure leaves UNREGISTERED, so new work
cannot enter but cleanup can be retried safely.

Recovery cleanup and failure reporting use a context that retains Run, Scope,
trace, and idempotency identity but removes the already-fired deadline and
cancellation token. Staged cleanup suppresses its own exception because it must
not hide the original load failure.

### 7. Reliable and redacted lifecycle events

Review together:

- `src/congeries_core/plugin/events.py`
- `src/congeries_core/event/model.py`
- `src/congeries_core/event/schema.py`
- `docs/rfcs/RFC-0010-runtime-events.md`

Transition-requested and failed events are AUDIT delivery. Their EventId is a
deterministic digest of safe logical identity, allowing at-least-once retry and
outbox deduplication. Lifecycle-changed is post-commit OBSERVABILITY and is
best-effort, so its failure cannot roll state back.

Payloads contain references, state names, operation identity, active lease count,
outcome, and safe error code. They must never gain entrypoints, implementation
objects, raw permission constraints, exception messages, or Plugin-owned data.

## Linearization and recovery map

| Concern | Linearization point | Safe failure state |
| --- | --- | --- |
| Registry publication | Immutable snapshot assignment under registry lock | Previous snapshot |
| Registry removal | Replacement snapshot after every receipt key validates | Previous snapshot |
| State transition | Record replacement under per-Plugin condition | Previous state/version |
| Acquire versus drain | Shared per-Plugin condition | Lease exists, or DRAINING rejects it |
| Load preparation | No public point until registry commit | VALIDATED; staged cleanup attempted |
| Activation | REGISTERED to ACTIVE CAS after permission/hook success | REGISTERED |
| Drain wait timeout/cancel | No state rollback | DRAINING with live receipt/resources |
| Unload hook/cleanup failure | Receipt already removed | UNREGISTERED; retry cleanup |
| Successful repeated unload | Existing UNLOADED record | UNLOADED; cleanup not repeated |

## Test evidence map

| Concern | Primary evidence |
| --- | --- |
| Strict Manifest/SemVer/Scope and exact round-trip | `tests/test_plugin_contract.py::test_manifest_round_trips_strictly`; invalid contract and Scope parameterized tests |
| Permutation-independent dependencies and distinct errors | `test_dependency_resolution_is_permutation_independent`; missing/incompatible/cycle/ambiguity tests |
| Atomic snapshots, generation receipts, stale ownership | `test_registry_commit_is_atomic_owned_and_idempotently_unregistered`; `test_registry_generation_identity_rejects_stale_receipt`; `test_registry_readers_never_observe_partial_registration` |
| Lifecycle CAS and lease idempotency | `tests/test_plugin_lifecycle.py::test_lifecycle_transitions_hooks_and_discovery_are_idempotent`; `test_execution_lease_is_active_only_and_idempotent` |
| Acquire/drain linearization | `test_acquire_and_drain_have_only_linearized_outcomes` |
| Deadline/cancellation preserve active resources | `test_drain_wait_honors_cancellation_and_deadline_without_releasing`; integration drain timeout test |
| Loader contract failure and staged cleanup | `tests/integration/test_plugin_runtime.py::test_loader_contract_violation_cleans_staged_resources` |
| Activation permission and invocation Action/Scope | `test_activation_evaluates_declared_permissions_before_hook`; `test_invocation_requires_declared_action_and_effective_scope` |
| Lease release on every call outcome | `test_invocation_failure_timeout_and_cancellation_always_release_lease` |
| Drain rejection and preservation of existing work | `test_unload_drains_existing_work_and_rejects_new_invocations` |
| Hook/cleanup retry and concurrent unload | `test_unload_failure_stays_unregistered_and_retry_resumes`; `test_concurrent_unload_serializes_cleanup_once` |
| Reliable, deduplicated, reference-only events | `tests/test_plugin_events.py::test_plugin_events_are_reliable_deduplicated_and_reference_only` |
| Two replaceable loaders | `test_fake_loaders_share_complete_load_invoke_unload_contract` |
| Exact compatibility catalogs | `tests/test_compatibility_fixtures.py` Plugin fixture checks |

## High-risk review checklist

- [ ] Manifest failure occurs before authorization, loader, registry, lifecycle,
      and event effects.
- [ ] Dependency resolution reads one immutable snapshot and never selects an
      arbitrary provider.
- [ ] No registry writer mutates the mapping held by an existing snapshot.
- [ ] Every registration key validates before publication or removal.
- [ ] A stale or retired receipt cannot delete a newer generation.
- [ ] Acquire and DRAINING admission remain protected by the same condition.
- [ ] No path unregisters or cleans resources while the lease count is nonzero.
- [ ] Capability Action and effective Scope match the manifest declaration.
- [ ] Permission evaluation happens before activation hooks can make the Plugin
      ACTIVE.
- [ ] Timeout, cancellation, provider failure, and cleanup exceptions cannot leak
      an execution lease.
- [ ] Drain timeout/cancellation leaves registrations and resources available for
      retry or explicit cancel-drain.
- [ ] Completed hooks and successful cleanup are not repeated by retry/unload.
- [ ] AUDIT failure prevents its protected transition; observability failure does
      not change authoritative state.
- [ ] Events and errors contain no implementation object, secret, or raw Plugin
      content.

## Deliberate v1 limits

- YAML parsing, file discovery, and dynamic imports belong to loaders.
- Version ranges have no optional, wildcard, caret, tilde, or OR syntax.
- One Plugin or capability identity cannot have multiple active versions.
- Lease state and hook progress are in-process; v1 does not recover a lease that
  was executing across process failure.
- Hook identities support safe retry of completed Core steps, but external side
  effects are not promised exactly once.
- Runtime Events are not authoritative state and are not used to rebuild the
  registry or lifecycle.
- Skill/Tool execution, MCP adapters, and remaining Workflow node kinds are later
  milestones built on this Plugin boundary.
