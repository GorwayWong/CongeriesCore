"""Deterministic one-at-a-time dependency scheduler for Workflow v0.2."""

from __future__ import annotations

from collections.abc import Collection

from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import NodeId

from .model import WorkflowDefinition, WorkflowNode
from .validation import ValidatedWorkflow


class DeterministicScheduler:
    def __init__(
        self,
        workflow: ValidatedWorkflow,
        completed: Collection[NodeId] = (),
    ) -> None:
        self._definition = workflow.definition
        self._nodes = {item.node_id: item for item in self._definition.nodes}
        self._completed = set(completed)
        if not self._completed.issubset(self._nodes):
            raise ValueError("scheduler completed set contains an unknown node")
        self._dependencies: dict[NodeId, frozenset[NodeId]] = {
            node_id: frozenset(
                item.source_node_id
                for item in self._definition.dependencies
                if item.target_node_id == node_id
            )
            for node_id in self._nodes
        }

    @property
    def definition(self) -> WorkflowDefinition:
        return self._definition

    @property
    def completed(self) -> frozenset[NodeId]:
        return frozenset(self._completed)

    def ready(self) -> tuple[WorkflowNode, ...]:
        return tuple(
            self._nodes[node_id]
            for node_id in sorted(self._nodes, key=lambda item: item.value)
            if node_id not in self._completed
            and self._dependencies[node_id].issubset(self._completed)
        )

    def next(self) -> WorkflowNode | None:
        ready = self.ready()
        return ready[0] if ready else None

    def mark_completed(self, node_id: NodeId) -> None:
        if node_id in self._completed:
            return
        ready = {item.node_id for item in self.ready()}
        if node_id not in ready:
            raise core_error(
                ErrorCategory.CONFLICT,
                "workflow_node_not_ready",
                "Workflow node dependencies are not stably completed",
            )
        self._completed.add(node_id)

    def pending_nodes(self) -> tuple[NodeId, ...]:
        return tuple(
            node_id
            for node_id in sorted(self._nodes, key=lambda item: item.value)
            if node_id not in self._completed
        )

    @property
    def done(self) -> bool:
        return len(self._completed) == len(self._nodes)
