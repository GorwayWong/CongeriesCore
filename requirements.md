# Agent Harness Runtime Core Requirements

Version: 0.1.0

## Purpose

Build a lightweight, extensible and business-independent Agent Runtime Core.

The Core provides runtime capabilities only. Business capabilities are implemented by plugins.

## Goals

- Enterprise backend Agent runtime support
- Personal Agent support
- Workflow based execution
- Plugin based extension
- Provider independent architecture

## Non Goals

The Core does not include:

- Business domain models
- User models
- Specific memory implementation
- Database coupling
- LLM provider coupling

## Principles

### Runtime First

Core understands:

- Agent
- Workflow
- Skill
- Tool
- Context
- Workspace
- Artifact
- Policy

Core does not understand business concepts.

### Plugin First

Business capability MUST be provided by plugins.

Plugins MAY provide:

- Workflow
- Skill
- Tool
- Context Provider
- Memory Provider
- MCP Adapter

### Workflow Driven

Workflow represents deterministic execution.

Workflow defines:

- input schema
- nodes
- dependencies
- output schema
- execution policy

### Capability Separation

Skill defines capability.

Tool defines external action.

Workflow composes capabilities.

Agent combines:

Identity + Context + Skills + Tools + Policy.

## Harness Requirements

Core provides:

### Execution Harness

- lifecycle
- retry
- timeout
- recovery

### Context Harness

- context resolution
- context injection
- provider management

### Memory Harness

Only provides Memory Protocol.

No concrete memory implementation.

### Approval Harness

Supports human approval workflows.

### Evaluation Harness

Supports output validation and quality checks.

## Extension Requirements

Core supports:

- Plugin discovery
- Skill registry
- MCP integration
- Storage abstraction

## Quality Requirements

The Core SHALL be:

- lightweight
- modular
- testable
- observable
- extensible
- business independent
