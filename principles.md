# Agent Harness Runtime Core Principles

Version: 0.2.0

## 1. Purpose

This document defines the mandatory long-term architectural invariants of
CongeriesCore. Requirements, ADRs, designs, RFCs, implementations, and plugins
must comply with these principles.

The goal is a lightweight, reliable, extensible runtime that remains independent
from business domains and replaceable infrastructure.

## 2. Core Is Runtime, Not Application

Core provides execution capability and runtime control. It does not provide
business intelligence.

Core may understand runtime concepts:

- Agent and Workflow
- Run and SessionRef
- Skill and Tool
- Context and Scope
- Workspace and Artifact
- Policy and Provider
- Runtime Event and Checkpoint

Core does not understand:

- Business entities or industry concepts
- User, customer, or organization models
- Application-specific workflows
- Domain memory schemas
- Application API or presentation models

Application meaning stays in plugins and application services.

## 3. Extension Over Modification

New capability should be implemented through a Plugin, Provider, Adapter,
Workflow, or Skill before Core is modified.

A capability belongs in Core only when it is universally required to execute or
control multiple kinds of applications. A mature extension must be removable
without changing unrelated Core behavior.

Core must not contain domain branches or application-specific conditions.

## 4. Interface First

Core modules expose stable, typed interfaces. Implementations remain replaceable
through dependency injection.

External systems are accessed through providers or adapters. Core does not
directly depend on a specific model vendor, database, vector store, workflow
engine, transport, or application framework.

Framework-native types must not leak into public Core contracts.

## 5. Execution Identity Is Explicit

Every execution has an explicit Run identity. AgentRun and WorkflowRun are
peer execution types and may both be root runs. Parent and root relationships
make nested execution observable without coupling Agent to Workflow.

Workspace owns durable execution state and artifacts. SessionRef groups related
runs for correlation and isolation only. Core Session state does not own message
history, participants, user data, or business state.

## 6. Workflow Is Plugin-Defined Orchestration

Workflow represents execution order, dependency, control flow, and execution
policy. Core defines and executes the workflow contract; plugins define concrete
workflow content and its business meaning.

Workflow is not a prompt collection, an autonomous planning replacement, or a
place for hardcoded domain logic inside Core.

The workflow engine remains replaceable through adapters.

## 7. Capabilities Have Separate Responsibilities

- Agent composes identity, instructions, capabilities, context, policy, and a
  model binding.
- Skill packages reusable instructions, resources, examples, scripts, and
  references without owning global state or business lifecycle.
- Tool performs a bounded external action with explicit schemas, authorization,
  and execution policy.
- Workflow composes nodes into an executable graph.
- Provider supplies replaceable external capability.
- Plugin packages application or integration capability.

## 8. Context and Authority Are Explicit

Context enters execution only through explicit providers and resolvers.
External calls receive an explicit runtime call context.

Authorization is scope-based and deny-by-default. Core scope types describe
runtime boundaries only. Application scopes such as users or organizations are
plugin-defined values, not Core business concepts.

Hidden global state, implicit database access, uncontrolled context injection,
and default-allow access are prohibited.

## 9. Memory and Models Are Pluggable

Core defines MemoryProvider and ModelProvider contracts, not concrete memory or
model implementations.

Memory is persistent knowledge available across runs; it is not conversation
history storage. ModelProvider is vendor-neutral and does not expose vendor SDK
types through Core APIs.

Applications select memory semantics, storage engines, model vendors, and model
policies through plugins and providers.

## 10. Reliable Execution Over Autonomous Complexity

Core prioritizes predictable execution, control, observability, approval,
recovery, and cancellation over opaque autonomous behavior.

Workflow recovery uses atomic checkpoints at stable boundaries and provides
at-least-once execution. A recovered node may be replayed. Side-effecting Tool
and Provider operations therefore require idempotency keys.

Terminal Run states are irreversible. Retry and recovery occur as explicit new
attempts, not by mutating a completed outcome.

## 11. Events Observe State; They Do Not Own It

Runtime Events provide observability and audit signals. They are not the sole
source of runtime truth and do not imply Event Sourcing.

Ordinary observability delivery is non-blocking. Security, authorization, and
approval audit events use reliable delivery according to execution policy.

Business events remain outside Core.

## 12. Lifecycle Boundaries Are Safe

Lifecycle operations are explicit, observable, and idempotent where repeated
requests are possible.

Plugin unload drains active work before unregistering capability or releasing
resources. Core must not dispose a plugin while it owns active execution leases.

Cancellation, timeout, approval waits, retry, recovery, and drain timeout have
defined outcomes rather than hidden behavior.

## 13. Minimal Dependencies

Core avoids mandatory dependencies on specific LLM providers, vector databases,
workflow frameworks, storage engines, application frameworks, and event brokers.

External frameworks may be integrated through adapters. A default implementation
may be provided, but the public contract must remain independent.

## 14. Compatibility

Public Core APIs, provider contracts, plugin manifests, event schemas, and
checkpoint formats evolve deliberately.

After the v0.2 pre-implementation baseline, breaking changes require:

- A documented rationale
- Compatibility impact analysis
- A migration plan
- A version change appropriate to the affected contract

Superseded RFCs and ADRs remain discoverable through migration links.

## 15. Design Review Checklist

Before adding or changing a capability, ask:

1. Is it runtime capability or application capability?
2. Can it be implemented as an extension?
3. Does it introduce a business concept into Core?
4. Is execution identity and ownership explicit?
5. Does it cross a Scope or permission boundary?
6. Are retry, cancellation, recovery, and idempotency defined?
7. Does it require a stable interface or an ADR?
8. Does it increase mandatory dependencies?
9. Are compatibility and migration consequences documented?

If a capability belongs to application logic, it must not enter Core.
