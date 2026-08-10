# Agent Harness Runtime Core Principles

Version: 0.1.0

## 1. Purpose

This document defines the long-term architectural principles of Agent
Harness Runtime Core.

These principles are mandatory constraints for all future design
decisions.

The goal is to keep the Core lightweight, extensible and independent
from business domains.

------------------------------------------------------------------------

# 2. Core Philosophy

## Principle 1: Core is Runtime, Not Application

The Core provides execution capability.

The Core does not provide business intelligence.

The Core understands:

-   Agent
-   Workflow
-   Skill
-   Tool
-   Context
-   Workspace
-   Artifact
-   Policy

The Core does not understand:

-   Business entities
-   User concepts
-   Industry concepts
-   Application-specific workflows

------------------------------------------------------------------------

# 3. Extension Over Modification

New capabilities SHOULD be implemented through extension mechanisms.

Preferred:

    Plugin
    Provider
    Adapter
    Skill
    Workflow

Avoid:

    Modify Core
    Add business condition
    Add domain-specific branch

A mature plugin should be removable without changing Core behavior.

------------------------------------------------------------------------

# 4. Interface First

Core modules SHALL expose stable interfaces.

Implementation details SHOULD remain replaceable.

Example:

Preferred:

    MemoryProvider

          |

    Implementation

Avoid:

    Core directly depends on PostgreSQL Memory

------------------------------------------------------------------------

# 5. Workflow Is Business Orchestration

Workflow represents:

-   execution order
-   dependency
-   control flow
-   business process

Workflow is not:

-   prompt collection
-   autonomous planning replacement
-   hardcoded domain logic inside Core

Core executes workflows.

Plugins define workflows.

------------------------------------------------------------------------

# 6. Skill Is Capability

Skill represents reusable capability.

Skill SHOULD contain:

-   instructions
-   resources
-   examples
-   scripts
-   references

Skill SHOULD NOT own:

-   business lifecycle
-   global state
-   user data

------------------------------------------------------------------------

# 7. Context Is Explicit

Agent behavior depends on context.

Context MUST be provided through explicit providers.

Avoid:

-   hidden global state
-   implicit database access
-   uncontrolled context injection

Preferred:

    Agent

    ^

    Context Resolver

    ^

    Context Provider

------------------------------------------------------------------------

# 8. Memory Is Pluggable

Memory is a capability, not a Core implementation.

Core provides:

    Memory Protocol

Plugins provide:

    Memory Implementation

Different applications MAY use different memory strategies.

------------------------------------------------------------------------

# 9. Reliable Execution Over Autonomous Complexity

The Core prioritizes:

-   predictable execution
-   observability
-   recovery
-   control

over:

-   uncontrolled autonomous planning
-   opaque agent behavior

------------------------------------------------------------------------

# 10. Minimal Dependencies

Core SHOULD avoid mandatory dependency on:

-   specific LLM provider
-   specific vector database
-   specific workflow framework
-   specific storage engine

External frameworks MAY be integrated through adapters.

------------------------------------------------------------------------

# 11. Backward Compatibility

Core APIs should evolve carefully.

Breaking changes require:

-   migration plan
-   compatibility consideration
-   documented rationale

------------------------------------------------------------------------

# 12. Design Review Checklist

Before adding a feature, ask:

1.  Is this a runtime capability or business capability?
2.  Can this be implemented as a plugin?
3.  Does this introduce domain coupling?
4.  Does this require a new interface?
5.  Does this increase mandatory dependencies?

If a feature belongs to business logic, it should not enter Core.
