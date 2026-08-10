# CongeriesCore Agent Guide

Version: 0.2.0

## 1. Purpose

This file is the navigation map and contribution guide for AI coding agents.
It does not redefine the product requirements, architecture, principles, or
delivery plan. Follow the linked specification documents as their source of
truth.

Keep this file concise and below 500 lines. Put detailed product decisions in
the document that owns them, then link to that document from here if additional
navigation is needed.

## 2. Specification Map

| Document | Owns | Read when |
| --- | --- | --- |
| [principles.md](principles.md) | Long-term architectural invariants and design review constraints | Evaluating whether a capability belongs in Core |
| [requirements.md](requirements.md) | Product scope, goals, non-goals, required capabilities, and quality attributes | Adding or changing observable Core behavior |
| [design.md](design.md) | Architecture, component responsibilities, runtime concepts, and integration boundaries | Changing interfaces, data flow, lifecycle, or module relationships |
| [tasks.md](tasks.md) | Delivery phases, implementation work, dependencies, and acceptance criteria | Planning, sequencing, or completing implementation work |
| [agents.md](agents.md) | Repository navigation and contributor workflow | Before making any repository change |

Use the map by ownership:

- Start with `principles.md` to test the architecture boundary.
- Use `requirements.md` to determine what the system must or must not do.
- Use `design.md` to determine how the requirement is represented in the
  architecture.
- Use `tasks.md` to determine when and how the work is delivered.
- Use this file only for the process used to make the change safely.

## 3. Authority and Conflict Resolution

Resolve conflicts by document ownership, not by duplicating or blending text.

1. `principles.md` governs architectural invariants.
2. `requirements.md` governs product scope and required behavior and must
   comply with the principles.
3. `design.md` governs the technical design and must satisfy both the
   principles and requirements.
4. `tasks.md` governs delivery sequencing and must not introduce new product
   requirements or architecture.
5. `agents.md` governs contribution workflow only and must not override any
   specification document.

When documents disagree:

1. Identify the type of decision and its owning document.
2. Treat the higher-level invariant or requirement as authoritative.
3. Update the incorrect downstream document in the same change when possible.
4. Do not preserve contradictory alternatives without explicitly marking an
   unresolved decision.
5. If intent cannot be determined safely, stop and request a design decision.

## 4. Specification Writing Policy

All additions and modifications to the specification and design documents
listed below MUST be written in English:

- `principles.md`
- `requirements.md`
- `design.md`
- `tasks.md`
- `agents.md`

This rule applies to headings, prose, tables, diagram labels, examples,
acceptance criteria, and normative statements. Preserve code identifiers and
external names exactly when required.

Keep each decision in one owning document:

- Put enduring constraints in `principles.md`.
- Put externally meaningful scope and behavior in `requirements.md`.
- Put interfaces, responsibilities, state transitions, and execution semantics
  in `design.md`.
- Put implementation order and testable completion criteria in `tasks.md`.
- Put contributor navigation and workflow rules in `agents.md`.

Prefer relative Markdown links and section references over copied paragraphs.
When a specification change affects multiple ownership levels, update the
affected documents together so they remain consistent.

## 5. Architectural Guardrails

These are navigation-level guardrails. The complete rationale and definitions
remain in `principles.md`, `requirements.md`, and `design.md`.

### 5.1 Keep Core business-independent

Core may understand runtime concepts such as Agent, Workflow, Skill, Tool,
Context, Workspace, Artifact, and Policy. It must not contain business entity
models, user models, industry concepts, application workflows, or
domain-specific memory schemas.

Never add business-specific directories or branches such as:

```text
core/persona/
core/customer/
core/project/
core/business/
if business_type == ...
```

### 5.2 Prefer extension points

Before changing Core, determine whether the capability belongs in a:

- Plugin
- Provider
- Adapter
- Workflow
- Skill

Add a Core capability only when it is universally required by multiple use
cases and cannot be expressed safely through an existing extension point.

### 5.3 Depend on interfaces

External capabilities must be reached through explicit interfaces and
dependency injection. Provider and adapter implementations must remain
replaceable. Do not couple Core directly to an LLM vendor, database, vector
store, workflow framework, or application service.

### 5.4 Separate runtime from orchestration

- Agent Runtime owns composition, configuration, lifecycle, execution, and
  runtime state.
- Workflow Runtime executes workflow definitions and node graphs.
- Plugins define business workflows and domain behavior.
- Skills provide reusable capabilities; they do not own business lifecycle or
  global application state.
- Tools expose bounded external actions with explicit schemas, permissions, and
  execution policies.

### 5.5 Keep context and state explicit

Context must enter through explicit providers and resolvers. Avoid hidden global
state, implicit database access, uncontrolled context injection, and magic
runtime behavior.

## 6. Change Workflow

For every change:

1. Classify it as a principle, requirement, design, task, implementation, or
   documentation change.
2. Read the owning specification and any higher-level documents it depends on.
3. Confirm that the capability belongs in Core rather than an extension.
4. Identify the interface, provider, adapter, plugin, workflow, or skill boundary.
5. Update affected specifications first when behavior or architecture changes.
6. Implement the smallest coherent change with explicit interfaces and
   dependency injection.
7. Add tests proportional to the affected contract and failure modes.
8. Update acceptance criteria and delivery status in `tasks.md` when applicable.
9. Verify that documentation, implementation, and tests describe the same
   behavior.

Do not modify a specification merely to justify an implementation that violates
an existing principle. Escalate the architectural decision instead.

## 7. Specification Change Checklist

Before completing a specification or design change, verify:

- The owning document contains the decision and other documents link or defer
  to it.
- The text is written in English.
- Core and plugin responsibilities are explicit.
- Public inputs, outputs, errors, lifecycle, and state transitions are defined
  where relevant.
- Provider and adapter boundaries are replaceable.
- Permission and execution boundaries are defined for tools and integrations.
- Recovery, timeout, retry, cancellation, and observability implications are
  considered.
- Compatibility impact and migration requirements are documented.
- `tasks.md` contains testable acceptance criteria for implementation work.

## 8. Testing Expectations

New or changed Core behavior requires appropriate coverage:

- Unit tests for interfaces, lifecycle, state transitions, validation, and
  runtime behavior.
- Integration tests for workflow execution, plugin loading, provider
  interaction, and adapter boundaries.
- Compatibility tests when public APIs, manifests, schemas, or plugin contracts
  change.

Tests must cover failure paths relevant to the change, including timeout,
retry, cancellation, recovery, invalid schemas, denied permissions, and provider
failures when applicable.

## 9. Implementation Style

Prefer:

- Explicit, small interfaces
- Dependency injection
- Small modules with clear ownership
- Clear naming and typed schemas
- Observable lifecycle transitions
- Minimal mandatory dependencies

Avoid:

- Hidden state and implicit dependencies
- Unbounded tool access
- Domain-specific conditionals in Core
- Framework-specific contracts exposed as Core APIs
- Excessive abstraction without multiple concrete use cases
- Breaking changes without migration guidance

## 10. Completion Standard

A change is complete only when:

- It respects the architecture boundary.
- The owning specifications are consistent and written in English.
- Implementation and tests satisfy the documented contract.
- Existing plugin and provider compatibility is preserved or migration is
  documented.
- Relevant acceptance criteria are verifiable.

The guiding principle remains: Core becomes more powerful by becoming more
abstract, while application-specific capability remains removable and external.
