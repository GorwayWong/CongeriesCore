# Agent Harness Runtime Core Tasks

Version: 0.2.0

## 1. Delivery Rules

Implementation follows [principles.md](principles.md),
[requirements.md](requirements.md), [design.md](design.md), and the Accepted RFC
registry in [docs/rfcs/README.md](docs/rfcs/README.md).

Every task requires unit tests for its public contract and integration tests for
cross-module behavior. Failure-path tests are part of acceptance, not optional
follow-up work.

### 1.1 Status Semantics

- `Not Started`: no public implementation contract has been delivered.
- `In Progress`: a reusable foundation exists, but at least one acceptance
  criterion or required integration path remains incomplete.
- `Implemented in 0.2.0`: the task contract and failure paths have verified
  implementation coverage for the v0.2 baseline.

### 1.2 Current Delivery Snapshot

| Area | Status | Delivered baseline |
| --- | --- | --- |
| Project and shared types | Implemented | Python 3.12 package, typed identifiers, JSON boundary, deadlines, cancellation, trace, and structured errors |
| Run, Session, and Workspace | Implemented | Run hierarchy, lifecycle, attempts, continuation, compare-and-set repository, Session lifecycle, and Workspace versioning |
| Scope and Authorization | Implemented | Generic Scope model, default-deny AuthorizedDispatcher, audit integration, and non-bypass Context, Memory, Model, and EventSink paths |
| Runtime Events | Implemented | Versioned envelope, schema registry, redaction, observability queue, reliable audit outbox, and SQLite reference adapter |
| Execution Harness and Storage | In Progress | RunService lifecycle coordination, active Provider-call cancellation, authorized CheckpointStore, recovery, approval persistence, and Evaluation; Artifact storage and provider-wide storage contracts remain |
| Context, Memory, Model, and direct Agent execution | Implemented | Authorized Context resolution, independent Memory operations, Model generation and streaming, AgentSpec registries, and root AgentRun execution |
| Workflow | In Progress | Minimal direct runtime with versioned contracts, DAG validation, AgentNode child Runs, durable output references, checkpoint recovery, ApprovalNode, EvaluationNode, and exact fixtures; remaining node catalog and engine adapters are deferred |
| Plugins, Tools, and MCP | In Progress | Plugin v1 manifest, deterministic dependencies, atomic registry, execution leases, authorized lifecycle, safe drain/unload, events, and fixtures implemented; Skill, Tool, and MCP execution remain |

### 1.3 Current Workflow Milestone

The delivered milestone is a **minimal direct Workflow Runtime**. It is a
deliberately narrow Task 4.1 slice and followed this dependency order:

1. Implement immutable Workflow definition, node, dependency, policy, context,
   result, and durable node-output reference contracts with exact v0.2
   compatibility fixtures. Raw Agent output bodies never enter Checkpoints.
2. Validate schema identities, node uniqueness, dependencies, the DAG, required
   outputs, declared permissions, and supported node contracts before execution.
3. Add deterministic dependency scheduling. The first implementation may limit
   concurrency to one, but it must dispatch only nodes whose dependencies and
   policy conditions are satisfied.
4. Support AgentNode first. Each dispatch creates a child AgentRun and propagates
   Run relationships, Scope, deadline, cancellation, trace, and idempotency.
5. Commit the workflow-start and stable AgentNode boundaries through
   CheckpointCoordinator. Recovery must restore node outcomes, pending work, and
   side-effect keys before scheduling resumes.
6. Add ApprovalNode next by composing the implemented ApprovalCoordinator.

SkillNode, ToolNode, ContextNode, EvaluationNode, custom node extensions, and
external Workflow engine adapters were outside this first slice. EvaluationNode
was delivered afterward by Task 4.3. The other node types remain deferred, and
unsupported contracts are rejected during validation rather than skipped at
runtime.

Task 6.3 now includes exact Workflow fixtures for the delivered public schemas.

Tool and MCP tasks must register their actions and reuse the implemented
AuthorizedDispatcher boundary before they may be marked Implemented.

### 1.4 Completed Evaluation Milestone

The **Task 4.3 Evaluation pipeline** is implemented on top of SchemaRegistry,
AuthorizedDispatcher, RuntimeCallContext, Runtime Events, durable output
references, and Checkpoint recovery.

Completed delivery order:

1. Freeze the Evaluation contract in an owning RFC before implementation. Define
   versioned request, typed verdict/result, evidence references, evaluator
   capability, error, audit/redaction, idempotency, and checkpoint semantics.
   Keep evaluation-policy semantics distinct from access authorization rather
   than overloading AuthorizationPolicy.
2. Implement pure schema evaluation first. Schema mismatch produces a structured
   failed verdict and cannot create a successful output or stable success
   Checkpoint.
3. Add an injected, authorized evaluation-policy boundary. A denied or failed
   input remains denied or failed; later evaluators cannot upgrade it to success.
4. Add a replaceable quality evaluator with typed results and durable evidence
   references. Core coordinates evaluation but does not embed vendor scoring,
   domain rubrics, or business quality rules.
5. Compose the evaluators in a deterministic Evaluation harness with explicit
   precedence, Scope, deadline, cancellation, trace, idempotency, and redacted
   auditable events.
6. Enable EvaluationNode only after the standalone harness passes. Reuse the
   existing Workflow output-persistence and Checkpoint boundaries, and unlock
   downstream nodes only after a stable successful verdict.
7. Add exact fixtures, failure-path unit tests, Workflow integration tests, and
   documentation synchronization before changing task or RFC status.

Evaluation milestone acceptance:

- Unknown contract versions and invalid schemas fail before evaluator dispatch.
- Every external evaluation call is authorized and receives RuntimeCallContext.
- Schema mismatch, policy denial, and quality failure remain distinct typed
  outcomes and never become terminal success.
- Deadline and cancellation stop active evaluator work and discard late results.
- Audit events expose verdict metadata and references without raw evaluated
  content.
- EvaluationNode failure or denial does not unlock downstream Workflow nodes.
- A successful EvaluationNode persists any required output or evidence reference
  before its stable Checkpoint; recovery skips that committed evaluation.
- In-memory fakes demonstrate identical outcomes through replaceable evaluator
  implementations, and the full coverage, Ruff, and Pyright gates remain green.

### 1.5 Current Plugin Milestone and Next Recommendation

**Task 5.1 Plugin SDK and Safe Unload is implemented.** Delivered:

- Strict Plugin Manifest v1, pure validation, exact SemVer comparator ranges,
  and JSON compatibility fixtures.
- Permutation-independent dependency planning with distinct missing,
  incompatible, ambiguous, and cyclic failures.
- Immutable ownership-aware registry snapshots, generation-bound receipts,
  atomic registration, and idempotent rollback and unregister.
- Versioned lifecycle state, activation epochs, ACTIVE-only execution leases,
  linearized drain admission, and lease cleanup on every terminal call outcome.
- Authorized load, activate, drain, cancel-drain, and unload operations; Plugin
  permission evaluation before activation; declared Action and Scope enforcement
  on capability invocation.
- Recoverable drain timeout and cancellation, unload-hook retry, repeated and
  concurrent unload safety, reliable requested/failed audit events, and redacted
  post-commit lifecycle observability.
- Two fake loaders plus manifest, dependency, registry, lease, rollback, race,
  unload, authorization, event, and exact fixture tests.

Start **Task 5.2 Skill and Tool Registries** next, using this lifecycle boundary
for registration and execution. Freeze the standalone Skill and Tool contracts
and gateways before connecting Agent or Workflow execution; otherwise those
runtimes would accidentally become the place where registry, authorization, and
idempotency rules are defined. Defer Task 5.3 MCP adapters, remaining Workflow
node execution, parallel scheduling, external engines, and general
StorageProvider work until their owning milestones.

## 2. Phase 1: Architecture Baseline and Core Types

### Task 1.1 Project Structure

Status: Implemented in 0.2.0

Create modules for runtime, workflow, harness, state, policy, provider, plugin,
skill, tool, event, checkpoint, and adapter contracts.

Acceptance:

- Core builds and runs without an application plugin.
- No module contains business or vendor-specific types.
- Public modules depend on interfaces rather than concrete infrastructure.

### Task 1.2 Shared Runtime Types

Status: Implemented in 0.2.0

Implement stable identifiers, RuntimeCallContext, structured provider errors,
deadlines, cancellation, trace correlation, and idempotency keys.

Failure scenarios:

- Invalid identifiers or missing required context
- Expired deadlines and cancellation propagation
- Unsupported schema or contract versions

Acceptance:

- Shared types round-trip through the selected serialization boundary.
- Error categories preserve retryability and causal information.
- No vendor SDK type appears in a public contract.

## 3. Phase 2: Run, Session, Scope, and Authorization

### Task 2.1 Run Model and State Machine

Status: Implemented in 0.2.0

Implement Run, AgentRun, WorkflowRun, parent/root relationships, attempts,
timestamps, errors, and the RFC-0004 state machine.

Failure scenarios:

- Illegal or repeated state transition
- Cancellation racing with completion
- Retryable attempt failure followed by exhausted retry policy
- Retry or recovery requested from a terminal Run

Acceptance:

- AgentRun and WorkflowRun may each execute as a root Run.
- AgentNode creates a correctly related child AgentRun.
- PAUSED and WAITING_APPROVAL resume to RUNNING.
- SUCCEEDED, FAILED, and CANCELLED are irreversible.
- Retryable attempt failure enters RETRYING without entering FAILED, increments
  attempt, and redispatches the failed resumable phase.
- Non-retryable or exhausted failure enters terminal FAILED.
- Concurrent transition tests have one deterministic outcome.

### Task 2.2 Session and Workspace

Status: Implemented in 0.2.0

Implement SessionRef with OPEN/CLOSED lifecycle and Workspace state ownership.

Acceptance:

- Multiple Runs may share an authorized SessionRef or Workspace.
- Closing a Session prevents new associations without deleting Run history.
- Core Session state stores no messages, participants, or user model.

### Task 2.3 Scope and Authorization

Status: Implemented in 0.2.0

Delivered:

- Generic namespaced ScopeRef and runtime principal, action, and resource values
- AuthorizationPolicy, deny-all default, PolicyDecision, and explicit Grant
- AuthorizedDispatcher with unknown-action denial, Scope validation, deadline,
  cancellation, and audit gates
- Audit failure integration with PAUSED or policy-selected FAILED Run control
- Authorized Context capability and provide paths with key and budget narrowing
- Authorized Memory capability, retrieve, remember, forget, and consolidate
  paths with operation-specific constraint narrowing
- Authorized Model capability, generate, and stream paths with model, Tool, and
  budget narrowing
- Existing EventSink paths share the boundary
- Non-bypass, unknown-action, cancellation, deadline, invalid-grant, and
  audit-failure integration coverage

Future Tool, Storage, and MCP tasks must register actions and reuse
this boundary before their individual task status becomes Implemented.

Implement ScopeRef, runtime principals, AccessRequest, PolicyDecision, and
AuthorizationPolicy with default-deny behavior.

Failure scenarios:

- Missing grant
- Cross-scope access
- Unknown action or resource
- Authorization provider failure

Acceptance:

- Tool, Provider, and MCP dispatch cannot bypass authorization.
- Unspecified access is denied.
- Plugin-defined scope kinds remain opaque to Core.
- Denials and cross-scope grants produce audit events.

## 4. Phase 3: Context, Memory, and Model Providers

### Task 3.1 Context Harness

Status: Implemented in 0.2.0

Implement ContextProvider, provider selection, ContextResolver, injection, and
complete or partial ContextResult handling.

Acceptance:

- Provider selection is explicit and deterministic under policy.
- Provide and capability-discovery calls receive RuntimeCallContext and pass
  authorization.
- Denied, timeout, cancelled, partial, and unavailable outcomes remain distinct.
- Hidden global context and implicit database access are absent.

### Task 3.2 MemoryProvider

Status: Implemented in 0.2.0

Implement `retrieve`, `remember`, `forget`, and optional `consolidate` contracts
with Scope, pagination, idempotency, and structured results.

Acceptance:

- No `store` MemoryProvider operation exists.
- Forget validates both memory identity and Scope.
- Remember is idempotent for the same idempotency key.
- Implementations can omit consolidate while reporting capability accurately.
- Every operation and capability-discovery call receives RuntimeCallContext.
- Core has no memory schema, embedding, ranking, or database implementation.

### Task 3.3 ModelProvider

Status: Implemented in 0.2.0

Implement vendor-neutral `generate`, `stream`, and `capabilities` contracts and
AgentSpec model bindings.

Failure scenarios:

- Unsupported model capability
- Partial stream failure
- Timeout, cancellation, denial, and provider unavailability

Acceptance:

- Generate and stream report usage and structured errors.
- Generate, stream, and capabilities receive RuntimeCallContext and pass
  authorization.
- Cancellation terminates streaming without leaking provider resources.
- AgentSpec contains provider and model references, not SDK clients.
- At least two fake provider implementations pass the same contract tests.

### Task 3.4 AgentSpec and Minimal Agent Runtime

Status: Implemented in 0.2.0

Implement AgentSpec, validated composition and bindings, Agent construction, and
direct Agent execution coordinated through AgentRun.

Dependencies:

- Task 2.3 generic authorization foundation
- Task 3.1 ContextProvider and ContextResolver
- Task 3.3 ModelProvider

Failure scenarios:

- Missing or incompatible Context or Model binding
- Context resolution denied, partial, unavailable, timed out, or cancelled
- Model invocation denied, unavailable, timed out, cancelled, or malformed
- Stale Run transition while completing or cancelling execution

Acceptance:

- An Agent with no Skills or Tools can execute as a root AgentRun.
- AgentSpec stores registered capability references, not implementation objects.
- CONTEXT_LOADING and RUNNING transitions surround the corresponding protected
  calls and emit Runtime Events after state commit.
- Deadline, cancellation, Scope, trace, and idempotency propagate through every
  external call.
- The minimal runtime has no business concept, application workflow, or vendor
  SDK dependency.
- An end-to-end test executes a fake ContextProvider and fake ModelProvider
  through authorization and reaches SUCCEEDED without a plugin.

## 5. Phase 4: Workflow, Checkpoint, and Harness

### Task 4.1 Workflow Runtime

Status: In Progress

Implement Workflow definition, input and output schemas, graph validation,
ExecutionPolicy, WorkflowContext, WorkflowResult, and Agent, Skill, Tool,
Context, Approval, and Evaluation nodes.

First delivery slice:

- Immutable and explicitly serializable Workflow definition, node, dependency,
  policy, context, and result contracts
- An injected boundary that turns node output needed after recovery into typed,
  scoped durable references; the first slice selects no mandatory Artifact or
  storage implementation
- Validation before execution, including acyclic dependencies and supported node
  contracts
- Deterministic direct dependency scheduler with AgentNode as the first executable
  node type
- Child AgentRun creation with inherited runtime control boundaries
- CheckpointCoordinator commits at workflow start and stable node outcomes
- RecoveryCoordinator restoration before the scheduler can dispatch further work
- ApprovalNode integration only after AgentNode checkpoint recovery passes

Delivered in the first slice:

- Frozen, strictly serialized v1 Workflow definition, node, dependency, policy,
  context, terminal result, and approval suspension contracts
- Exact v0.2 Workflow definition, context, terminal result, suspension, and action
  catalog fixtures
- Pure DAG validation with exact SchemaRef compatibility and zero execution or
  Checkpoint effects on validation failure
- Deterministic one-at-a-time dependency scheduling
- Authorized AgentNode execution through child AgentRuns and typed, scoped,
  durable output references
- Workflow-start and stable node Checkpoints, marker-based recovery, stable-node
  skipping, and interrupted-node replay with stable idempotency keys
- ApprovalNode waiting, restart reconstruction, authorized decisions, and
  downstream resumption through the existing ApprovalCoordinator

Still deferred after the Evaluation delivery:

- SkillNode, ToolNode, and ContextNode execution
- Custom node registration and capability discovery
- External Workflow engine adapters and distributed scheduling

Acceptance:

- Invalid graphs and schemas fail before execution.
- Independent nodes execute only when dependency and policy rules allow.
- Unsupported node contracts fail validation rather than being ignored.
- AgentNode creates a child AgentRun with correct parent and root relationships.
- A checkpointed AgentNode cannot become stable until output required by later
  nodes or recovery has a durable reference.
- Stable node outcomes are not re-executed after recovery; interrupted work may
  replay with the same side-effect idempotency identity.
- Recovery restoration completes before dependency scheduling resumes.
- Workflow engines can be replaced through an adapter contract.
- No predefined business workflow exists in Core.

### Task 4.2 CheckpointStore and Recovery

Status: Implemented in 0.2.0

Implement atomic save, load, list, and delete; stable node boundaries; graph
version validation; recovery attempts; and checkpoint migration hooks.

Delivered:

- Versioned immutable Checkpoint, approval, side-effect, query, page, deletion,
  migration, and recovery contracts with canonical SHA-256 integrity
- Authorized CheckpointStore gateway and thread-safe in-memory adapter
- WorkflowRun marker compare-and-set, explicit orphan semantics, and protected
  orphan-only deletion
- Non-destructive migration, explicit audited corruption fallback, and minimal
  restoration with recovery attempt source tracking
- Durable pre- and post-approval checkpoint boundaries

Failure scenarios:

- Partial or corrupt checkpoint write
- Missing checkpoint
- Graph-version mismatch without migrator
- Replayed side effect without a valid idempotency key

Acceptance:

- Recovery provides documented at-least-once node execution.
- Side-effecting nodes cannot run without an idempotency key.
- Approval waiting and resolution each have a durable checkpoint boundary.
- Failed atomic writes never replace the last valid checkpoint.

### Task 4.3 Execution, Approval, and Evaluation Harnesses

Status: Implemented in 0.2.0

Delivered:

- Pure RunStateMachine and asynchronous RunService coordination
- Start, advance, pause, resume, retry, redispatch, recovery, completion,
  failure, and cancellation lifecycle operations
- State-version compare-and-set and competing completion/cancellation coverage
- Deadline and cancellation primitives propagated by RuntimeCallContext
- Active Context, Memory, and Model Provider calls are cancelled while Core
  awaits them; late results and post-terminal stream events are discarded
- Authorized approval request/decision coordination with durable pre- and
  post-decision checkpoints
- Checkpoint-based recovery coordination and attempt source tracking
- Versioned Evaluation request, stage result, verdict, result, capabilities,
  evidence reference, error, action, and fixed output-schema contracts
- Pure schema evaluation followed by an independent authorized EvaluationPolicy
  and one replaceable QualityEvaluator with deterministic fail-fast composition
- Reliable redacted verdict AUDIT acknowledgement before result persistence and
  stable successful or non-successful EvaluationNode Checkpoints
- Recovery that skips committed successful evaluations and terminalizes from a
  committed non-success result without evaluator redispatch
- Exact compatibility fixtures plus schema, policy, quality, timeout,
  cancellation, audit, idempotency, persistence-crash, Checkpoint-crash, and
  Workflow dependency-gate coverage

Acceptance:

- Deadlines and cancellation propagate to active child calls.
- Approval accepts only authorized, correlated decisions.
- Retry and recovery increment attempts and emit state transitions.
- Evaluation cannot turn a denied or failed result into success.

## 6. Phase 5: Plugin SDK, Skill, Tool, and MCP

### Task 5.1 Plugin SDK and Safe Unload

Status: Implemented in 0.2.0

Delivered strict manifest validation, deterministic dependency resolution,
atomic capability registration, ACTIVE execution leases, safe drain,
unregistration, unload recovery, authorization, and lifecycle events.

Failure scenarios:

- Invalid manifest or incompatible dependency
- Registration collision
- New call during DRAINING
- Drain timeout or unload hook failure

Acceptance:

- The lifecycle matches RFC-0002.
- DRAINING rejects new leases and preserves active work.
- Unload does not dispose active resources and is idempotent.
- Drain timeout remains recoverable by retry or explicit return to ACTIVE.

### Task 5.2 Skill and Tool Registries

Status: Implemented in 0.2.0

Delivered:

- RFC-0013 with exact Skill v1, Tool v1, CapabilityRef, AgentSpec v2, migration,
  authorization, retry, event, and lease semantics
- Frozen descriptors, requests, results, implementation wrappers, strict JSON
  serialization, canonical Skill byte accounting, and pure path/policy validation
- Read-only SkillRegistry and ToolRegistry facades over the atomic Plugin
  capability snapshot with current registration identity, owner, Action,
  permission, and Schema checks
- PluginCapabilityInvoker shared by PluginManager, progressive Skill loading, and
  Tool execution without a second registration or lease boundary
- Resource-specific Skill authorization and bounded lazy reads under one Plugin
  lease without automatic Agent context injection
- Schema-aware Tool execution with grant narrowing, whole-call deadline, stable
  operation identity, in-lease retry, output validation, and replay conflict
- SkillToolResolver preflight for AgentSpec and future Workflow adapters while
  Workflow SkillNode/ToolNode execution remains unsupported
- AgentSpec v2 exact CapabilityRef encoding with byte-exact legacy v1 reading and
  explicit `upgrade_v2()` migration
- Redacted Skill and Tool observability events plus exact descriptors, calls,
  results, actions, events, and dual AgentSpec fixtures
- Two loader and two executor contract implementations with permission, Schema,
  grant, retry, timeout, drain race, replay, lease release, and Agent preflight
  coverage

Implement discovery, versioned registration, progressive Skill loading, typed
Tool schemas, authorization, execution policy, and idempotency declaration.

Recommended delivery order:

The sequence below is retained as the implementation record. Its step 8 gates
are satisfied by the Task 5.2 suites together with the shared Plugin registry,
drain, unload, rollback, and recovery suites.

1. Write and accept the exact Skill and Tool v1 RFC contract. Define stable
   references, contract versions, Skill resource descriptors, Tool input/output
   `SchemaRef` values, permissions, execution policy, retry/deadline behavior,
   side-effect classification, and idempotency requirements. Keep concrete
   instructions, scripts, and business Tool behavior outside Core.
2. Implement frozen public models, exact JSON serialization, and pure validators.
   Reject unknown fields, unsupported versions, invalid resource paths, duplicate
   references, inconsistent side-effect/idempotency declarations, and malformed
   schemas before consulting a loader or registry.
3. Add typed SkillRegistry and ToolRegistry facades over the Plugin capability
   registry. Preserve its immutable snapshots, owner-bound generation receipts,
   atomic visibility, deterministic lookup, and structured collision behavior;
   do not create a second independent publication transaction.
4. Add pure discovery plus loader protocols. Skill metadata is discoverable
   without loading its instructions, examples, scripts, or references. A resource
   request loads only named resources under explicit size, Scope, deadline, and
   cancellation bounds.
5. Add the authorized Tool execution gateway. Validate input before leasing,
   authorize the declared Action and resource, narrow Scope, acquire the owning
   Plugin lease, execute with deadline/cancellation control, validate output, and
   release in `finally`. Side-effecting Tools require a stable idempotency key.
6. Add the Skill resource gateway. Authorize each resource request, acquire the
   owning Plugin lease for the full read, reject access during DRAINING, and
   prevent unbounded or undeclared context injection.
7. Add shared resolution and validation adapters for AgentSpec references and
   future Workflow SkillNode/ToolNode consumers. Missing registrations,
   incompatible contracts, undeclared permissions, invalid schemas, or a
   side-effecting call without an idempotency identity must fail before execution.
   Keep Workflow scheduling and node execution in Task 4.1.
8. Add exact Skill/Tool/action/event fixtures and shared contract suites for two
   fake loaders/executors. Cover lazy loading, authorization denial, schema
   failure, duplicate side-effect retries, timeout/cancellation, acquire/drain
   races, unload during use, rollback, and recovery before marking Task 5.2
   Implemented.

Failure scenarios:

- Invalid or incompatible Skill/Tool contracts
- Eager or undeclared Skill resource loading
- Tool input/output schema mismatch
- Permission, Scope, or execution-policy denial
- Side-effecting Tool invocation without idempotency identity
- Timeout, cancellation, retry, or Plugin drain racing with execution

Acceptance:

- Registry conflicts produce structured errors.
- Skill resources load only when requested.
- Tool calls cannot bypass Scope, RuntimeCallContext, authorization, schema
  validation, Plugin leases, or declared execution policy.
- Side-effecting retries preserve one stable operation identity and never silently
  downgrade to at-most-once assumptions.
- Agent reference resolution and future Workflow adapters resolve only registered
  compatible references and never call loader implementations directly.
- Exact fixtures, full pytest coverage, Ruff, and Pyright remain green.

### Task 5.3 MCP Adapter

Status: In Progress (RFC-0014 Accepted)

Implement capability discovery, schema mapping, Context and Tool integration,
and policy enforcement.

Recommended next slice:

Start this work only after the Task 5.2 change has completed review and the full
verification gates provide a clean baseline. MCP adds a transport boundary; it
must adapt the existing capability boundaries rather than creating another
authorization, schema, retry, or lifecycle path.

Recommended delivery order:

1. Write and accept RFC-0014 before adding runtime code. Freeze MCP capability
   identity, discovery records, schema mapping, call context, error mapping,
   cancellation, deadlines, idempotency, lifecycle, and redacted events.
2. Add frozen transport-neutral descriptors and pure mapping validation. Keep a
   mandatory MCP SDK out of Core and reject malformed or unsupported remote
   capabilities before opening a connection or invoking a remote operation.
3. Adapt discovered MCP Tools through the existing `ToolGateway` contracts and
   adapt MCP Context access through the existing Context authorization boundary.
   Every operation receives `RuntimeCallContext` and uses `AuthorizedDispatcher`;
   the adapter must not expose a second direct invocation API.
4. Treat remote constraints as narrowing only. Remote discovery cannot replace
   a declared Action, Schema, Scope, side-effect class, idempotency identity, or
   local deadline. Plugin-backed adapters keep their Plugin lease for the entire
   remote call and validation boundary.
5. Add redacted discovery/invocation observability and structured transport error
   normalization. Payloads contain references, counts, latency, and safe codes,
   never remote arguments, results, credentials, exception text, or raw protocol
   frames.
6. Run one shared contract suite against two independent fake MCP transports.
   Cover discovery, schema mapping, default denial, invalid grants, Scope escape,
   timeout, cancellation, retry identity, malformed replies, disconnects,
   acquire/drain races, unload during use, and recovery after transport failure.

Explicit exclusions:

- Raw database, table, filesystem, or generic CRUD exposure
- Agent Tool execution loops and automatic Skill injection
- Workflow SkillNode/ToolNode scheduling
- Parallel Workflow scheduling or external Workflow engine integration
- A mandatory MCP client or server SDK dependency in Core

Failure scenarios:

- Discovery advertises an unsupported kind, version, Action, or Schema
- A remote capability broadens local Scope or grant constraints
- Input or output fails local schema validation
- Authorization, deadline, cancellation, or idempotency context is missing
- A transport disconnects, times out, returns malformed data, or completes late
- Plugin drain or unload races with an in-flight Plugin-backed MCP call

Acceptance:

- MCP exposes scoped capability, not raw database tables.
- MCP calls follow the same authorization, timeout, cancellation, event, and
  error contracts as local capabilities.
- No MCP adapter can bypass `RuntimeCallContext`, `AuthorizedDispatcher`, local
  Schema validation, stable operation identity, or a required Plugin lease.
- Discovery and invocation fail before remote side effects when local reference,
  Action, Schema, permission, Scope, or grant validation fails.
- Exact fixtures, dual-transport contract tests, full pytest coverage, Ruff, and
  Pyright remain green before Task 5.3 is marked Implemented.

## 7. Phase 6: Events, Storage, Observability, and Compatibility

### Task 6.1 Runtime Events and Event Sinks

Status: Implemented in 0.2.0

Implement the versioned envelope, per-Run sequence, observability and audit
classes, redaction, sink routing, acknowledgement, and deduplication.

Acceptance:

- Observability sink failure does not fail a Run.
- Audit sink failure pauses by default and may fail by policy.
- Reliable audit delivery is at least once and duplicate event IDs are safe.
- Runtime state can be reconstructed from state stores without event replay.

### Task 6.2 Storage Abstractions

Status: In Progress

Delivered:

- Replaceable RunRepository with a thread-safe in-memory compare-and-set adapter
- Replaceable SessionRepository with an in-memory lifecycle adapter
- Versioned WorkspaceState updates
- Replaceable EventSequenceStore and AuditOutbox with in-memory and SQLite
  adapters
- Authorized CheckpointStore contract and thread-safe in-memory adapter

Remaining:

- Workspace and Artifact repository contracts
- StorageProvider boundary and authorization for all state access
- Common contract tests shared by in-memory and external adapters
- Standard storage failure mapping and compatibility fixtures

Implement replaceable Workspace, Artifact, Session, Run, and Checkpoint storage
contracts and adapters without selecting a mandatory database.

Acceptance:

- In-memory adapters pass the same contract suite as external adapters.
- Scope and authorization are preserved at every storage boundary.
- Storage failures map to standard structured errors.

### Task 6.3 Compatibility Suite

Status: In Progress

Delivered:

- Stable v0.2 JSON fixtures for Content, ContextBinding, ModelBinding, AgentSpec,
  Memory, Checkpoint, approval, Workflow, and Evaluation contracts, plus Provider,
  Checkpoint, Workflow, and Evaluation action catalogs and the Core event catalog
- Exact deserialize, equality, and reserialize checks for delivered fixtures

Remaining:

- Storage contract fixtures after those public schemas are implemented
- Migration fixtures and compatibility checks for future contract versions

Add compatibility fixtures for public schemas, provider contracts, plugin
manifests, event envelopes, and checkpoint formats.

Acceptance:

- Contract version changes are detected automatically.
- Breaking fixtures require migration documentation.
- Existing compatible plugins and providers continue to pass.

## 8. Global Constraints

- No business coupling or application workflow in Core.
- Providers and execution engines remain replaceable.
- Authorization is deny-by-default.
- Recovery is at least once and side effects are idempotent.
- Runtime Events are not Event Sourcing.
- No mandatory dependency on a specific Agent framework or infrastructure.
