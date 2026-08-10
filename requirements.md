# Agent Harness Runtime Core Requirements

Version: 0.2.0

## 1. Purpose

CongeriesCore is a lightweight, extensible, business-independent runtime for
executing and controlling Agents and Workflows. Core provides runtime capability;
applications provide domain capability through extensions.

## 2. Goals

Core shall support:

- Direct Agent execution and Workflow-based execution
- Long-running, pausable, recoverable execution
- Personal and enterprise runtime deployment scenarios without domain coupling
- Plugin-based business and integration capability
- Provider-independent context, memory, model, and storage capability
- Explicit authorization, approval, audit, and execution policy
- Replaceable workflow engines and infrastructure adapters

## 3. Non-Goals

Core does not provide:

- Business, user, customer, organization, or industry models
- Application-specific workflows or APIs
- Concrete memory, model, database, or event-broker implementations
- Conversation history ownership
- Full Event Sourcing
- Exactly-once distributed execution
- Mandatory coupling to one Agent or Workflow framework

## 4. Runtime Concepts

Core understands the following runtime concepts:

- Agent and Workflow
- Run, AgentRun, and WorkflowRun
- Workspace, SessionRef, and Artifact
- Skill and Tool
- Context, ScopeRef, and RuntimeCallContext
- Policy, AuthorizationPolicy, and Provider
- RuntimeEvent and Checkpoint
- Plugin and Adapter

Application-defined values may travel through these contracts, but Core must not
interpret their business meaning.

## 5. Execution Requirements

### 5.1 Run Model

- AgentRun and WorkflowRun shall be peer Run types.
- Either type may be a root Run.
- Nested Runs shall expose parent and root relationships.
- Workflow AgentNode execution shall create a child AgentRun.
- Run state transitions, attempts, timestamps, cancellation, and error summaries
  shall be observable.
- Terminal states shall be irreversible.

### 5.2 Session and Workspace

- Workspace shall own durable execution state and artifacts.
- SessionRef shall correlate and isolate related Runs.
- Session lifecycle shall support OPEN and CLOSED.
- Core Session state shall not own messages, participants, user profiles, or
  business state.

### 5.3 Execution Harness

The Execution Harness shall support:

- Start, pause, resume, cancel, retry, and recovery
- Deadline and timeout propagation
- Human approval waits and decisions
- Explicit attempts for retry and recovery
- Validation of legal state transitions

### 5.4 Workflow and Checkpoint

Workflow shall define an input schema, node graph, dependencies, output schema,
and execution policy.

Core shall provide an atomic CheckpointStore contract. Checkpoints shall be
committed at stable node boundaries and around approval waits. Recovery shall
provide at-least-once node execution. Side-effecting nodes shall use idempotency
keys. Graph-version mismatch shall reject recovery unless a migration is
registered.

## 6. Capability Requirements

### 6.1 Agent

Agent shall compose identity, instructions, skills, tools, explicit context,
policy, and a model binding. AgentSpec shall reference a ModelProvider and model
identifier rather than a vendor SDK object.

### 6.2 Skill and Tool

- Skill shall describe reusable capability and progressive resource loading.
- Tool shall define input and output schemas, permission requirements, execution
  policy, deadline behavior, and idempotency behavior.
- Tool and MCP calls shall not expose uncontrolled CRUD or internal access.

### 6.3 ContextProvider

Context shall be resolved and injected explicitly through providers. The
contract shall define provider selection, complete and partial results, denial,
timeout, cancellation, and failure behavior.

### 6.4 MemoryProvider

Core shall define only the MemoryProvider protocol. It shall expose `retrieve`,
`remember`, `forget`, and optional `consolidate` operations with typed requests,
results, Scope, pagination where applicable, and structured errors.

Memory implementations and storage strategies belong to plugins.

### 6.5 ModelProvider

Core shall define vendor-neutral `generate`, `stream`, and `capabilities`
operations. The contract shall support structured output, usage, deadlines,
cancellation, policy, and structured errors without exposing vendor types.

### 6.6 Shared Provider Errors

Provider contracts shall represent at least invalid request, denied,
unavailable, timeout, cancelled, conflict, version mismatch, and partial-result
outcomes.

## 7. Security Requirements

- Every Tool, Provider, and MCP call shall receive RuntimeCallContext.
- RuntimeCallContext shall carry Run relationships, Workspace, optional
  SessionRef, Scope, deadline, cancellation, trace, and idempotency data.
- ScopeRef shall be generic and namespaced.
- Core shall define only runtime scope kinds; application scope kinds remain
  plugin-defined.
- AuthorizationPolicy shall evaluate principal, action, resource, and Scope.
- Authorization shall deny access by default.
- Approval, denied authorization, and cross-scope grants shall emit audit events.
- Sensitive event and checkpoint data shall support redaction or reference-based
  storage.

## 8. Plugin and Extension Requirements

Plugins may provide Workflows, Skills, Tools, ContextProviders,
MemoryProviders, ModelProviders, StorageProviders, and MCP adapters.

Core shall support:

- Manifest validation and capability registration
- Dependency and version declaration
- Discovery, validation, load, registration, activation, drain, unregistration,
  and unload
- Idempotent unload
- Rejection of new work while draining
- Active execution leases that prevent premature disposal
- Drain timeout with retry or explicit return to ACTIVE
- Skill registry, MCP integration, and StorageProvider abstraction

## 9. Runtime Event Requirements

RuntimeEvent shall use a versioned envelope containing identity, type, schema
version, timestamp, Run relationships, per-Run sequence, Scope, correlation,
causation, sensitivity classification, and payload.

Core shall distinguish:

- Non-blocking observability events
- Reliably acknowledged approval, authorization, and security audit events

Audit delivery shall be at least once and deduplicated by event identity. Audit
sink failure shall pause execution by default; execution policy may choose
failure. Runtime Events shall not be the sole source of runtime state.

Business events remain outside Core.

## 10. Quality Requirements

Core shall be:

- Lightweight and modular
- Business-independent and provider-independent
- Testable and observable
- Secure by default
- Deterministic at declared workflow boundaries
- Recoverable with documented at-least-once semantics
- Extensible without Core modification
- Compatible through versioned public contracts
- Free of mandatory third-party production dependencies in the v0.2 reference
  package; development and verification tooling may remain external

Detailed contracts are indexed in [docs/rfcs/README.md](docs/rfcs/README.md), and
accepted cross-cutting decisions are indexed in
[docs/adrs/README.md](docs/adrs/README.md).
