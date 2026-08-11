# CongeriesCore Documentation Registry

Target Version: 0.2.0

## Purpose

This registry defines document roles, authority, lifecycle, and navigation for
CongeriesCore. Start with the [system overview](overview.md) for orientation.

## Authority by Subject

| Document class | Owns |
| --- | --- |
| [Principles](../principles.md) | Mandatory long-term architectural invariants |
| [Requirements](../requirements.md) | Product scope, required behavior, and quality attributes |
| [ADRs](adrs/README.md) | Accepted cross-cutting and durable decisions |
| [Design](../design.md) | Current system architecture and contract composition |
| [Accepted and Implemented RFCs](rfcs/README.md) | Detailed public contracts within their declared scope |
| [Tasks](../tasks.md) | Implementation order and acceptance criteria |
| [Overview](overview.md) | Non-normative orientation and links |
| [Agent Guide](../agents.md) | Contribution workflow |

No document may weaken a higher-level principle or requirement. Design and an
Accepted or Implemented RFC are synchronized views: design owns system
composition, while the RFC owns detailed semantics in its scope.

## RFC Lifecycle

The valid RFC statuses are:

```text
Draft -> In Review -> Accepted -> Implemented
                    -> Rejected
Accepted or Implemented -> Deprecated -> Superseded
```

- Draft and In Review RFCs are proposals and are not normative.
- Accepted and Implemented RFCs are normative within their declared scope.
- Implemented additionally means the contract has verified implementation
  coverage.
- Rejected RFCs remain available as historical proposals.
- Deprecated RFCs remain supported during a documented migration window.
- Superseded RFCs link to their replacement.
- Withdrawn/Misclassified is reserved for legacy document-governance repair.

## ADR Lifecycle

ADRs use Proposed, Accepted, Deprecated, or Superseded. An ADR records why a
cross-cutting decision was made. Detailed interface fields stay in RFCs.

## Required Metadata

Every RFC and ADR contains:

| Field | Meaning |
| --- | --- |
| ID | Permanent RFC or ADR identifier |
| Title | Stable descriptive title |
| Status | A valid lifecycle status |
| Target Version | Project baseline that accepts the document |
| Owner | Responsible maintainer group |
| Created | Initial document date |
| Updated | Last semantic update date |
| Related | Relative links to owning or dependent documents |
| Supersedes | Earlier document identifiers, or None |

Document metadata is not a substitute for public contract versioning. A schema
or interface that crosses a process or persistence boundary carries its own
version where its RFC requires one.

## Numbering and Paths

- RFC and ADR identifiers are permanent and never reused.
- RFC-0001 is Reserved; the imported draft set began with RFC-0002.
- RFCs live under `docs/rfcs/`.
- ADRs live under `docs/adrs/`.
- Legacy paths contain migration pages and no normative content.
- New identifiers increase monotonically within their document class.

## Change Synchronization

An Accepted or Implemented RFC, or an Accepted ADR, updates every affected
higher-level or downstream document in the same change:

1. Confirm compliance with principles.
2. Update requirements when observable behavior changes.
3. Add or update an ADR for a durable cross-cutting decision.
4. Update design composition and links.
5. Update the owning RFC contract.
6. Update tasks and acceptance criteria.
7. Document migration and compatibility impact.

Normative text is written in English. Repeated normative paragraphs are replaced
with relative links to the owning document.

## Registries

- [RFC Registry](rfcs/README.md)
- [ADR Registry](adrs/README.md)

## Review Guides

- [Evaluation Pipeline Code Review Guide](reviews/evaluation-pipeline-code-review.md)
- [Plugin v1 Code Review Guide](reviews/plugin-v1-code-review.md)
- [Skill and Tool v1 Code Review Guide](reviews/task-5.2-skill-tool-code-review.md)

## Legacy Migration Pages

- [Original system overview path](Agent_Harness_Runtime_Core_System_Overview.md)
- [Original RFC-0002 path](RFC-0002-Plugin-SDK-Specification.md)
- [Original RFC-0003 path](RFC-0003-Workflow-Specification.md)
- [Original RFC-0004 path](RFC-0004-Agent-Runtime-Lifecycle.md)
- [Original RFC-0005 path](RFC-0005-Architecture-Decision-Records.md)
- [Original RFC-0006 path](RFC-0006-Context-and-Memory-Provider-Specification.md)
