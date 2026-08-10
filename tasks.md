# Agent Harness Runtime Core Tasks

Version: 0.1.0

## Phase 1 Core Foundation

### Task 1.1 Project Structure

Create:

- runtime
- workflow
- harness
- plugin
- skill
- tool

Acceptance:

Core can run independently.

### Task 1.2 Agent Runtime

Implement:

- AgentSpec
- AgentBuilder
- Agent lifecycle

Acceptance:

A minimal Agent can execute.

## Phase 2 Workflow Runtime

### Task 2.1 Workflow Engine

Implement:

- Workflow interface
- WorkflowContext
- WorkflowResult

### Task 2.2 Node System

Implement:

- AgentNode
- SkillNode
- ToolNode
- ContextNode

## Phase 3 Harness

### Task 3.1 Execution Harness

Implement:

- lifecycle
- retry
- timeout

### Task 3.2 Context Harness

Implement:

- ContextProvider
- ContextResolver

### Task 3.3 Memory Protocol

Implement:

- MemoryProvider interface
- memory lifecycle hooks

No business memory implementation.

### Task 3.4 Approval Harness

Implement:

- approval request
- approval decision

### Task 3.5 Evaluation Harness

Implement:

- evaluator interface
- validation pipeline

## Phase 4 Extension

### Task 4.1 Plugin SDK

Implement:

- manifest
- discovery
- registration

### Task 4.2 Skill Registry

Implement:

- discovery
- progressive loading

### Task 4.3 MCP Adapter

Implement:

- tool discovery
- schema mapping

## Phase 5 Production

### Task 5.1 Observability

Implement:

- tracing
- runtime events

### Task 5.2 Storage Abstraction

Implement:

- storage interface
- adapters

## Constraints

- No business coupling.
- Plugins contain domain logic.
- Providers are replaceable.
- Avoid mandatory dependency on one Agent framework.
