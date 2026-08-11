# Agent Harness Runtime Core Design

Version: 0.2.0

## 1. Role of This Document

This document describes the current v0.2 architecture and how its normative
contracts compose. Detailed fields, state transitions, and failure semantics
belong to the Accepted or Implemented RFC linked by each section.

The design complies with [principles.md](principles.md), satisfies
[requirements.md](requirements.md), and applies the decisions indexed in
[docs/adrs/README.md](docs/adrs/README.md).

## 2. Architecture

```text
Application Services
        |
Application Plugins
  Workflows | Skills | Tools | Providers | MCP
        |
+---------------------------------------------------+
| CongeriesCore                                     |
|                                                   |
|  Runtime      Harness         State               |
|  Agent        Execution       Workspace           |
|  Workflow     Context         SessionRef          |
|  Run          Memory          Artifact            |
|               Approval        Checkpoint          |
|               Evaluation                          |
|                                                   |
|  Policy       Events          Extension           |
|  Scope        Observability   Plugin Registry     |
|  Authorization Audit          Adapter Registry    |
+---------------------------------------------------+
        |
Provider and Adapter Interfaces
        |
Models | Storage | Workflow Engines | MCP | Event Sinks
```

Core owns runtime contracts and control. Applications own domain meaning.

## 3. Runtime Composition

### 3.1 Agent

Agent is a runtime composition:

```text
Agent = Identity
      + Instructions
      + Skills
      + Tools
      + Context Binding
      + Policy
      + Model Binding
```

AgentSpec references registered capabilities and a vendor-neutral ModelProvider
binding. Legacy v1 ResourceRef values remain compatible; AgentSpec v2 uses exact
versioned CapabilityRef values. It does not contain provider implementation
objects.

AgentRegistry resolves an exact Agent and definition identity. The minimal
direct Agent Runtime validates Run, AgentSpec, binding, Scope, Workspace, and
Session identity before execution, then coordinates:

```text
CREATED -> STARTING -> CONTEXT_LOADING
    -> authorized ContextResolver
    -> RUNNING
    -> authorized ModelProvider.generate
    -> SUCCEEDED
```

Expected Provider and policy failures return `AgentExecutionResult` with the
committed AgentRun and structured error. Cancellation maps to CANCELLED; denied,
timeout, unavailable, malformed, and unacceptable partial results map to FAILED.
A reliable audit failure preserves the PAUSED or policy-selected FAILED state
already committed by RunService.

SkillToolResolver preflights all declared Skill and Tool references before Run,
Context, or Model effects. The direct Agent runtime neither loads Skill content
nor executes Tool proposals.

### 3.2 Skill and Tool

SkillRegistry and ToolRegistry are typed read-only views over the same atomic
Plugin capability snapshot. Skill metadata discovery is pure. Skill resources
load one named resource through authorization and one owning Plugin lease. Tool
calls validate input Schema before authorization, execute all allowed retries
under one lease and stable operation identity, then validate output Schema before
release. Loaders and executors never escape the leased invocation callback.

The normative contract is
[RFC-0013](docs/rfcs/RFC-0013-skill-tool-contracts.md).

### 3.3 Workflow

Workflow is an executable graph:

```text
Workflow = Definition
         + Input Schema
         + Node Graph
         + Output Schema
         + Execution Policy
```

Node types include Agent, Skill, Tool, Context, Approval, and Evaluation. An
AgentNode creates a child AgentRun. Workflow engines remain replaceable through
adapters.

The normative contract is
[RFC-0003](docs/rfcs/RFC-0003-workflow.md).

The minimal direct Workflow runtime now provides immutable versioned contracts,
pre-execution DAG validation, deterministic one-at-a-time dependency scheduling,
AgentNode child Runs, authorized durable node-output references, checkpoint-based
recovery, and ApprovalNode coordination. Checkpoint, Run marker, restoration,
migration, fallback, and approval services remain independently replaceable.
EvaluationNode composes the provider-neutral Evaluation harness and persists
both successful and non-successful boundaries. Skill, Tool, Context, custom node
execution, parallel scheduling, and external engine adapters remain deferred;
their delivery order is owned by
[tasks.md](tasks.md).

## 4. Run, Session, and Workspace

### 4.1 Run Envelope

Run is the common execution envelope. AgentRun and WorkflowRun are peer types,
and either may be a root Run.

```text
Run
├── identity: run_id, kind, definition_id
├── relationships: root_run_id, parent_run_id
├── boundaries: workspace_id, session_ref, scope
├── control: status, attempt, continuation_status, policy
└── record: timestamps, error_summary, attempt_history
```

Workflow AgentNode execution creates an AgentRun whose parent is the executing
WorkflowRun. Direct Agent execution creates a root AgentRun.

The normative state machine is
[RFC-0004](docs/rfcs/RFC-0004-execution-run-lifecycle.md).

Run mutation separates pure lifecycle calculation from asynchronous
coordination:

```text
Lifecycle Command
    -> RunService
    -> RunStateMachine (pure transition)
    -> RunRepository compare-and-set
    -> RunStateChanged publication after commit
```

RunRepository is replaceable. The in-memory reference implementation protects
compare-and-set with a process-local thread lock so competing completion and
cancellation commands cannot both commit.

For WorkflowRun, `latest_checkpoint_ref` is a compare-and-set recovery marker.
Checkpoint Store durability precedes the marker mutation; only the marker makes
a stored checkpoint eligible for recovery. Recovery opens a new attempt in
RECOVERING and records the actual source reference before restoration begins.

### 4.2 SessionRef

SessionRef correlates related root Runs and contributes an isolation boundary.
Its lifecycle is OPEN or CLOSED. Core Session state does not store messages,
participants, user records, or application state.

### 4.3 Workspace

Workspace owns durable execution state and Artifacts. It may be shared by Runs
according to Scope and AuthorizationPolicy. Checkpoints reference Workspace,
Context, Memory, and Artifact state without making Runtime Events the source of
truth.

## 5. Runtime Call Boundary

Every Tool, Provider, and MCP call receives RuntimeCallContext:

```text
RuntimeCallContext
├── run_id, root_run_id, parent_run_id
├── workspace_id, optional session_ref
├── scope
├── deadline and cancellation
├── trace and correlation
└── idempotency_key
```

Derived child calls retain cancellation propagation and may only narrow Scope
or shorten a parent deadline. They cannot silently broaden either boundary.
CancellationToken provides an asynchronous wait boundary so Provider tasks can
be actively cancelled while Core is awaiting them. Core checks cancellation and
deadline both before and after each Provider await; late results are discarded.

Before dispatch, AuthorizationPolicy evaluates an AccessRequest containing the
runtime principal, action, resource, and ScopeRef. Missing authorization is
denied. The normative security contract is
[RFC-0008](docs/rfcs/RFC-0008-scope-authorization.md).

## 6. Harness Layer

### 6.1 Execution Harness

Execution Harness starts, pauses, resumes, cancels, retries, and recovers Runs.
It validates state transitions, propagates deadline and cancellation, manages
attempts, and coordinates approval and checkpoint boundaries.

A retryable attempt failure closes the attempt and moves the non-terminal Run
to RETRYING without entering FAILED. Retry redispatch increments attempt and
returns to the failed resumable phase. A non-retryable failure or exhausted
retry policy moves the Run to irreversible FAILED.

`continuation_status` is persisted only while a Run is PAUSED or RETRYING. A
PAUSED Run records the resumable phase selected for `resume`; a RETRYING Run
records the failed phase selected for retry redispatch. Resume and redispatch
clear the field atomically while opening the current attempt if necessary.

### 6.2 Context Harness

```text
Run -> ContextResolver -> Selected ContextProviders -> ContextResult -> Runtime
```

The resolver applies authorization before provider invocation and has explicit
complete, partial, denied, timeout, cancelled, and failed outcomes.

ContextBinding contains only ordered Provider references, typed requirements,
budget, merge strategy, and completeness policy. ContextProviderRegistry owns
implementations. ContextResolver performs authorized capability discovery and
supports deterministic `single`, `first_success`, `merge`, and `all` behavior.
SchemaRegistry validates entries, and ContextMergeRegistry requires an explicit
schema merge policy for conflicting values.

See [RFC-0006](docs/rfcs/RFC-0006-context-provider.md).

### 6.3 Memory Harness

Core owns an independent authorized Memory gateway:

```text
Caller -> MemoryGateway -> AuthorizedDispatcher -> MemoryProviderRegistry
       -> retrieve | remember | forget | optional consolidate
```

MemoryGateway performs authorized capability discovery before every operation,
validates content through SchemaRegistry, enforces cursor query fingerprints,
and applies operation-specific grant narrowing. It actively cancels outstanding
Provider work on cancellation or deadline and discards late results.

MemoryProviderRegistry owns implementations. Query, item, and consolidation
values contain references and JSON data rather than Provider or storage objects.
AgentRuntime does not automatically retrieve or persist Memory in v0.2. Memory
implementation, ranking, schemas, embeddings, consolidation algorithms, and
persistence belong to plugins. See
[RFC-0007](docs/rfcs/RFC-0007-memory-provider.md).

### 6.4 Approval and Evaluation

Approval pauses a Run in WAITING_APPROVAL, writes a checkpoint, emits a reliable
audit event, and resumes only after an authorized, correlated decision is
durably captured by a second checkpoint and audit event. Approval state belongs
to checkpoints rather than Runtime Event replay.

Evaluation composes pure schema validation, an independent content-policy
boundary, and one replaceable quality evaluator in a fixed fail-fast sequence.
It cannot silently convert a failed or denied result into success. A reliable
verdict audit acknowledgement precedes durable result persistence and a stable
Checkpoint. Both successful and non-successful Evaluation nodes are durable;
only successful nodes unlock dependents. The provider-neutral public contract
reuses Scope, RuntimeCallContext, access authorization, Runtime Events, and
Checkpoint recovery as specified by
[RFC-0012](docs/rfcs/RFC-0012-evaluation.md).

The reference runtime makes the effect order visible:

```text
input
  -> pure SchemaEvaluator
  -> authorized EvaluationPolicyGateway
  -> authorized QualityEvaluatorGateway
  -> required verdict AUDIT acknowledgement
  -> durable EvaluationResult reference
  -> Checkpoint compare-and-swap
  -> mark completed only when verdict == passed
```

Every non-success arrow stops before the next evaluator. After the audit gate,
the runtime persists that non-success result through `error_ref`, commits the
stable node outcome, and terminalizes the Run without marking the node complete.
The detailed review path is documented in the
[Evaluation Pipeline Code Review Guide](docs/reviews/evaluation-pipeline-code-review.md).

## 7. Provider Layer

Core defines replaceable ContextProvider, MemoryProvider, ModelProvider,
QualityEvaluator, StorageProvider, CheckpointStore, AuthorizationPolicy,
EvaluationPolicy, and EventSink contracts.

ModelProvider supports vendor-neutral generation, streaming, capability
discovery, usage, deadline, cancellation, structured output, and structured
errors. See [RFC-0009](docs/rfcs/RFC-0009-model-provider.md).

ModelProviderRegistry owns implementations while ModelBinding stores a binding
reference, primary Provider/model selector, capability requirements, defaults,
and ordered fallback selectors. ModelGateway is the only generation and stream
path. It applies authorization constraints, validates structured output through
SchemaRegistry, normalizes stream termination, and closes cancellable streams.
Fallback occurs only for unavailable or unsupported capability outcomes.

Storage remains an abstraction:

```text
Core Contract -> Provider or Adapter -> External Implementation
```

## 8. Checkpoint and Recovery

CheckpointStore atomically saves, loads, lists, and deletes stable workflow
boundaries behind AuthorizedDispatcher. Recovery uses at-least-once node
execution, so a node may be replayed. Side-effecting operations require stable
idempotency keys and request fingerprints.

A checkpoint records graph version, sequence, node state, pending nodes,
attempt, references to external state, idempotency data, and integrity data.
Canonical SHA-256 protects the complete versioned payload. Store writes become
committed recovery points only after WorkflowRun marker compare-and-set; failed
marker updates leave explicit orphans. Only true orphans may be deleted.

Graph or definition mismatch requires an explicit, non-destructive migrator.
Corrupt fallback is disabled by default. Migration and explicit fallback require
reliable audit acknowledgement before their Run marker mutation.

See [RFC-0011](docs/rfcs/RFC-0011-checkpoint-recovery.md).

## 9. Runtime Events

RuntimeEvent uses a versioned envelope and a per-Run sequence. Events fall into
two delivery classes:

- Observability events are non-blocking.
- Approval, authorization, and security audit events require reliable
  acknowledgement and at-least-once delivery.

Checkpoint saved/failed events are observability. Approval request/decision and
checkpoint migration/fallback authorization events are reliable audit.

Audit sink failure pauses the Run by default; execution policy may fail it.
Runtime Events do not own execution state and do not imply Event Sourcing.

Context resolution, Memory operations, and Model invocation emit redacted
OBSERVABILITY events with references, counts, usage, latency, outcome, and safe
error codes. They never include Context or Memory values, query content,
metadata, cursors, provenance content, prompts, or generated output. Their
delivery failure does not alter Run state.

```text
RuntimeEvent
    -> schema validation
    -> EventSink authorization
    -> explicit sensitivity redaction
    -> OBSERVABILITY: bounded asynchronous queue
    -> AUDIT: durable outbox -> required acknowledgements
```

EventSequenceStore and AuditOutbox are replaceable ports. The reference SQLite
adapter uses WAL, transactions, a busy timeout, and thread offloading. Runtime
state remains in Run, Session, and Workspace stores rather than the outbox.

See [RFC-0010](docs/rfcs/RFC-0010-runtime-events.md).

## 10. Plugin System

```text
plugin/
├── manifest.yaml
├── workflows/
├── skills/
├── tools/
├── providers/
└── mcp/
```

The lifecycle is:

```text
DISCOVERED -> VALIDATED -> LOADED -> REGISTERED -> ACTIVE
ACTIVE -> DRAINING -> UNREGISTERED -> UNLOADED
```

Execute is behavior while ACTIVE, not a lifecycle state. DRAINING rejects new
work and waits for active leases. Unload is idempotent. A drain timeout leaves
the plugin in DRAINING until drain is retried or explicitly cancelled.

Pure Manifest validation is separated from environment preflight and lifecycle
authorization. Dependency resolution consumes immutable catalog snapshots.
Capability publication uses an ownership-aware registry transaction whose
committed snapshot changes atomically. Lifecycle commands reuse
RuntimeCallContext, AuthorizedDispatcher, and reliable redacted Runtime Events.

See [RFC-0002](docs/rfcs/RFC-0002-plugin-sdk.md).

## 11. MCP and Application Integration

MCP is a scoped capability gateway for Tool and Context capability. It is not a
CRUD gateway. MCP calls use RuntimeCallContext and AuthorizationPolicy.

HTTP frameworks, API schemas, user sessions, and application services stay
outside Core:

```text
Application API -> Application Service -> CongeriesCore -> Extensions
```

## 12. Standard Failure Model

Provider and external capability contracts represent at least:

- Invalid request
- Denied
- Unavailable
- Timeout
- Cancelled
- Conflict
- Version mismatch
- Partial result

Each owning RFC defines whether an outcome is retryable and how it maps to Run
state, audit, and checkpoint behavior.

## 13. Delivery Roadmap

Implementation phases and acceptance criteria are defined in
[tasks.md](tasks.md), which is the single progress tracker. This design
introduces no implementation framework or vendor dependency.
