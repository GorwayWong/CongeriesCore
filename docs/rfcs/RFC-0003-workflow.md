# RFC-0003: Workflow

- ID: RFC-0003
- Title: Workflow
- Status: Accepted
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
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
| `workflow_id` | Stable namespaced identifier |
| `version` | Graph and contract version |
| `input_schema` | Accepted invocation input |
| `nodes` | Versioned node definitions |
| `dependencies` | Directed execution constraints |
| `output_schema` | Successful WorkflowResult shape |
| `execution_policy` | Concurrency, timeout, retry, approval, audit, and failure behavior |

Workflow determinism means the validated graph, dependencies, transition rules,
and declared policy determine what can execute next. It does not require model
outputs or external systems to be deterministic.

## 3. Node Contract

Every node declares:

- A stable node identifier
- A node type and contract version
- Input bindings and output schema
- Dependencies
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

## 5. Execution

Workflow invocation creates a WorkflowRun. The engine:

1. Validates input and graph.
2. Resolves ready nodes from completed dependencies and policy.
3. Authorizes each node before dispatch.
4. Executes nodes using RuntimeCallContext.
5. Records node outcome and Runtime Events.
6. Commits checkpoints at declared stable boundaries.
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
RUNNING or terminates it according to policy.

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

## 10. Conformance

A conforming implementation demonstrates:

- Validation before execution.
- Correct dependency scheduling.
- Child AgentRun relationships.
- Authorization before every node dispatch.
- Approval checkpoint and resume behavior.
- Cancellation propagation.
- Replaceable engine adapters with identical public outcomes.
- Recovery behavior defined by RFC-0011.

