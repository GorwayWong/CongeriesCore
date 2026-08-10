# RFC-0009: ModelProvider

- ID: RFC-0009
- Title: ModelProvider
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [ADR-0005](../adrs/ADR-0005-interface-first.md), [RFC-0008](RFC-0008-scope-authorization.md)
- Supersedes: None

## 1. Scope

This RFC defines a vendor-neutral model capability boundary for generation,
streaming, capability discovery, usage, cancellation, and structured errors.
It does not standardize vendor-specific tuning or expose vendor SDK types.

## 2. Protocol

ModelProvider exposes:

```text
generate(ModelRequest, RuntimeCallContext) -> ModelResponse
stream(ModelRequest, RuntimeCallContext) -> ModelEvent stream
capabilities(ModelSelector, RuntimeCallContext) -> ModelCapabilities
```

Provider implementations may support only declared capabilities. Unsupported
operations return a structured unsupported-capability error.

## 3. Model Binding

AgentSpec contains a ModelBinding:

- Binding reference
- Provider identifier
- Model identifier
- Required capability constraints
- Default request policy
- Optional fallback bindings declared in order

AgentSpec does not contain a vendor client or vendor request object.
The primary selector and ordered fallbacks are registry references. Fallback is
permitted only for unavailable Providers or unsupported capability. Denial,
timeout, cancellation, invalid request, and protocol failure do not fall through
to another selector.

## 4. ModelRequest

ModelRequest contains:

- Model selector
- Typed input content
- Instruction and context references
- Optional Tool capability declarations
- Requested structured-output schema
- Generation policy and budget
- Trace and idempotency identity where supported

Deadline, cancellation, Scope, and authorization are carried by
RuntimeCallContext rather than duplicated in vendor fields.

## 5. ModelResponse

ModelResponse contains:

- Typed output content
- Structured output when requested
- Tool call requests, if allowed
- Finish reason
- Usage in provider-neutral units where available
- Provider and model identity
- Warnings and provenance

Structured output is validated before success. Invalid structured output is a
distinct failure or policy-controlled retry, not untyped success.
Validation uses the same injected `SchemaRegistry` and `SchemaRef` contract as
Context entries.

## 6. Streaming

ModelEvent types include start, content delta, structured delta, tool request,
usage update, completion, and failure.

The stream has exactly one terminal completion or failure event. Cancellation
requests provider cleanup; events received after terminalization are discarded.

Wire event types are `start`, `content_delta`, `structured_delta`,
`tool_request`, `usage_update`, `completion`, and `failure`. Provider exceptions,
deadline, cancellation, missing start, missing terminal, and invalid completion
are normalized to one terminal failure event. The gateway closes an underlying
closable async iterator before returning.

Partial content is not a successful ModelResponse unless request policy defines
an explicit partial-result shape.

## 7. Capabilities

ModelCapabilities declares at least:

- Generation and streaming support
- Structured-output support
- Tool-call support
- Input and output modalities
- Declared budget limits
- Usage reporting support
- Provider contract version

Core selects only bindings that meet required constraints.

## 8. Authorization and Policy

Authorization occurs before provider dispatch and covers provider, model,
operation, Scope, Tool exposure, and budget. Provider-internal policy may narrow
the grant but cannot broaden it.

Core registers `core.model.capabilities`, `core.model.generate`, and
`core.model.stream`, version `1`. A grant must preserve the requested model and
may only reduce the declared Tool set and input or output budget.

Tool calls requested by a model are proposals. Each Tool dispatch is separately
authorized under RuntimeCallContext.

The minimal Agent Runtime does not implement a Tool loop. It fails if a model
proposes a Tool to an Agent with no Tool references; the Model gateway never
executes a proposal.

Generate and stream emit `core.model.invocation_started`,
`core.model.invocation_completed`, or `core.model.invocation_failed` as redacted
OBSERVABILITY events. They contain operation, Provider/model references, usage,
latency, outcome, and safe error codes, never prompt or output content.

## 9. Failure Semantics

Outcomes include invalid request, denied, unavailable, timeout, cancelled,
conflict, version mismatch, partial result, unsupported capability, invalid
structured output, and provider protocol failure.

Each error identifies retryability without exposing provider secrets. Provider
rate-limit details may be mapped to unavailable plus a safe retry hint.

## 10. Conformance

A conforming implementation demonstrates:

- Identical public behavior across at least two fake providers.
- No vendor type in public contracts.
- Deadline and cancellation propagation for generation and streaming.
- Exactly one terminal stream event.
- Structured-output validation.
- Usage and capability reporting.
- Capability discovery receives RuntimeCallContext and cannot bypass
  authorization.
- Model Tool requests remain proposals and are never dispatched through the
  ModelProvider boundary; future Tool execution must be separately authorized.
