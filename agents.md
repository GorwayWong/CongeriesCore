# CongeriesCore Agent Guide

Version: 0.2.0

## 1. Purpose

This file is the navigation map and contribution guide for AI coding agents.
It does not redefine product requirements, architecture, public contracts, or
delivery plans. Follow the linked document that owns each decision.

Keep this file below 500 lines. Put detailed decisions in their owning document
and link to them instead of copying them here.

## 2. Documentation Map

| Document | Authority and ownership | Read when |
| --- | --- | --- |
| [principles.md](principles.md) | Long-term architectural invariants | Deciding whether a capability belongs in Core |
| [requirements.md](requirements.md) | Product scope, required behavior, and quality attributes | Changing observable Core behavior |
| [docs/adrs/README.md](docs/adrs/README.md) | Accepted cross-cutting architectural decisions | Understanding why a durable decision was made |
| [design.md](design.md) | Current system architecture and composition of public contracts | Changing responsibilities, data flow, or module relationships |
| [docs/rfcs/README.md](docs/rfcs/README.md) | Detailed public contracts; Accepted and Implemented RFCs are normative within their scope | Changing an interface, lifecycle, schema, or execution semantic |
| [tasks.md](tasks.md) | Delivery phases and testable acceptance criteria | Planning or completing implementation work |
| [docs/overview.md](docs/overview.md) | Non-normative orientation and navigation | Learning the system for the first time |
| [docs/README.md](docs/README.md) | Document registry and governance | Adding, moving, or superseding documents |
| [agents.md](agents.md) | Repository navigation and contributor workflow | Before making any repository change |

Use the map by ownership:

1. Test the change against `principles.md`.
2. Confirm required behavior in `requirements.md`.
3. Check accepted ADRs for cross-cutting decisions.
4. Use `design.md` for the current architecture.
5. Read the owning Accepted or Implemented RFC for detailed contract semantics.
6. Use `tasks.md` for delivery order and acceptance criteria.

## 3. Authority and Conflict Resolution

Documents have authority by subject, not simply by publication date.

- Principles override every downstream document.
- Requirements must comply with principles and own product behavior.
- Accepted ADRs own cross-cutting decisions but cannot weaken requirements.
- Design must reflect principles, requirements, and accepted ADRs.
- An Accepted or Implemented RFC owns the detailed contract in its declared
  scope and must be synchronized with design.
- Tasks cannot introduce new requirements or architecture.
- Overview and migration pages are non-normative.
- This guide governs contribution workflow only.

When documents disagree:

1. Identify the decision type and owning document.
2. Treat the higher-level invariant or requirement as authoritative.
3. For equal-level conflicts, prefer an Accepted document over Draft or
   Superseded material.
4. Update every affected downstream document in the same change.
5. Do not preserve contradictory alternatives unless they are explicitly
   recorded as an unresolved Draft RFC question.

## 4. Specification Writing Policy

All additions and modifications to specifications, designs, RFCs, ADRs,
registries, migration pages, and this guide MUST be written in English. This
includes headings, prose, tables, diagram labels, examples, acceptance criteria,
and normative statements. Preserve external names and code identifiers exactly.

Keep each decision in one owning document:

- Enduring constraints belong in `principles.md`.
- Externally meaningful behavior belongs in `requirements.md`.
- Cross-cutting decision rationale belongs in one ADR.
- Current architecture and relationships belong in `design.md`.
- Detailed interfaces and execution semantics belong in one RFC.
- Delivery order and verifiable completion criteria belong in `tasks.md`.
- Orientation belongs in `docs/overview.md`.

Use relative Markdown links instead of copied normative paragraphs. Follow the
metadata and status rules in [docs/README.md](docs/README.md).

## 5. Architectural Guardrails

These are navigation-level guardrails. The owning definitions remain in the
principles, requirements, design, ADRs, and RFCs.

### 5.1 Keep Core business-independent

Core may understand runtime concepts such as Agent, Workflow, Run, Skill, Tool,
Context, Scope, Workspace, SessionRef, Artifact, Policy, and Provider. It must
not contain business entities, user models, industries, application workflows,
or domain-specific memory schemas.

Never add business-specific directories or branches such as:

```text
core/persona/
core/customer/
core/project/
core/business/
if business_type == ...
```

### 5.2 Prefer extension points

Before changing Core, determine whether the capability belongs in a Plugin,
Provider, Adapter, Workflow, or Skill. Add Core capability only when it is
universally required and cannot be expressed safely through an extension.

### 5.3 Depend on explicit interfaces

External capability must be reached through explicit interfaces and dependency
injection. Do not couple Core to an LLM vendor, database, vector store, workflow
framework, transport, or application service.

### 5.4 Keep authority and state explicit

Context, memory, models, tools, and MCP calls must receive a runtime call
context and pass authorization. Avoid hidden global state, implicit database
access, uncontrolled context injection, and default-allow behavior.

### 5.5 Preserve reliable execution

Run transitions, checkpoint boundaries, cancellation, retry, recovery, approval,
and audit delivery must be observable and testable. Side-effecting operations
must follow the idempotency contract defined by the owning RFC.

## 6. Change Workflow

For every change:

1. Classify it as a principle, requirement, ADR, design, RFC, task,
   implementation, or documentation change.
2. Read the owning document and all higher-level documents it depends on.
3. Confirm that the capability belongs in Core rather than an extension.
4. Identify the affected interface, provider, adapter, plugin, workflow, or
   state boundary.
5. Update affected specifications before implementation when behavior or
   architecture changes.
6. Implement the smallest coherent change with explicit interfaces.
7. Add tests for the contract, lifecycle, permissions, and failure modes.
8. Update `tasks.md` acceptance criteria and status when applicable.
9. Verify that documentation, implementation, and tests agree.

Do not change a specification merely to justify an implementation that violates
an existing principle. Escalate the architectural decision instead.

## 7. Specification Change Checklist

Before completing a specification or design change, verify:

- The decision exists in exactly one owning document.
- Required metadata, status, version, and cross-links are present.
- The text is written in English.
- Core and extension responsibilities are explicit.
- Public inputs, outputs, errors, lifecycle, and state transitions are defined.
- Provider and adapter boundaries remain replaceable.
- Scope, authorization, audit, and redaction implications are defined.
- Retry, timeout, cancellation, recovery, and idempotency are considered.
- Compatibility or migration impact is documented.
- `tasks.md` contains testable implementation acceptance criteria.

## 8. Testing Expectations

New or changed Core behavior requires appropriate coverage:

- Unit tests for interfaces, lifecycle transitions, validation, authorization,
  idempotency, and runtime behavior.
- Integration tests for workflow execution, checkpoints, plugin lifecycle,
  provider interaction, event sinks, and adapter boundaries.
- Compatibility tests when public APIs, manifests, schemas, or plugin contracts
  change.

Tests must cover relevant failure paths, including timeout, retry, cancellation,
recovery, invalid schema, denied permission, provider failure, audit sink
failure, graph version mismatch, and drain timeout.

## 9. Implementation Style

Prefer explicit small interfaces, dependency injection, typed schemas, clear
ownership, observable lifecycle transitions, and minimal mandatory dependencies.

Avoid hidden state, unbounded tool access, domain conditionals in Core,
framework-specific public contracts, speculative abstraction, and breaking
changes without migration guidance.

## 10. Completion Standard

A change is complete only when:

- It respects the architecture boundary.
- The owning documents are synchronized and written in English.
- Implementation and tests satisfy the documented contract.
- Compatibility is preserved or migration is documented.
- Acceptance criteria are verifiable.

Core becomes more powerful by becoming more abstract, while application-specific
capability remains removable and external.
