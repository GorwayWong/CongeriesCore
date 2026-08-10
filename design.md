# Agent Harness Runtime Core Design

Version: 0.1.0

## Architecture

```
Application Plugins

        |

Agent Core Runtime

        |

+----------------+
| Harness Layer  |
+----------------+

+----------------+
| Runtime Layer  |
+----------------+

+----------------+
| State Layer    |
+----------------+

+----------------+
| Integration    |
+----------------+
```

## Runtime Layer

### Agent Runtime

Agent is runtime composition:

```
Agent =
Identity
+
Instructions
+
Skills
+
Tools
+
Context
+
Policy
+
Model
```

### Workflow Runtime

Workflow is executable process:

```
Workflow =
Definition
+
Node Graph
+
Execution Policy
```

Node types:

- Agent Node
- Skill Node
- Tool Node
- Context Node
- Approval Node
- Evaluation Node

## Harness Layer

### Execution Harness

Provides:

- run
- pause
- resume
- cancel
- retry

### Context Harness

Context is injected through providers.

```
Agent

^

Context Resolver

^

Context Providers
```

### Memory Harness

Core defines:

```
MemoryProvider
```

Example methods:

```
retrieve()
store()
forget()
consolidate()
```

Memory implementations belong to plugins.

### Approval Harness

Provides human-in-the-loop execution.

### Evaluation Harness

Provides:

- schema validation
- policy checking
- quality evaluation

## Workspace

Workspace stores execution state.

```
Workspace

Goal
Plan
Tasks
Artifacts
Decisions
State
```

Supports:

- snapshot
- incremental update

## Event Runtime

Core only provides runtime events:

- AgentStarted
- WorkflowStarted
- ToolCalled
- ArtifactCreated

Business events belong to plugins.

## Plugin System

```
plugin/

manifest.yaml

workflows/

skills/

tools/

providers/
```

Lifecycle:

discover -> validate -> load -> register

## MCP

MCP is a capability gateway.

Supports:

- Tool capability
- Context capability

It is not a CRUD gateway.

## Storage

Storage is abstracted:

```
Storage Provider

        |

Adapter

        |

Database
```

Core owns interfaces, not databases.
