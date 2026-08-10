# RFC-0006: ContextProvider

- ID: RFC-0006
- Title: ContextProvider
- Status: Implemented
- Target Version: 0.2.0
- Owner: CongeriesCore Maintainers
- Created: 2026-08-10
- Updated: 2026-08-10
- Related: [Requirements](../../requirements.md), [Design](../../design.md), [RFC-0008](RFC-0008-scope-authorization.md)
- Supersedes: Context portions of the legacy RFC-0006 draft

## 1. Scope

This RFC defines explicit runtime context resolution and injection. Memory is
defined by [RFC-0007](RFC-0007-memory-provider.md), and authorization is defined
by [RFC-0008](RFC-0008-scope-authorization.md).

Context answers what a Run needs for its current execution. It is short-lived
runtime input and is not persistent memory.

## 2. Core Types

### ContextRequest

ContextRequest contains:

- Run and definition identity
- RuntimeCallContext
- Required context keys and expected schemas
- Optional provider selectors
- Completeness policy
- Size or token budget
- Deadline and cancellation

Business values may appear in namespaced context payloads. Core validates their
declared schema but does not interpret their meaning.

### ContextResult

ContextResult contains:

- Provider identity and contract version
- Namespaced typed context entries
- COMPLETE or PARTIAL completeness
- Missing keys and structured warnings
- Provenance references
- Expiration or freshness metadata where applicable
- Provider-neutral usage and budget accounting

PARTIAL is a distinct successful transport outcome. ExecutionPolicy decides
whether it is sufficient for the requesting node.

Context entries and structured model output share registered `SchemaRef` values
and an injected `SchemaRegistry`. Core provides no application schema engine.
`ContentBlock` provides the shared provider-neutral `text`, `json`, and
`reference` content representation.

### ContextBinding

ContextBinding stores ordered Provider references, required key and schema
pairs, budget, merge strategy, and completeness policy. It never stores Provider
implementation objects. ContextProviderRegistry owns implementations;
ContextResolver owns selection and invocation.

## 3. Provider Contract

The protocol exposes:

```text
provide(ContextRequest) -> ContextResult
capabilities(RuntimeCallContext) -> ContextCapabilities
```

ContextCapabilities declares supported keys, schemas, Scope patterns, maximum
budgets, and partial-result support.

Provider implementation, external queries, caching, and application data models
remain outside Core.

## 4. Resolution Lifecycle

```text
Receive ContextRequest
    -> validate request and deadline
    -> authorize requested keys and Scope
    -> select compatible providers
    -> invoke providers with RuntimeCallContext
    -> merge typed entries by declared policy
    -> validate completeness and budget
    -> inject ContextResult into runtime
```

Provider selection is deterministic for the same registry, request, and policy.
Priority, composition, fallback, and merge strategy are declared rather than
discovered from hidden global state.

The reference Agent Runtime rejects PARTIAL by default. A binding must declare
`ALLOW_PARTIAL` before execution may continue with an incomplete result.

## 5. Merge Rules

The resolver supports declared strategies:

- `single`: exactly one provider supplies a key.
- `first_success`: ordered providers stop after an acceptable result.
- `merge`: schema-defined merge across providers.
- `all`: preserve provider-separated results.

Conflicting values without an explicit merge rule return conflict. The resolver
does not silently select the last writer.

## 6. Authorization

Authorization occurs before provider invocation and may restrict keys, Scope,
budget, or provider choice. A provider receives only the authorized request.

Core registers `core.context.capabilities` and `core.context.provide`, version
`1`. AccessRequest identifies the Provider resource and carries requested key
names and budgets. A grant may retain or reduce the key set and budget; unknown,
malformed, empty, or expanding constraints are invalid grants.

Provider-internal checks may further restrict access but cannot expand the grant.
Denial emits an audit event and returns the standard denied error.

## 7. Failure Semantics

Outcomes include:

- Invalid request or schema
- Denied
- Unavailable provider
- Timeout
- Cancelled
- Conflict
- Version mismatch
- Partial result

Timeout and cancellation propagate to every selected provider. Late provider
results are discarded and cannot mutate injected context.

## 8. Observability and Redaction

Resolution emits `core.context.resolution_started`,
`core.context.provider_selected`, `core.context.resolution_completed`, and
`core.context.resolution_failed` as OBSERVABILITY events. Payload values are
excluded. Events carry Provider references, key and entry counts, usage,
latency, completeness, outcome, and redacted error codes.

## 9. Conformance

A conforming implementation demonstrates:

- No provider invocation before authorization.
- Capability discovery receives RuntimeCallContext and cannot bypass
  authorization.
- Deterministic provider selection and merge behavior.
- Distinct complete, partial, denied, timeout, cancelled, and failed outcomes.
- Budget enforcement.
- Cancellation of all outstanding provider calls.
- No implicit database access or hidden global context.
