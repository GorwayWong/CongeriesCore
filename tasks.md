# Agent Harness Runtime Core Tasks

Version: 0.2.0

## 1. Delivery Rules

Implementation follows [principles.md](principles.md),
[requirements.md](requirements.md), [design.md](design.md), and the Accepted RFC
registry in [docs/rfcs/README.md](docs/rfcs/README.md).

Every task requires unit tests for its public contract and integration tests for
cross-module behavior. Failure-path tests are part of acceptance, not optional
follow-up work.

## 2. Phase 1: Architecture Baseline and Core Types

### Task 1.1 Project Structure

Create modules for runtime, workflow, harness, state, policy, provider, plugin,
skill, tool, event, checkpoint, and adapter contracts.

Acceptance:

- Core builds and runs without an application plugin.
- No module contains business or vendor-specific types.
- Public modules depend on interfaces rather than concrete infrastructure.

### Task 1.2 Shared Runtime Types

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

Implement SessionRef with OPEN/CLOSED lifecycle and Workspace state ownership.

Acceptance:

- Multiple Runs may share an authorized SessionRef or Workspace.
- Closing a Session prevents new associations without deleting Run history.
- Core Session state stores no messages, participants, or user model.

### Task 2.3 Scope and Authorization

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

Implement ContextProvider, provider selection, ContextResolver, injection, and
complete or partial ContextResult handling.

Acceptance:

- Provider selection is explicit and deterministic under policy.
- Provide and capability-discovery calls receive RuntimeCallContext and pass
  authorization.
- Denied, timeout, cancelled, partial, and unavailable outcomes remain distinct.
- Hidden global context and implicit database access are absent.

### Task 3.2 MemoryProvider

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

## 5. Phase 4: Workflow, Checkpoint, and Harness

### Task 4.1 Workflow Runtime

Implement Workflow definition, input and output schemas, graph validation,
ExecutionPolicy, WorkflowContext, WorkflowResult, and Agent, Skill, Tool,
Context, Approval, and Evaluation nodes.

Acceptance:

- Invalid graphs and schemas fail before execution.
- Independent nodes execute only when dependency and policy rules allow.
- Workflow engines can be replaced through an adapter contract.
- No predefined business workflow exists in Core.

### Task 4.2 CheckpointStore and Recovery

Implement atomic save, load, list, and delete; stable node boundaries; graph
version validation; recovery attempts; and checkpoint migration hooks.

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

Implement pause, resume, cancellation, retry, recovery, approval decisions,
schema validation, policy checking, and quality evaluation.

Acceptance:

- Deadlines and cancellation propagate to active child calls.
- Approval accepts only authorized, correlated decisions.
- Retry and recovery increment attempts and emit state transitions.
- Evaluation cannot turn a denied or failed result into success.

## 6. Phase 5: Plugin SDK, Skill, Tool, and MCP

### Task 5.1 Plugin SDK and Safe Unload

Implement manifest validation, dependency resolution, capability registration,
active leases, drain, unregistration, unload, and lifecycle events.

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

Implement discovery, versioned registration, progressive Skill loading, typed
Tool schemas, authorization, execution policy, and idempotency declaration.

Acceptance:

- Registry conflicts produce structured errors.
- Skill resources load only when requested.
- Tool calls cannot bypass Scope or RuntimeCallContext.

### Task 5.3 MCP Adapter

Implement capability discovery, schema mapping, Context and Tool integration,
and policy enforcement.

Acceptance:

- MCP exposes scoped capability, not raw database tables.
- MCP calls follow the same authorization, timeout, cancellation, event, and
  error contracts as local capabilities.

## 7. Phase 6: Events, Storage, Observability, and Compatibility

### Task 6.1 Runtime Events and Event Sinks

Implement the versioned envelope, per-Run sequence, observability and audit
classes, redaction, sink routing, acknowledgement, and deduplication.

Acceptance:

- Observability sink failure does not fail a Run.
- Audit sink failure pauses by default and may fail by policy.
- Reliable audit delivery is at least once and duplicate event IDs are safe.
- Runtime state can be reconstructed from state stores without event replay.

### Task 6.2 Storage Abstractions

Implement replaceable Workspace, Artifact, Session, Run, and Checkpoint storage
contracts and adapters without selecting a mandatory database.

Acceptance:

- In-memory adapters pass the same contract suite as external adapters.
- Scope and authorization are preserved at every storage boundary.
- Storage failures map to standard structured errors.

### Task 6.3 Compatibility Suite

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
