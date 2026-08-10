# Agent Harness Runtime Core Development Guidelines

Version: 0.1.0

## Purpose

This document defines engineering rules for AI coding agents
contributing to this repository.

The goal is to ensure generated code preserves the Core architecture.

------------------------------------------------------------------------

# 1. General Rules

## Rule 1: Preserve Business Independence

Never introduce:

-   business entity models
-   user models
-   application-specific workflows
-   domain-specific memory schemas

The Core must remain reusable.

------------------------------------------------------------------------

## Rule 2: Prefer Extension Points

Before modifying Core:

Check whether the requirement can be implemented as:

-   Plugin
-   Provider
-   Adapter
-   Workflow
-   Skill

Only add Core capability when it is universally required.

------------------------------------------------------------------------

# 2. Architecture Rules

## Agent Runtime

Agent runtime code MUST only handle:

-   lifecycle
-   execution
-   configuration
-   runtime state

It MUST NOT contain:

-   domain decisions
-   business rules

------------------------------------------------------------------------

## Workflow Runtime

Workflow engine MUST execute workflows.

Workflow engine MUST NOT contain:

-   predefined business workflows
-   domain-specific branching

Business workflows belong to plugins.

------------------------------------------------------------------------

## Skill System

Skills should be:

-   discoverable
-   composable
-   independently versioned

Avoid embedding large business logic into Skill runtime.

------------------------------------------------------------------------

## Tool System

Tools MUST define:

-   input schema
-   output schema
-   permission boundary
-   execution policy

Never expose unrestricted internal access.

------------------------------------------------------------------------

# 3. Provider Rules

All external capabilities should use providers.

Examples:

    MemoryProvider

    ContextProvider

    StorageProvider

    ModelProvider

Do not directly couple implementations.

------------------------------------------------------------------------

# 4. Dependency Rules

Avoid adding dependencies unless:

1.  The capability is fundamental.
2.  Multiple use cases require it.
3.  An adapter cannot solve the problem.

Prefer lightweight libraries.

------------------------------------------------------------------------

# 5. Testing Requirements

New Core features require:

## Unit Tests

For:

-   interfaces
-   runtime behavior
-   lifecycle

## Integration Tests

For:

-   plugin loading
-   workflow execution
-   provider interaction

## Compatibility Tests

Ensure existing plugins continue working.

------------------------------------------------------------------------

# 6. Coding Style

Prefer:

-   explicit interfaces
-   dependency injection
-   small modules
-   clear naming

Avoid:

-   magic behavior
-   hidden state
-   excessive abstraction
-   premature optimization

------------------------------------------------------------------------

# 7. AI Agent Workflow

When implementing a task:

1.  Read requirements.md
2.  Read design.md
3.  Read principles.md
4.  Confirm architecture boundary
5.  Implement minimal change
6.  Add tests
7.  Update documentation

------------------------------------------------------------------------

# 8. Forbidden Patterns

Do not create:

    core/persona/

    core/customer/

    core/project/

    core/business/

Do not create:

    if business_type == xxx:

Do not hardcode:

-   users
-   products
-   industries
-   application workflows

------------------------------------------------------------------------

# 9. Preferred Patterns

Use:

    Interface
        |
    Provider
        |
    Plugin

Use:

    Core Runtime

    +
    Extension Layer

    +
    Application Plugin

------------------------------------------------------------------------

# 10. Final Principle

The Core should become more powerful by becoming more abstract.

A feature that only helps one application usually belongs outside Core.
