# RFC-0003: Workflow

- ID: RFC-0003
- Title: Workflow
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-11
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [RFC-0004](RFC-0004-execution-run-lifecycle.md), [RFC-0011](RFC-0011-checkpoint-recovery.md)
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
before execution, schedules dependencies deterministically, and executes
AgentNode by creating child AgentRuns. Unsupported node contracts are rejected
during validation; they are never silently skipped or treated as success.

The reference runtime commits workflow-start and stable AgentNode boundaries
through CheckpointCoordinator. On recovery, CheckpointRestorer must finish
rehydrating stable node outcomes, pending nodes, external references, and
side-effect identities before dependency scheduling resumes. A node with a
stable committed outcome is not dispatched again. Work interrupted after the
latest committed boundary may replay under the at-least-once contract.

AgentNode text or JSON output required by downstream nodes or recovery is not
copied into a Checkpoint. Before that node outcome becomes a stable boundary, an
injected authorized persistence boundary converts the required output into typed,
scoped durable references. If no durable reference is available, the executor
must not commit the node as stably completed. The first reference runtime selects
no mandatory Artifact or storage implementation.

The injected `NodeOutputPersistence` boundary exposes `persist` and `load`; each
operation receives `RuntimeCallContext` and returns or consumes a typed, scoped
`CheckpointReference`. Calls are authorized through the registered v1 actions
`core.workflow.output.persist` and `core.workflow.output.load`. Node dispatch uses
`core.workflow.node.execute` v1. The Checkpoint wire contract remains unchanged.

ApprovalNode composes the existing durable ApprovalCoordinator. SkillNode,
ToolNode, ContextNode, EvaluationNode, custom node registration, and external
Workflow engine adapters are later slices.
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
- Replaceable engine adapters with identical public outcomes.
- Recovery behavior defined by RFC-0011.
