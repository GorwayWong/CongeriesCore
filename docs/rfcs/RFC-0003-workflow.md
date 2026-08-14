# RFC-0003: Workflow

- ID: RFC-0003
- Title: Workflow
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-12
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [RFC-0004](RFC-0004-execution-run-lifecycle.md), [RFC-0006](RFC-0006-context-provider.md), [RFC-0011](RFC-0011-checkpoint-recovery.md), [RFC-0012](RFC-0012-evaluation.md), [RFC-0013](RFC-0013-skill-tool-contracts.md), [RFC-0016](RFC-0016-tool-operation-log.md)
- Supersedes: None

## 1. Scope

This RFC defines the vendor-neutral Workflow definition, node graph, validation,
execution result, and engine adapter boundary. Concrete business workflows are
provided by plugins.

## 2. Workflow Definition

A Workflow definition contains:

| Field | Meaning |
| --- | --- |
| `contract_version` | Public Workflow wire-contract version; v0.2 accepts `"1"` |
| `workflow_id` | Stable namespaced identifier |
| `definition_id` | Exact definition identity shared with WorkflowRun and Checkpoint |
| `version` | Graph version corresponding to `WorkflowRun.graph_version` |
| `input_schema` | Accepted invocation input |
| `nodes` | Versioned node definitions |
| `dependencies` | Directed execution constraints |
| `output_schema` | Successful WorkflowResult shape |
| `output_binding` | Required whole-value node output used as the final result |
| `execution_policy` | Concurrency, timeout, retry, approval, audit, and failure behavior |

Workflow determinism means the validated graph, dependencies, transition rules,
and declared policy determine what can execute next. It does not require model
outputs or external systems to be deterministic.

## 3. Node Contract

Every node declares:

- A stable node identifier
- A node type and contract version
- Input bindings and output schema
- Timeout and retry policy
- Required Scope and permissions
- Whether the node can produce external side effects
- Idempotency requirements
- Checkpoint policy

Core node types are:

- AgentNode
- SkillNode
- ToolNode
- ContextNode
- ApprovalNode
- EvaluationNode

Plugins may register new node types through a versioned extension contract.

### 3.1 v0.2 Binding and Dependency Shape

`WorkflowDependency` is the only dependency-edge source. Nodes do not duplicate
dependency identifiers. A dependency either constrains execution order only or
carries one producer's complete output to a consumer input binding. The v0.2
runtime supports only whole-value binding from the Workflow input or one node
output. JSONPath, expression evaluation, coercion, and implicit multi-source
merging are not part of this contract version.

Schema compatibility in the direct v0.2 runtime is exact `SchemaRef` equality.
Permission evaluability means every declared Action is registered and its
resource and Scope are complete; the authorization policy still allows or denies
the actual dispatch.

### 3.2 ContextNode v1

`ContextNodeConfig` embeds one existing `ContextBinding`. It does not introduce a
binding registry. ContextNode v1 is a data source: `input_schema` is `None`,
`input_bindings` is empty, and `output_schema` is exactly
`CONTEXT_NODE_RESULT_SCHEMA`, defined as
`SchemaRef("core", "context_node_result", "1")`. The node is non-side-effecting,
requires a stable idempotency identity, and always enables its Checkpoint.

`ContextNodeResult` is the strict durable value contract. It contains
`contract_version`, `entries`, `completeness`, `missing_keys`, `warnings`,
`selected_providers`, and `usage`. Version 1 accepts no unknown or missing fields.
`ContextNodeResultSchemaValidator` parses this exact shape and is registered for
the fixed result Schema. The runtime converts `ResolvedContext` into this value;
the ContextProvider contract and Checkpoint wire format remain unchanged.

Before the Workflow enters RUNNING or writes its initial Checkpoint, validation
requires every binding requirement Schema and the fixed result Schema to be
registered. Both `core.context.capabilities` and `core.context.provide` must be
registered and declared for every configured Provider using the exact resource
`core/context_provider/{provider_id}`.

Execution first authorizes `core.workflow.node.execute`, derives the narrowed node
`RuntimeCallContext`, and calls only `ContextResolver.resolve`. A partial result is
successful only for `ALLOW_PARTIAL`; `REQUIRE_COMPLETE` raises
`partial_context_rejected`. A successful result is validated and persisted through
`NodeOutputPersistence` before the stable node Checkpoint is committed. Only the
committed Checkpoint may unlock dependents. Denial, timeout, cancellation, late
results, Provider or Schema failure, and persistence failure create no successful
node boundary. Recovery skips a committed ContextNode and replays interrupted work
with the same idempotency identity.

### 3.3 ContextNode in Plain Language (Non-Normative)

ContextNode is the Workflow equivalent of a read-only data-loading step. The
Workflow definition says which Context Providers may be consulted and which typed
pieces of context are required. The node does not receive the Workflow input and
does not call a Provider directly. It hands the embedded binding to the existing
ContextResolver, which already knows how to authorize, select, call, cancel, and
validate Providers.

The result is copied into a small, versioned Workflow-owned JSON document. This
copy is important: the Workflow needs a stable value it can store and load later,
instead of placing a live resolver object in a Checkpoint. The actual value is
stored by NodeOutputPersistence. The Checkpoint stores only the durable reference.

The success sequence is therefore:

```text
validate definition
    -> authorize Workflow node execution
    -> ContextResolver resolves and validates Provider data
    -> convert to ContextNodeResult and validate its fixed Schema
    -> persist the result and obtain a durable reference
    -> commit the Checkpoint by compare-and-set
    -> mark the node complete and release dependent nodes
```

There are two intentional crash windows:

1. If the result is durable but the Checkpoint is not committed, recovery treats
   the node as interrupted. It resolves again with the same idempotency identity;
   persistence may safely return the existing reference.
2. If the Checkpoint is committed but the in-memory scheduler was not updated,
   recovery reads the committed success state and skips Provider execution.

For review, the most important negative guarantee is simple: no denial, timeout,
cancellation, partial result rejected by policy, malformed Provider result, Schema
failure, persistence failure, or discarded late result can create a successful
Checkpoint or release downstream work.

This increment does not add a new Context Provider API, binding registry,
authorization action, persistence format, Checkpoint field, scheduler, or recovery
algorithm. It composes existing boundaries and freezes only the Workflow-specific
config and durable result contracts.

### 3.4 ContextNode Review Checklist (Non-Normative)

- The config and result are frozen, versioned, and reject unknown fields at every
  nested public boundary.
- The node has no input binding and always uses the fixed result Schema.
- Every requirement Schema and both Context actions are registered before RUNNING.
- Every bound Provider has both actions declared against its exact resource.
- WorkflowRuntime imports no Provider implementation or Provider registry path.
- Persistence completes before the successful Checkpoint is constructed and saved.
- Scheduler completion occurs only after Checkpoint compare-and-set succeeds.
- Stable recovery skips the Provider; interrupted recovery keeps the same key.
- Failure and late-result tests assert both zero persistence and zero downstream
  execution.

### 3.5 SkillNode v1

`SkillNodeConfig` names one exact Skill `CapabilityRef`, one declared resource ID,
and a positive byte budget. SkillNode has no input, is non-side-effecting, requires
a stable idempotency identity and Checkpoint, and has the fixed output Schema
`core/skill_node_result/1`. `SkillNodeResult` is strict, versioned, and contains the
complete `SkillResource` value.

Validation resolves the Skill through `SkillToolResolver`, checks owner and
contract version, declaration and byte budget, registered Action, and the exact
`core/skill_resource/{skill_id}:{resource_id}` permission. Runtime authorizes node
execution and calls only `SkillResourceGateway.load`. It never receives a loader,
Plugin implementation, or filesystem path. Result persistence, Checkpoint CAS,
and downstream unlock occur in that order. Interrupted work reuses the same
logical identity; committed work is skipped. Sequential replay is permitted while
overlapping duplicate Plugin reads still conflict.

### 3.6 ToolNode v1

`ToolNodeConfig` contains one exact Tool `CapabilityRef`. The node has exactly one
whole-value input binding and the fixed output Schema `core/tool_node_result/1`.
Its side-effect flag must equal the resolved descriptor classification; every Tool
except an explicit `none` descriptor is treated as side-effecting. External Tools
require caller-key idempotency and Checkpoint.

`ToolNodeRequest` freezes the `ToolCall`, resolved descriptor snapshot, Scope, and
node timeout. Canonical JSON is hashed as a lowercase SHA-256 request fingerprint.
`ToolNodeResult` freezes operation and Tool identity, fingerprint, durable status,
and exactly one ToolResult or structured error, with optional confirmation
evidence. Both contracts reject unknown fields and use fixed request/result
Schemas.

The execution order is normative:

```text
authorize and preflight
    -> persist ToolNodeRequest
    -> prepare ToolOperationRecord
    -> commit a pending pre-dispatch Checkpoint with operation references
    -> ToolGateway with ToolExecutionGuard
    -> persist ToolNodeResult
    -> finalize ToolOperationRecord
    -> commit the stable node Checkpoint
    -> unlock dependents only on succeeded
```

ToolOperationLog state and CAS are owned by RFC-0016. A recovered `prepared`
operation may dispatch with the same key and fingerprint. Recovered `dispatching`
becomes `unknown`; `unknown` never invokes the Tool or unlocks dependents. It
commits a pending suspension Checkpoint and pauses the same WorkflowRun.
`ToolOperationSuspension` contains the paused Run, Checkpoint reference, operation
identity, and record version.

`resolve_tool_operation` accepts `ToolOperationResolution` plus an application
`RuntimePrincipal`. Resolution requires the expected CAS version and durable
evidence. Success validates output Schema, persists and finalizes success, resumes
the same Run, and continues scheduling. Failure persists and finalizes a structured
error and terminates the Run. Core performs no automatic external query and never
uses Runtime Events as recovery authority.

## 4. Graph Validation

Validation occurs before a WorkflowRun enters RUNNING and rejects:

- Schema incompatibility
- Duplicate or missing node identifiers
- Missing dependencies
- Cycles not explicitly supported by the selected engine contract
- Unreachable required outputs
- Missing capability registrations
- Side-effecting nodes without idempotency requirements
- Permission declarations that cannot be evaluated
- Unsupported graph or node versions

Validation failure produces no node execution.
It also produces no output persistence call or execution Checkpoint.

## 5. Execution

Workflow invocation creates a WorkflowRun. The engine:

1. Validates input and graph.
2. Resolves ready nodes from completed dependencies and policy.
3. Authorizes each node before dispatch.
4. Executes nodes using RuntimeCallContext.
5. Records node outcome and Runtime Events.
6. Commits checkpoints at declared stable boundaries and advances the
   WorkflowRun recovery marker by compare-and-set.
7. Produces WorkflowResult after output validation.

An AgentNode creates a child AgentRun whose parent is the WorkflowRun. Other
nodes do not impersonate Agent execution.

## 6. Node Outcomes

A node outcome is one of:

- Succeeded with typed output
- Failed with structured error
- Denied
- Timed out
- Cancelled
- Waiting for approval
- Retry scheduled

ExecutionPolicy defines fail-fast, continue, compensation, and retry choices
only where the selected node contract supports them. It cannot turn denial or
invalid output into success.

## 7. Approval and Evaluation

ApprovalNode transitions the WorkflowRun to WAITING_APPROVAL after an atomic
checkpoint and reliable audit event. An authorized decision returns the Run to
RUNNING or terminates it according to policy only after the decision checkpoint
and reliable audit event. Recovery, migration, and fallback follow RFC-0011 and
never infer state from event replay.

EvaluationNode validates schema, policy, and quality. Evaluation output is typed
and auditable.

## 8. Engine Adapter

Core owns the Workflow contract, not an execution framework. An engine adapter:

- Accepts the normalized Workflow definition.
- Preserves Run, Scope, event, checkpoint, cancellation, and error semantics.
- Does not expose framework-native types through Core APIs.
- Declares supported graph and node capabilities.

Custom DAG, LangGraph, Temporal, or other engines may be adapters.

## 9. Result

WorkflowResult contains:

- WorkflowRun identity and terminal status
- Validated output on success
- Structured error on failure
- Artifact references
- Final checkpoint reference where available
- Attempt and timing summary

Partial node output is not a successful WorkflowResult unless the output schema
and execution policy explicitly define a partial-success shape.

## 10. Incremental Reference Runtime

The first Core reference runtime is deliberately smaller than the complete node
catalog. It implements the normalized Workflow contracts, validates the DAG
before execution, schedules dependencies deterministically, executes AgentNode
by creating child AgentRuns, coordinates ApprovalNode, composes EvaluationNode
through the [Evaluation contract](RFC-0012-evaluation.md), and resolves ContextNode
through the existing ContextResolver, loads SkillNode through SkillResourceGateway,
and executes ToolNode through ToolGateway plus the durable Tool Operation Log.
Unsupported node
contracts are rejected during validation; they are never silently skipped or
treated as success.

The reference runtime commits workflow-start and stable AgentNode, ContextNode,
SkillNode, ToolNode, and EvaluationNode boundaries through CheckpointCoordinator.
On recovery,
CheckpointRestorer must finish
rehydrating stable node outcomes, pending nodes, external references, and
side-effect identities before dependency scheduling resumes. A node with a
stable committed outcome is not dispatched again. Work interrupted after the
latest committed boundary may replay under the at-least-once contract.

AgentNode output and EvaluationResult required by downstream nodes or recovery
are not copied into a Checkpoint. Before that node outcome becomes a stable
boundary, an injected authorized persistence boundary converts the required
value into typed, scoped durable references. Evaluation non-success results use
the same boundary through `error_ref`. If no durable reference is available, the
executor must not commit the node boundary. The first reference runtime selects
no mandatory Artifact or storage implementation.

The injected `NodeOutputPersistence` boundary exposes `persist` and `load`; each
operation receives `RuntimeCallContext` and returns or consumes a typed, scoped
`CheckpointReference`. Calls are authorized through the registered v1 actions
`core.workflow.output.persist` and `core.workflow.output.load`. Node dispatch uses
`core.workflow.node.execute` v1. The Checkpoint wire contract remains unchanged.

ApprovalNode composes the existing durable ApprovalCoordinator. Custom node
registration and external Workflow engine adapters are later slices.
Delivering the smaller reference runtime does not change this RFC to Implemented;
full conformance still requires the complete contract below.

The direct reference implementation depends on normalized public contracts and
must not expose internal scheduler types, preserving later adapter replacement.

## 11. Conformance

A conforming implementation demonstrates:

- Validation before execution.
- Correct dependency scheduling.
- Child AgentRun relationships.
- Authorization before every node dispatch.
- Approval checkpoint and resume behavior.
- Cancellation propagation.
- SkillNode resource authorization, persistence ordering, and stable replay.
- ToolNode request fingerprint, operation-log CAS, unknown suspension, and explicit
  resolution behavior.
- Replaceable engine adapters with identical public outcomes.
- Recovery behavior defined by RFC-0011.
