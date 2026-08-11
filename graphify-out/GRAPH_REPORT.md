# Graph Report - .  (2026-08-10)

## Corpus Check
- 105 files · ~50,795 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1015 nodes · 5899 edges · 44 communities (30 shown, 14 thin omitted)
- Extraction: 55% EXTRACTED · 45% INFERRED · 0% AMBIGUOUS · INFERRED: 2651 edges (avg confidence: 0.54)
- Token cost: 19,000 input · 4,200 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Core Errors and Resources|Core Errors and Resources]]
- [[_COMMUNITY_Authorization and Context|Authorization and Context]]
- [[_COMMUNITY_Memory Provider Runtime|Memory Provider Runtime]]
- [[_COMMUNITY_Runtime Call Control|Runtime Call Control]]
- [[_COMMUNITY_JSON Memory Validation|JSON Memory Validation]]
- [[_COMMUNITY_Identifiers and Scope|Identifiers and Scope]]
- [[_COMMUNITY_Observation Event Delivery|Observation Event Delivery]]
- [[_COMMUNITY_Run State Persistence|Run State Persistence]]
- [[_COMMUNITY_Memory Provider Tests|Memory Provider Tests]]
- [[_COMMUNITY_Agent Runtime Models|Agent Runtime Models]]
- [[_COMMUNITY_Event Dispatch Authorization|Event Dispatch Authorization]]
- [[_COMMUNITY_Session State Persistence|Session State Persistence]]
- [[_COMMUNITY_SQLite Event Ledger|SQLite Event Ledger]]
- [[_COMMUNITY_Run State Machine|Run State Machine]]
- [[_COMMUNITY_Audit Delivery Models|Audit Delivery Models]]
- [[_COMMUNITY_Run State Tests|Run State Tests]]
- [[_COMMUNITY_Event Sink Tests|Event Sink Tests]]
- [[_COMMUNITY_Documentation Migration Pages|Documentation Migration Pages]]
- [[_COMMUNITY_Agent Audit Integration|Agent Audit Integration]]
- [[_COMMUNITY_Immutable Model Validation|Immutable Model Validation]]
- [[_COMMUNITY_Shared Runtime Utilities|Shared Runtime Utilities]]
- [[_COMMUNITY_Normative RFC Contracts|Normative RFC Contracts]]
- [[_COMMUNITY_Event Model Tests|Event Model Tests]]
- [[_COMMUNITY_JSON Codec|JSON Codec]]
- [[_COMMUNITY_Checkpoint Recovery Contracts|Checkpoint Recovery Contracts]]
- [[_COMMUNITY_Architecture Decision Documents|Architecture Decision Documents]]
- [[_COMMUNITY_Event Schema Registry|Event Schema Registry]]
- [[_COMMUNITY_Requirements and Overview|Requirements and Overview]]
- [[_COMMUNITY_ADR Registry|ADR Registry]]
- [[_COMMUNITY_Checkpoint Namespace|Checkpoint Namespace]]
- [[_COMMUNITY_CI Validation|CI Validation]]
- [[_COMMUNITY_Package Root|Package Root]]
- [[_COMMUNITY_Plugin Namespace|Plugin Namespace]]
- [[_COMMUNITY_Skill Namespace|Skill Namespace]]
- [[_COMMUNITY_Test Package|Test Package]]
- [[_COMMUNITY_Tool Namespace|Tool Namespace]]
- [[_COMMUNITY_Workflow Namespace|Workflow Namespace]]
- [[_COMMUNITY_Architecture Session Memory|Architecture Session Memory]]
- [[_COMMUNITY_Package Metadata|Package Metadata]]
- [[_COMMUNITY_RFC Registry|RFC Registry]]
- [[_COMMUNITY_Serena Project Configuration|Serena Project Configuration]]
- [[_COMMUNITY_Serena Local Overrides|Serena Local Overrides]]

## God Nodes (most connected - your core abstractions)
1. `ErrorCategory` - 149 edges
2. `ProviderId` - 129 edges
3. `AccessRequest` - 121 edges
4. `ErrorDetail` - 120 edges
5. `CoreError` - 108 edges
6. `Clock` - 107 edges
7. `RunId` - 104 edges
8. `ResourceRef` - 102 edges
9. `ActionRef` - 101 edges
10. `ScopeRef` - 101 edges

## Surprising Connections (you probably didn't know these)
- `test_deadline_constructor_rejects_naive_datetime()` --calls--> `Deadline`  [INFERRED]
  tests/test_runtime_primitives.py → src/congeries_core/runtime/control.py
- `test_identifier_rejects_invalid_values()` --calls--> `Identifier`  [INFERRED]
  tests/test_runtime_primitives.py → src/congeries_core/runtime/ids.py
- `test_sqlite_identity_and_acknowledgement_conflicts()` --calls--> `SqliteEventLedger`  [INFERRED]
  tests/test_sqlite_event.py → src/congeries_core/adapter/sqlite_event.py
- `test_sqlite_sequence_and_pending_survive_restart()` --calls--> `SqliteEventLedger`  [INFERRED]
  tests/test_sqlite_event.py → src/congeries_core/adapter/sqlite_event.py
- `test_audit_delivery_failure_pauses_the_protected_run()` --calls--> `RuntimeEventPublisher`  [INFERRED]
  tests/integration/test_runtime_loop.py → src/congeries_core/event/integration.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Core Architecture Governance** — congeriescore_agents, congeriescore_design, docs_readme_documentation_registry, adrs_adr_0005_interface_first [INFERRED 0.85]
- **v0.2 Authorized Provider Boundaries** — rfcs_rfc_0006_context_provider, rfcs_rfc_0007_memory_provider, rfcs_rfc_0009_model_provider, rfcs_rfc_0008_scope_authorization [EXTRACTED 1.00]
- **Reliable Execution Lifecycle** — rfcs_rfc_0003_workflow, rfcs_rfc_0004_execution_run_lifecycle, rfcs_rfc_0011_checkpoint_recovery, rfcs_rfc_0010_runtime_events [INFERRED 0.85]

## Communities (44 total, 14 thin omitted)

### Community 0 - "Core Errors and Resources"
Cohesion: 0.08
Nodes (93): AcceptValidator, RunAuditFailureHandler, RuntimeFixture, TransitionRecorder, ActionRef, AuthorizedCall, AuthorizedDispatcher, CorePrincipalKind (+85 more)

### Community 1 - "Authorization and Context"
Cohesion: 0.05
Nodes (77): AuthorizedOperation, Collection, runtime_fixture(), test_agent_cancellation_and_completion_race_have_one_terminal_state(), test_agent_deadline_maps_to_failed_without_provider_invocation(), test_agent_denial_preserves_failed_run(), test_agent_rejects_partial_context_and_provider_failure(), test_agent_uses_fallback_only_for_unsupported_capability() (+69 more)

### Community 2 - "Memory Provider Runtime"
Cohesion: 0.06
Nodes (12): JsonValue, _json_string_list(), MemoryCapabilities, MemoryGateway, _resource_name(), _schema_data(), core_error(), ProviderId (+4 more)

### Community 3 - "Runtime Call Control"
Cohesion: 0.06
Nodes (28): Exception, Future, await_provider(), _cancel_and_wait(), _ignore_cancellation(), Deadline and cancellation enforcement around provider awaits., ResultT, Runtime call context propagated to every external capability. (+20 more)

### Community 4 - "JSON Memory Validation"
Cohesion: 0.11
Nodes (13): _json_mapping(), as_array(), as_int(), as_json_value(), as_object(), _object(), _serialized(), test_content_context_model_and_agent_spec_v02_fixtures() (+5 more)

### Community 5 - "Identifiers and Scope"
Cohesion: 0.15
Nodes (31): AgentId, CheckpointRef, DefinitionId, Identifier, ModelBindingRef, Validated opaque identifiers used by public runtime contracts., Opaque, serializable identity with conservative boundary validation., Agent or Workflow definition identity. (+23 more)

### Community 6 - "Observation Event Delivery"
Cohesion: 0.15
Nodes (23): EventDeliveryPolicy, EventDiagnostic, _QueuedObservation, Runtime Event creation, routing, reliable audit, and best-effort observation., Versioned Runtime Events for observability and reliable audit., Runtime and authorization Event publishers backed by EventDispatcher., RuntimeEventPublisher, ClassifiedPayload (+15 more)

### Community 7 - "Run State Persistence"
Cohesion: 0.13
Nodes (11): Mutation, RunAuditFailureHandler, RunId, Run, InMemoryRunRepository, Replaceable state repositories and in-memory reference implementations., RunRepository, NullRunEventPublisher (+3 more)

### Community 8 - "Memory Provider Tests"
Cohesion: 0.27
Nodes (29): AuditRecorder, call_context(), root_scope(), ActionConstraintPolicy, fake_provider(), FakeMemoryProvider, gateway(), item() (+21 more)

### Community 9 - "Agent Runtime Models"
Cohesion: 0.12
Nodes (9): AgentExecutionResult, AgentRegistry, AgentRuntime, AgentSpec, AgentSpec composition and the minimal direct Agent runtime., Execution, context, approval, and evaluation harness namespace., test_agent_registry_and_execution_value_validation(), ErrorDetail (+1 more)

### Community 10 - "Event Dispatch Authorization"
Cohesion: 0.15
Nodes (4): EventDispatcher, Routes events without making the event stream runtime state authority., RuntimeEvent, EventSink

### Community 11 - "Session State Persistence"
Cohesion: 0.14
Nodes (11): SessionRef, ArtifactId, Explicit Run, Session, and Workspace state stores., InMemorySessionRepository, Lightweight SessionRef lifecycle state., SessionRepository, SessionState, SessionStatus (+3 more)

### Community 12 - "SQLite Event Ledger"
Cohesion: 0.14
Nodes (8): Replaceable integrations for external infrastructure., _json_dump(), _json_object(), SQLite Event sequence and AuditOutbox reference adapter., Durable per-Run sequencing and at-least-once Audit delivery state., SqliteEventLedger, Connection, Path

### Community 13 - "Run State Machine"
Cohesion: 0.35
Nodes (4): datetime, Pure state transition rules; persistence and publication live elsewhere., RunStateMachine, RunTransition

### Community 14 - "Audit Delivery Models"
Cohesion: 0.17
Nodes (7): InMemoryEventLedger, In-memory Event ports for tests and ephemeral deployments., EventAcknowledgement, EventSinkCapabilities, AuditOutbox, PendingAuditDelivery, Async EventSink, sequence, and AuditOutbox ports.

### Community 15 - "Run State Tests"
Cohesion: 0.19
Nodes (16): agent_run(), child_scope(), Shared deterministic test collaborators., session_ref(), test_failure_cancel_and_illegal_transitions(), test_lifecycle_pause_resume_retry_recovery_and_terminal_rules(), test_run_attempt_history_invariants(), test_run_invariants() (+8 more)

### Community 16 - "Event Sink Tests"
Cohesion: 0.51
Nodes (15): SinkRegistration, InMemoryEventSink, MatchingAllowPolicy, capabilities(), dispatcher(), make_event(), principal(), test_audit_fail_closed_and_compatibility_checks() (+7 more)

### Community 17 - "Documentation Migration Pages"
Cohesion: 0.15
Nodes (13): Workflow Engines Are Adapters, Memory Is Provided by Plugins, Workflow Is First-Class, Run Is Generic and SessionRef Is Lightweight, Scope Authorization Denies by Default, Recovery Is At Least Once, System Overview Migration, Documentation Registry (+5 more)

### Community 18 - "Agent Audit Integration"
Cohesion: 0.27
Nodes (10): event_dispatcher(), event_sink(), test_audit_delivery_failure_pauses_the_protected_run(), test_root_agent_run_authorization_state_and_events_close_loop(), _unused(), AcknowledgementId, Durable Event acknowledgement identity., sqlite_event() (+2 more)

### Community 19 - "Immutable Model Validation"
Cohesion: 0.21
Nodes (3): _freeze_mapping(), _require_text(), _require_unique_names()

### Community 20 - "Shared Runtime Utilities"
Cohesion: 0.20
Nodes (6): Provider-neutral typed content blocks., Structured errors shared across runtime capability boundaries., JSON value types and strict boundary narrowing helpers., Schema references and dependency-injected value validation., CoreScopeKind, Generic namespaced runtime Scope references.

### Community 21 - "Normative RFC Contracts"
Cohesion: 0.28
Nodes (9): ADR-0009 Safe Plugin Unload, Extension Over Modification, Interface First, RFC-0002 Plugin SDK, RFC-0006 ContextProvider, RFC-0007 MemoryProvider, RFC-0008 Scope and Authorization, RFC-0009 ModelProvider (+1 more)

### Community 22 - "Event Model Tests"
Cohesion: 0.36
Nodes (5): EventId, Runtime Event identity., _minimal_event(), test_event_model_invariants_and_capabilities(), test_event_model_schema_and_redaction()

### Community 23 - "JSON Codec"
Cohesion: 0.36
Nodes (6): ModelT, dumps(), JsonModel, JsonModelType, loads(), Explicit JSON boundary for public dataclass contracts.

### Community 24 - "Checkpoint Recovery Contracts"
Cohesion: 0.50
Nodes (5): Reliable Execution Over Autonomous Complexity, RFC-0003 Workflow, RFC-0004 Execution Run Lifecycle, RFC-0011 Checkpoint and Recovery, Delivery Rules and Task Snapshot

### Community 25 - "Architecture Decision Documents"
Cohesion: 0.50
Nodes (4): Runtime Events Are Not Event Sourcing, External Capability Is Interface-First, Agent Guide, Agent Harness Runtime Core Design

### Community 26 - "Event Schema Registry"
Cohesion: 0.67
Nodes (3): core_schema_registry(), require_payload_fields(), PayloadValidator

### Community 27 - "Requirements and Overview"
Cohesion: 0.67
Nodes (3): CongeriesCore System Overview, Core Is Runtime, Not Application, Runtime Core Requirements

## Knowledge Gaps
- **16 isolated node(s):** `congeries-core`, `CI Validation Workflow`, `Architecture Session Memory`, `Serena Local Overrides`, `Serena Project Configuration` (+11 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **14 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ErrorCategory` connect `Core Errors and Resources` to `Authorization and Context`, `Memory Provider Runtime`, `Runtime Call Control`, `Identifiers and Scope`, `Observation Event Delivery`, `Run State Persistence`, `Memory Provider Tests`, `Agent Runtime Models`, `Event Dispatch Authorization`, `Session State Persistence`, `SQLite Event Ledger`, `Run State Machine`, `Audit Delivery Models`, `Event Sink Tests`, `Shared Runtime Utilities`?**
  _High betweenness centrality (0.105) - this node is a cross-community bridge._
- **Why does `RunId` connect `Run State Persistence` to `Core Errors and Resources`, `Authorization and Context`, `Runtime Call Control`, `Identifiers and Scope`, `Observation Event Delivery`, `Memory Provider Tests`, `Agent Runtime Models`, `Event Dispatch Authorization`, `Session State Persistence`, `SQLite Event Ledger`, `Run State Machine`, `Audit Delivery Models`, `Run State Tests`, `Event Sink Tests`, `Event Model Tests`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `ScopeRef` connect `Identifiers and Scope` to `Core Errors and Resources`, `Authorization and Context`, `Memory Provider Runtime`, `Runtime Call Control`, `JSON Memory Validation`, `Observation Event Delivery`, `Run State Persistence`, `Memory Provider Tests`, `Agent Runtime Models`, `Event Dispatch Authorization`, `Session State Persistence`, `Run State Machine`, `Audit Delivery Models`, `Run State Tests`, `Event Sink Tests`, `Shared Runtime Utilities`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Are the 139 inferred relationships involving `ErrorCategory` (e.g. with `SqliteEventLedger` and `EventDeliveryPolicy`) actually correct?**
  _`ErrorCategory` has 139 INFERRED edges - model-reasoned connections that need verification._
- **Are the 66 inferred relationships involving `ProviderId` (e.g. with `AcceptValidator` and `RunAuditFailureHandler`) actually correct?**
  _`ProviderId` has 66 INFERRED edges - model-reasoned connections that need verification._
- **Are the 94 inferred relationships involving `AccessRequest` (e.g. with `EventDeliveryPolicy` and `EventDiagnostic`) actually correct?**
  _`AccessRequest` has 94 INFERRED edges - model-reasoned connections that need verification._
- **Are the 95 inferred relationships involving `ErrorDetail` (e.g. with `AgentExecutionResult` and `AgentRegistry`) actually correct?**
  _`ErrorDetail` has 95 INFERRED edges - model-reasoned connections that need verification._