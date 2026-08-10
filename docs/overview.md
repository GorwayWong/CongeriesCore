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

```text
Create root Run
    -> authorize and load Context
    -> execute Agent or Workflow
    -> invoke scoped capabilities
    -> checkpoint stable boundaries
    -> pause for approval when requested
    -> evaluate output
    -> record terminal state
```

Every transition emits runtime events. Observability events do not block normal
execution. Security, authorization, and approval audit events use reliable
delivery according to execution policy.

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

