# Agent Harness Runtime Core Design

Version: 0.2.0

## 1. Role of This Document

This document describes the current v0.2 architecture and how its normative
contracts compose. Detailed fields, state transitions, and failure semantics
belong to the Accepted RFC linked by each section.

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
binding. It does not contain provider implementation objects.

### 3.2 Workflow

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

## 4. Run, Session, and Workspace

### 4.1 Run Envelope

Run is the common execution envelope. AgentRun and WorkflowRun are peer types,
and either may be a root Run.

```text
Run
├── identity: run_id, kind, definition_id
├── relationships: root_run_id, parent_run_id
├── boundaries: workspace_id, session_ref, scope
├── control: status, attempt, policy
└── record: timestamps, error_summary
```

Workflow AgentNode execution creates an AgentRun whose parent is the executing
WorkflowRun. Direct Agent execution creates a root AgentRun.

The normative state machine is
[RFC-0004](docs/rfcs/RFC-0004-execution-run-lifecycle.md).

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

### 6.2 Context Harness

```text
Run -> ContextResolver -> Selected ContextProviders -> ContextResult -> Runtime
```

The resolver applies authorization before provider invocation and has explicit
complete, partial, denied, timeout, cancelled, and failed outcomes.

See [RFC-0006](docs/rfcs/RFC-0006-context-provider.md).

### 6.3 Memory Harness

Core owns the MemoryProvider protocol only:

```text
retrieve | remember | forget | optional consolidate
```

Memory implementation, ranking, schemas, embeddings, and persistence belong to
plugins. See [RFC-0007](docs/rfcs/RFC-0007-memory-provider.md).

### 6.4 Approval and Evaluation

Approval pauses a Run in WAITING_APPROVAL, writes a checkpoint, emits a reliable
audit event, and resumes only after an authorized decision.

Evaluation performs schema validation, policy checks, and quality evaluation.
Evaluation cannot silently convert a failed or denied result into success.

## 7. Provider Layer

Core defines replaceable ContextProvider, MemoryProvider, ModelProvider,
StorageProvider, CheckpointStore, AuthorizationPolicy, and EventSink contracts.

ModelProvider supports vendor-neutral generation, streaming, capability
discovery, usage, deadline, cancellation, structured output, and structured
errors. See [RFC-0009](docs/rfcs/RFC-0009-model-provider.md).

Storage remains an abstraction:

```text
Core Contract -> Provider or Adapter -> External Implementation
```

## 8. Checkpoint and Recovery

CheckpointStore atomically saves and loads stable workflow boundaries. Recovery
uses at-least-once node execution, so a node may be replayed. Side-effecting
operations require idempotency keys.

A checkpoint records graph version, sequence, node state, pending nodes,
attempt, references to external state, idempotency data, and integrity data.
Graph-version mismatch requires an explicit migrator.

See [RFC-0011](docs/rfcs/RFC-0011-checkpoint-recovery.md).

## 9. Runtime Events

RuntimeEvent uses a versioned envelope and a per-Run sequence. Events fall into
two delivery classes:

- Observability events are non-blocking.
- Approval, authorization, and security audit events require reliable
  acknowledgement and at-least-once delivery.

Audit sink failure pauses the Run by default; execution policy may fail it.
Runtime Events do not own execution state and do not imply Event Sourcing.

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
[tasks.md](tasks.md). This design introduces no implementation framework or
vendor dependency.
