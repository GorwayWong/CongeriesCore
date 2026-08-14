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

Workflow ContextNode shall embed a typed ContextBinding, accept no Workflow value
input, and produce the fixed versioned ContextNode result Schema. Validation shall
confirm binding Schemas, Context actions, and exact Provider permission resources
before RUNNING or the initial Checkpoint. Execution shall use only the injected
ContextResolver. A successful result shall be persisted by reference before its
stable Checkpoint may unlock dependents. Partial, denial, timeout, cancellation,
late result, Provider or Schema failure, and recovery behavior shall preserve the
ContextProvider, authorization, checkpoint, and idempotency contracts.

Workflow SkillNode shall name one declared read-only Skill resource, accept no
Workflow value input, and produce the fixed versioned SkillNode result Schema.
Validation shall resolve the exact Skill owner, version, resource, Action, budget,
and permission before RUNNING. Runtime shall load only through the injected Skill
resource gateway, persist the typed result before Checkpoint, skip committed reads,
and replay interrupted reads sequentially with the same logical identity.

Workflow ToolNode shall freeze the Tool call, descriptor snapshot, Scope, timeout,
stable idempotency key, canonical request fingerprint, and typed result contract.
Validation shall require one exact input binding, registered input/output Schemas,
Action and permission, descriptor-consistent side-effect classification, and
caller-key idempotency for external effects. Runtime shall dispatch only through
ToolGateway and shall commit a pre-dispatch Checkpoint before executor entry.

Side-effecting Tool operations shall use an independently durable, replaceable
Tool Operation Log with compare-and-set transitions. An uncertain post-dispatch
outcome shall become `unknown`, keep the node pending, pause the same WorkflowRun,
never unlock dependents, and never automatically replay or query the external
system. An authorized application actor may resolve unknown only with an expected
record version and durable evidence. Confirmed success resumes the same Run after
output Schema validation; confirmed failure commits a stable error and terminates
the Run. Checkpoint v1 shall remain byte-compatible.

### 5.5 Evaluation

Evaluation shall apply schema validation, content policy evaluation, and one
replaceable quality evaluator in that order. The first non-success verdict shall
terminate evaluation. Content policy is distinct from access authorization, and
Core shall not define business rubrics, scores, thresholds, or evidence storage.

Evaluation verdicts shall be typed, auditable, and durable. A required audit
acknowledgement shall precede every stable Evaluation node boundary. Successful
and non-successful results shall be persisted by reference, but only a committed
successful result may unlock downstream Workflow nodes. Recovery shall not
redispatch a committed non-successful Evaluation node.

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

### 6.6 StorageProvider

Core shall define replaceable Workspace and immutable Artifact repository
contracts behind an authorized StorageProvider boundary. Workspace writes shall
use compare-and-set versions. Artifact writes shall validate caller-supplied
identity, byte length, SHA-256, UTC creation time, Scope, and owning Workspace;
same-identity same-content replay shall be idempotent and different content shall
conflict. Artifact list operations shall use stable scoped pagination.

All StorageProvider operations shall receive RuntimeCallContext, deny by default,
normalize backend failures, and emit redacted events without Artifact content,
business metadata, Workspace values, or backend detail. Core shall provide no
mandatory database. Artifact update, deletion, garbage collection, and retention
require a later contract that preserves or migrates durable references.

### 6.7 Shared Provider Errors

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
- Approval, Evaluation verdicts, denied authorization, and cross-scope grants
  shall emit audit events.
- Sensitive event and checkpoint data shall support redaction or reference-based
  storage.

## 8. Plugin and Extension Requirements

Plugins may provide Workflows, Skills, Tools, ContextProviders,
MemoryProviders, ModelProviders, QualityEvaluators, StorageProviders, and MCP
adapters.

Core shall support:

- Manifest validation and capability registration
- Dependency and version declaration
- Discovery, validation, load, registration, activation, drain, unregistration,
  and unload
- Idempotent unload
- Rejection of new work while draining
- Active execution leases that prevent premature disposal
- Drain timeout with retry or explicit return to ACTIVE
- Deterministic dependency plans and atomic registration visibility
- Recoverable registration rollback and lifecycle-hook failure
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
