# CongeriesCore System Overview

Target Version: 0.2.0
Status: Non-Normative

## Positioning

CongeriesCore is a lightweight, business-independent runtime for executing and
controlling Agents and Workflows. Applications add domain behavior through
plugins, providers, adapters, workflows, skills, tools, and MCP capability.

The normative project boundary is defined by
[principles](../principles.md) and [requirements](../requirements.md).

## Architecture Map

```text
Application Services
        |
Application Plugins
  Workflows | Skills | Tools | Providers | MCP
        |
CongeriesCore
  Runtime      Agent | Workflow | Run
  Harness      Execution | Context | Memory | Approval | Evaluation
  State        Workspace | SessionRef | Artifact | Checkpoint
  Policy       Scope | Authorization
  Operations   Runtime Events | Audit | Observability
  Extension    Plugin Registry | Adapter Registry
        |
Provider and Adapter Interfaces
        |
Models | Storage | Workflow Engines | MCP | Event Sinks
```

The current composition is described in [design.md](../design.md).

## Core and Application Boundary

Core handles execution identity, lifecycle control, provider contracts,
authorization, checkpoints, events, and extension loading.

Applications handle domain workflows, business data, user-facing APIs, memory
semantics, model selection, and concrete infrastructure.

## Runtime Concepts

### Run

Run is the common execution envelope. AgentRun and WorkflowRun are peer types.
Both can be root runs, and Workflow AgentNode execution creates a child AgentRun.

### SessionRef and Workspace

SessionRef correlates related runs and contributes an isolation boundary. It
does not contain conversation history or participants. Workspace owns durable
execution state and artifacts.

### RuntimeCallContext and Scope

Tool, Provider, and MCP calls receive a RuntimeCallContext. ScopeRef describes
the authorized runtime boundary. Application scopes use plugin namespaces rather
than becoming built-in business concepts.

### Workflow and Checkpoint

Workflow defines a typed node graph and execution policy. Stable node boundaries
can be checkpointed. Recovery can replay a node, so side-effecting operations
use idempotency keys. The implemented CheckpointStore contract uses an authorized
gateway and a WorkflowRun compare-and-set marker; stored orphans are never chosen
implicitly for recovery.

## Typical Execution Flow

The implemented v0.2 direct Agent path is:

```text
Create root AgentRun
    -> STARTING
    -> CONTEXT_LOADING
    -> authorize Context capability discovery and resolution
    -> RUNNING
    -> authorize Model capability discovery and generation
    -> SUCCEEDED, FAILED, or CANCELLED
```

ContextProvider and ModelProvider implementations are resolved from injected
registries. AgentSpec, ContextBinding, and ModelBinding contain references, not
live Provider or vendor SDK objects. Context and Model calls cannot bypass
AuthorizedDispatcher.

AgentSpec can also carry exact, versioned Skill and Tool references. AgentRuntime
preflights those references before changing Run state or calling Context or
Model providers. It passes only Tool identities to the Model request; it does not
load Skill resources, inject Skill content, or execute Tool proposals.

MemoryProvider is an independent authorized gateway in v0.2. Callers explicitly
select a registered Provider and invoke retrieve, remember, forget, or optional
consolidate operations. AgentRuntime does not automatically read or write
Memory.

Run transitions are committed with compare-and-set before their state-change
events are published. Context resolution and Model invocation also emit redacted
observability events. Observability failure does not change Run outcome;
reliable authorization audit failure pauses the Run by default or fails it when
the Run control policy requires failure.

## Current v0.2 Implementation Boundary

The verified direct Agent slice includes:

- Provider-neutral text, JSON, and reference content blocks
- Shared registered schemas for Context and structured Model output
- Deterministic Context selection with single, first-success, merge, and all
  strategies
- Model generation, streaming, capabilities, usage, structured output, and
  restricted fallback
- Active deadline and cancellation propagation to Provider calls
- Exactly one terminal Model stream event and cleanup of closable streams
- Root AgentRun execution with optional Skill/Tool reference preflight, but no
  automatic Skill loading or Tool execution loop
- Authorized Memory capability discovery, pagination, idempotent mutation,
  optional consolidation, cancellation cleanup, and redacted operation events
- Stable v0.2 compatibility fixtures for Content, Context, Model, AgentSpec,
  Memory, Checkpoint, approval, Workflow, Evaluation, Provider actions, and Core
  event catalogs
- Authorized in-memory CheckpointStore, canonical integrity, marker commits,
  migration/fallback policy, minimal restoration, and approval persistence
- Immutable Workflow contracts, strict DAG validation, deterministic
  one-at-a-time scheduling, AgentNode child Runs, authorized durable output
  references, Checkpoint recovery, and ApprovalNode suspension and resumption
- Deterministic schema-policy-quality Evaluation, reliable verdict audit,
  replaceable quality evaluators, and durable EvaluationNode success and failure
  boundaries
- Atomic Plugin publication, lifecycle, execution leases, drain-safe unload, and
  failure recovery
- Immutable Skill/Tool v1 contracts, typed Plugin-registry views, progressive
  Skill resource reads, schema-aware Tool execution, in-lease retry, and
  AgentSpec v1/v2 compatibility
- Accepted transport-neutral MCP Adapter v1 contract for explicit remote Tool
  and exact-resource Context bindings on protocol revision `2026-07-28`

The following remain outside this implemented slice:

- Model-driven Tool execution loops
- Skill, Tool, Context, and custom Workflow node execution
- Parallel scheduling and external Workflow engine adapters
- MCP Adapter implementation and transport contract coverage

These future capability families must reuse the implemented authorization,
RuntimeCallContext, cancellation, error, and event boundaries before their own
delivery tasks can be marked Implemented.

## Skill and Tool in Plain Language

Think of a Skill as a library catalog. Reading the catalog is cheap and has no
loader side effect. Opening one named item requires an authorized, bounded read;
Core holds the owning Plugin lease until the content and its byte count have been
checked. Core then hands the item back to the caller without placing it in an
Agent context automatically.

Think of a Tool as a guarded function owned by a Plugin. Core checks the input
schema before the Plugin can run, verifies the declared Action and effective
Scope, holds one Plugin lease across every allowed retry, checks the output
schema, and releases the lease even on timeout, cancellation, or failure. All
attempts share one operation identity so retry cannot quietly become a second
side effect.

The typed Skill and Tool registries are read-only views of the existing atomic
Plugin registry. They do not introduce another registration transaction, and a
resolved public value never exposes the loader or executor. The detailed review
path and safety checklist are in the
[Skill and Tool v1 Code Review Guide](reviews/task-5.2-skill-tool-code-review.md).

## Implemented Minimal Workflow Runtime

The delivered direct Workflow Runtime is deliberately smaller than a complete
workflow engine. It validates immutable Workflow contracts before execution,
uses a deterministic dependency scheduler, and executes AgentNode through child
AgentRuns. Stable boundaries use CheckpointCoordinator, and recovery finishes
restoring node state before scheduling resumes. Agent output needed after
recovery is converted by an injected persistence boundary into typed durable
references; raw text and JSON bodies remain outside Checkpoints.

### What This Means in Practice

In plain language, Core can now run a small, durable checklist of Agent steps:

1. It checks the whole checklist before doing any work. A broken dependency,
   unsupported step, incompatible schema, or unevaluable permission stops the
   Workflow before a node or Checkpoint is created.
2. It runs one ready step at a time in a predictable order. A step cannot start
   until every declared prerequisite has completed successfully.
3. Each Agent step gets its own child Run, while the parent Workflow keeps the
   same workspace, session, cancellation signal, deadline limit, trace, and
   stable idempotency identity.
4. A completed step is saved only after any output needed later has a durable
   typed reference. Checkpoints contain those references, not the raw response.
5. After a crash, Core reloads the last committed save point. Finished steps are
   skipped, while interrupted work may run again with the same idempotency key.
6. An approval step creates a durable pause. Restarting does not create a second
   request, and downstream work stays locked until an authorized decision is
   durably recorded.

This does not yet make Core a general-purpose workflow engine. It intentionally
does not execute Skill, Tool, Context, or custom nodes, and it does
not provide parallel scheduling, compensation, or an external engine adapter.

### Evaluation in Plain Language

Think of Evaluation as a three-question checkpoint for one value:

1. Does the value match its declared schema?
2. Does an independent content policy allow it?
3. Does one selected, replaceable quality evaluator accept it?

Core asks those questions in that order and stops on the first "no." It never
lets a later evaluator turn an earlier failure into success. The quality profile
and evidence bodies stay outside Core; Core carries only opaque profile names,
safe measurements, and scoped evidence references.

Before any result becomes recoverable, a required audit sink must acknowledge
the verdict. Core then stores the typed `EvaluationResult` and commits a
Checkpoint. A passed node may unlock its dependents. A denied, failed, timed-out,
or cancelled node is also saved, but it terminates the Run and unlocks nothing.
Recovery reads that saved result and does not call the evaluator again.

ApprovalNode composes the durable ApprovalCoordinator. EvaluationNode composes
this fail-fast Evaluation harness. Skill, Tool, Context, custom nodes, parallel
scheduling, compensation, and external engine adapters remain deferred. The
normative delivery sequence remains in [tasks.md](../tasks.md), and reviewers can
use the [Evaluation Pipeline Code Review Guide](reviews/evaluation-pipeline-code-review.md)
for a file-by-file walkthrough and verification checklist.

## Extension Flow

```text
Discover -> Validate -> Load -> Register -> Active
Active -> Draining -> Unregister -> Unloaded
```

Active execution leases protect in-flight work during drain. External engines
and infrastructure remain replaceable through adapters and providers.

## Contract Navigation

| Area | Contract |
| --- | --- |
| Plugin SDK and safe unload | [RFC-0002](rfcs/RFC-0002-plugin-sdk.md) |
| Workflow graph | [RFC-0003](rfcs/RFC-0003-workflow.md) |
| Run and Session lifecycle | [RFC-0004](rfcs/RFC-0004-execution-run-lifecycle.md) |
| ContextProvider | [RFC-0006](rfcs/RFC-0006-context-provider.md) |
| MemoryProvider | [RFC-0007](rfcs/RFC-0007-memory-provider.md) |
| Scope and authorization | [RFC-0008](rfcs/RFC-0008-scope-authorization.md) |
| ModelProvider | [RFC-0009](rfcs/RFC-0009-model-provider.md) |
| Runtime Events | [RFC-0010](rfcs/RFC-0010-runtime-events.md) |
| Checkpoint, commit, and recovery | [RFC-0011](rfcs/RFC-0011-checkpoint-recovery.md) |
| Evaluation pipeline and node boundary | [RFC-0012](rfcs/RFC-0012-evaluation.md) |
| Skill and Tool contracts | [RFC-0013](rfcs/RFC-0013-skill-tool-contracts.md) |

Cross-cutting rationale is indexed in the [ADR Registry](adrs/README.md), and
implementation sequencing is defined in [tasks.md](../tasks.md).
