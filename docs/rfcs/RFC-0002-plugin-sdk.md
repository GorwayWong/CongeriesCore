# RFC-0002: Plugin SDK

- ID: RFC-0002
- Title: Plugin SDK
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0009](../adrs/ADR-0009-safe-plugin-unload.md)
- Supersedes: None

## 1. Scope

This RFC defines plugin packaging, manifest validation, capability registration,
execution leases, drain, and unload. It does not define business capability or
concrete plugin loading technology.

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

## 3. Manifest

The manifest contains:

| Field | Requirement |
| --- | --- |
| `name` | Stable namespaced plugin identifier |
| `version` | Plugin semantic version |
| `core_api` | Compatible Core contract version range |
| `entrypoint` | Loader-specific entry reference |
| `provides` | Versioned capability identifiers by type |
| `requires` | Plugin or Core capability dependencies |
| `permissions` | Requested actions and Scope patterns |
| `lifecycle` | Optional hook capability declarations |

Manifest validation rejects missing required fields, invalid versions, duplicate
capability identifiers, unresolved dependencies, and permission declarations
that cannot be represented by the active AuthorizationPolicy.

## 4. Capability Types

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

## 5. Lifecycle

The lifecycle states are:

```text
DISCOVERED -> VALIDATED -> LOADED -> REGISTERED -> ACTIVE
ACTIVE -> DRAINING -> UNREGISTERED -> UNLOADED
```

`Execute` is behavior while ACTIVE and is not a lifecycle state.

| Transition | Required behavior |
| --- | --- |
| DISCOVERED -> VALIDATED | Parse manifest and verify compatibility, dependencies, schemas, and permission declarations |
| VALIDATED -> LOADED | Resolve the entrypoint without publishing capability |
| LOADED -> REGISTERED | Atomically publish capability registrations or publish none |
| REGISTERED -> ACTIVE | Allow new execution leases |
| ACTIVE -> DRAINING | Reject new leases and retain existing leases |
| DRAINING -> UNREGISTERED | Proceed only after the active lease count reaches zero |
| UNREGISTERED -> UNLOADED | Run disposal hooks and release plugin-owned resources |

Invalid transitions return a lifecycle conflict and do not change state.

## 6. Execution Leases

Every plugin capability invocation obtains a lease before dispatch. A lease:

- Identifies the plugin, capability, and Run.
- Is created only while the plugin is ACTIVE.
- Is released after success, failure, timeout, or cancellation cleanup.
- Prevents unregistration and disposal while active.

Lease acquisition and release are idempotent for the same invocation identity.

## 7. Drain and Unload

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

## 8. Lifecycle Hooks

Optional hooks are:

- `on_load`
- `on_activate`
- `on_drain`
- `on_unload`

Hooks receive RuntimeCallContext or a lifecycle-specific equivalent with Scope,
deadline, cancellation, trace, and audit correlation. Hook presence is declared
in the manifest; loaders do not discover hidden hooks.

## 9. Authorization and Events

Permission requests are evaluated before activation. Capability invocation is
evaluated again with the caller RuntimeCallContext.

Lifecycle state changes emit Runtime Events. Drain, denied activation, and
unload failures are audit-relevant events under
[RFC-0010](RFC-0010-runtime-events.md).

## 10. Standard Errors

The SDK represents at least:

- `invalid_manifest`
- `incompatible_core_api`
- `dependency_unavailable`
- `registration_conflict`
- `permission_denied`
- `invalid_lifecycle_transition`
- `plugin_draining`
- `drain_timeout`
- `unload_failed`

Errors identify retryability and preserve the causing plugin and capability.

## 11. Conformance

A conforming implementation demonstrates:

- Atomic registration rollback.
- Rejection of new work during DRAINING.
- Preservation of active work until lease release.
- Idempotent unload and cleanup retry.
- Explicit return from DRAINING to ACTIVE.
- Version, dependency, permission, and collision validation.

