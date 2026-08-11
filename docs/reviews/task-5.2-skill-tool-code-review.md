# Skill and Tool v1 Code Review Guide

Status: Non-normative reviewer aid  
Reviewed baseline: CongeriesCore 0.2.0, RFC-0013

## Purpose

This guide explains the Task 5.2 change in a practical review order. It does not
replace [RFC-0013](../rfcs/RFC-0013-skill-tool-contracts.md); the RFC wins if the
two disagree.

The central rule is simple: a caller may discover immutable Skill or Tool
metadata without touching Plugin implementation objects, but actual Skill
content or Tool execution is available only after validation, authorization,
Scope narrowing, and Plugin lease acquisition.

## One-minute mental model

- A `CapabilityRef` is an exact address: capability kind, ID, owner, and contract
  version. It never means "latest compatible version."
- `SkillRegistry` and `ToolRegistry` are read-only windows onto the existing
  Plugin registry. They do not publish or remove anything themselves.
- A Skill is a catalog. Discovery reads the catalog; loading opens exactly one
  named resource under a byte budget.
- A Tool is a guarded function. Input is checked before execution, output is
  checked before success, and every permitted retry shares one operation identity.
- A Plugin lease is the safety pin. It stays held while loader/executor code and
  all post-processing still depend on the Plugin implementation.
- AgentRuntime validates references early, but does not load Skills or run Tools.

```text
AgentSpec / future Workflow adapter
              |
              v
       SkillToolResolver
              |
              v
     typed registry facade ---------> immutable Plugin registry snapshot
              |
              v
 SkillResourceGateway or ToolGateway
              |
       pure/schema validation
              |
              v
     PluginCapabilityInvoker
              |
     authorize + narrow Scope
              |
              v
      acquire one Plugin lease
              |
       use implementation only here
              |
       validate result and release
```

## Recommended review order

### 1. Exact references and AgentSpec compatibility

Start with:

- `src/congeries_core/runtime/capability.py`
- `src/congeries_core/harness/agent.py`
- `tests/fixtures/v0.2/agent_spec.json`
- `tests/fixtures/v0.2/agent_spec_v2.json`

`CapabilityRef` requires an explicit owner and exact wire contract version. Skill
and Tool v1 use wire version `1`; `registration_key` maps that value to the
Plugin registry's canonical `1.0.0` declaration key. No range or implicit-latest
lookup exists.

AgentSpec has two intentional wire shapes:

- v1 has no top-level version and keeps legacy `ResourceRef` values exactly;
- v2 declares `contract_version: "2"` and requires `CapabilityRef` values.

An ownerless v1 reference remains readable and serializable because compatibility
is a wire promise. It cannot be resolved or upgraded because Core cannot guess
which Plugin owns it. `upgrade_v2()` performs only representation migration; it
does not load, authorize, or resolve a capability.

AgentRuntime validates all Skill and Tool references before the first Run state
transition or Context/Model call. The Model request receives only Tool resource
identities. A returned Tool proposal is not executed in this slice.

### 2. Frozen Skill contracts and progressive resources

Review together:

- `src/congeries_core/skill/model.py`
- `src/congeries_core/skill/registry.py`
- Skill fixtures under `tests/fixtures/v0.2/`

Constructors reject invalid v1 references, duplicate resource IDs or paths, and
paths that are absolute, contain backslashes, empty segments, `.` or `..`.
Collections are tuples and public resolved values expose descriptor, owner, and
registration identity only.

Skill metadata discovery never calls the resource loader. A request names one
declared resource, and the loader receives only its descriptor. Text byte counts
use UTF-8; JSON byte counts use canonical sorted compact JSON. This makes the
declared and enforced budget deterministic across implementations.

### 3. Frozen Tool contracts and policy consistency

Review together:

- `src/congeries_core/tool/model.py`
- `src/congeries_core/tool/registry.py`
- Tool fixtures under `tests/fixtures/v0.2/`

`ToolDescriptor` binds one exact Tool to input/output Schemas, one Action, an
execution policy, side-effect classification, and idempotency mode. External
side effects require caller-key idempotency; side-effect-free Tools declare that
idempotency is not applicable. Invalid combinations are constructor errors, not
runtime policy guesses.

The typed facade verifies the current Plugin registration, owner, descriptor,
Action permission, and both registered Schemas. It returns the current
`registration_id`; generation ownership and stale-receipt protection remain in
the Plugin registry transaction rather than in `CapabilityRef`.

### 4. Shared authorization and lease boundary

Review:

- `src/congeries_core/plugin/invocation.py`
- `PluginManager.invoke` in `src/congeries_core/plugin/manager.py`

`PluginCapabilityInvoker` centralizes the order shared by Plugin, Skill, and Tool
calls:

1. resolve the exact current registration;
2. verify owner and authorization resource identity;
3. require the Action declared by the capability;
4. dispatch through deny-by-default authorization;
5. verify the narrowed Scope still matches the manifest permission;
6. acquire the Plugin execution lease;
7. expose the opaque implementation only inside the leased callback; and
8. release in `finally`.

The optional `resource_validator` is not a bypass. It exists because a Skill
resource read authorizes a more specific `skill_resource` child instead of the
parent Skill. The validator must still bind that child to the exact Skill
registration and request.

`PluginManager.invoke` remains a compatibility wrapper over this boundary. There
must not be a second direct path that hands a loader or executor to callers.

### 5. Skill resource gateway

Review `src/congeries_core/skill/gateway.py` from `load` through `_validate_grant`.

The no-effect checks happen first: invocation identity, typed registration,
declared resource, and requested byte budget. Authorization then targets one
resource-specific identity and carries fixed path/media constraints. A grant may
only lower `max_bytes`; it cannot choose another resource or representation.

One Plugin lease covers loader access, media-type comparison, construction of the
`SkillResource`, and actual byte-count enforcement. DRAINING therefore either
rejects the acquisition or waits for this complete read boundary. The result is
returned only; there is no Agent-context mutation in the gateway.

### 6. Tool gateway, retry, and whole-call timeout

Review `src/congeries_core/tool/gateway.py` in this order:

1. `execute` before the nested callback;
2. `_validate_grant`;
3. `_with_timeout`;
4. the retry loop; and
5. `_execute_attempt` cleanup.

Input schema validation occurs before authorization, lease acquisition, or an
executor effect. The grant can preserve or narrow the descriptor: fixed Action,
Schemas, side-effect, and idempotency values cannot change; attempts can only
decrease; a finite timeout can only become shorter. When the descriptor timeout
is `None`, a grant may add a finite timeout because that reduces authority.

The Tool deadline is anchored at `execute` entry. Registration resolution,
schema validation, authorization, lease acquisition, all attempts, and output
validation spend the same whole-invocation budget. An earlier caller deadline
still wins.

One lease surrounds the whole retry loop. Only retryable structured errors and
normalized ordinary executor exceptions may retry. Schema, protocol, denial,
invalid-grant, and non-retryable failures stop immediately. Every attempt receives
the same `ToolCall`, narrowed context, and operation identity. `_execute_attempt`
owns an explicit task so timeout or cancellation cancels and awaits executor
teardown before another attempt or lease release.

Output normalization and schema validation happen before `ToolResult` and before
lease release. Replaying a released invocation identity fails through the Plugin
lease contract instead of silently repeating a side effect.

### 7. Events and redaction

Review:

- `src/congeries_core/skill/gateway.py`
- `src/congeries_core/tool/gateway.py`
- `src/congeries_core/event/model.py`
- `tests/fixtures/v0.2/core_events.json`

Skill and Tool started/completed/failed events are best-effort observability.
Their payloads contain identities, byte counts, attempts, latency, and safe error
codes. They do not contain Skill content, Tool input/output, exception messages,
credentials, implementation entries, or scripts.

Authorization audit remains the reliable path owned by RFC-0008. Suppressing an
observability sink failure must never suppress or weaken an authorization audit
failure.

## Deliberate boundaries reviewers should preserve

| Question | Intended answer |
| --- | --- |
| Why is the Agent preflight before Run start? | An invalid static reference must fail before Run state, Context, Model, loader, or executor effects. Context identity is checked first; active deadline/cancellation is checked immediately before the first transition. |
| Does Tool timeout include authorization and lease waiting? | Yes. It is a whole-invocation budget anchored at gateway entry. |
| Can policy add a timeout when the descriptor has none? | Yes. `None` means no Tool-specific limit; a finite grant narrows that authority. It cannot override an earlier caller deadline. |
| Why does Skill authorize `skill_resource` instead of `skill`? | Policy can grant one declared resource without granting every resource in the Skill. The custom validator binds it back to the parent registration. |
| Is registration generation part of `CapabilityRef`? | No. The facade resolves the current snapshot and returns its registration identity. Generation receipts and stale removal protection belong to the Plugin registry. |
| Why does `CapabilityRef.key` omit namespace? | Skill/Tool v1 registries accept only `core`; namespace is validated before the key participates in those registries. A future multi-namespace contract must revisit this key. |
| Why are observer failures ignored? | These events report an outcome; they do not authorize it. Reliable authorization audit still fails closed. |
| Is exactly-once Tool execution promised? | No. The contract is at-least-once-safe with stable caller identity. External systems must honor that idempotency key. |

## Test evidence map

The primary shared suite is `tests/test_skill_tool_contract.py`; Agent integration
is in `tests/integration/test_agent_runtime.py`; exact wire compatibility is in
`tests/test_compatibility_fixtures.py`.

| Concern | Primary evidence |
| --- | --- |
| Frozen models, strict paths, exact round trips | model validation tests and Skill/Tool fixture checks |
| Lazy Skill loading | discovery/resolve assertions prove loader call count stays zero until `load` |
| Denial and invalid budget before effects | Skill denial/budget tests assert zero loader calls |
| Resource identity binding | invoker resource-mismatch test |
| Schema-aware Tool execution | input failure occurs before executor; output failure occurs before success and releases lease |
| Stable in-lease retry | retry test asserts one lease, one operation identity, and exact attempt count |
| Invalid grant and replay | grant/replay tests assert no extra executor side effect |
| Timeout, cancellation, and drain | lease release and acquire/drain linearization tests |
| Owner/version/action/schema resolution | shared resolver mismatch tests |
| Agent preflight before effects | Agent integration tests assert unchanged Run and zero Context/Model calls |
| Replaceable implementations | parameterized contracts run against two loader and two executor implementations |

## High-risk review checklist

- [ ] Legacy AgentSpec v1 remains byte-exact and v2 rejects unversioned references.
- [ ] Invalid references and input schemas fail before loader/executor effects.
- [ ] Typed facades never mutate the Plugin registry or expose implementations.
- [ ] Every authorization resource is bound to the exact current registration.
- [ ] Unknown or identity-changing grant constraints fail closed.
- [ ] The effective Scope still satisfies the Plugin manifest permission.
- [ ] Skill content size is computed from actual canonical bytes under the lease.
- [ ] One Plugin lease covers every Tool attempt and output validation.
- [ ] Every retry preserves operation identity and retries only allowed failures.
- [ ] Timeout, cancellation, failure, or schema rejection cannot leak a task or
      Plugin lease.
- [ ] DRAINING rejects new work while allowing already leased work to finish.
- [ ] Observability payloads contain no content, arguments, results, or raw errors.
- [ ] AgentRuntime does not load Skills, execute Tools, or mutate Workflow support.

## Verification commands

```text
uv run pytest
uv run ruff check .
uv run pyright
git diff --check
```

## Deliberate v1 limits

- Only exact Skill/Tool contract version `1` is supported.
- Retry has no backoff schedule and does not promise exactly-once effects.
- Core does not parse or execute Skill scripts or instructions.
- AgentRuntime has no Tool loop and no automatic Skill-context injection.
- Workflow SkillNode and ToolNode remain unsupported.
- MCP is not connected in this slice.
