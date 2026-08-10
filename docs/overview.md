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
use idempotency keys.

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
- Root AgentRun execution without a Plugin, Skill, Tool, MemoryProvider, or
  Workflow

The following remain outside this implemented slice:

- Persistent MemoryProvider behavior
- Model-driven Tool execution loops
- Workflow graph execution and checkpoint recovery
- Approval and evaluation coordination
- Plugin lifecycle and MCP capability adapters

These future capability families must reuse the implemented authorization,
RuntimeCallContext, cancellation, error, and event boundaries before their own
delivery tasks can be marked Implemented.

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
| Checkpoint and recovery | [RFC-0011](rfcs/RFC-0011-checkpoint-recovery.md) |

Cross-cutting rationale is indexed in the [ADR Registry](adrs/README.md), and
implementation sequencing is defined in [tasks.md](../tasks.md).
