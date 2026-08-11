"""Authorized persistence boundary for recoverable Workflow node outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from congeries_core.checkpoint import CheckpointReference
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.ids import (
    IdempotencyKey,
    NodeId,
    PrincipalId,
    ResourceId,
    RunId,
)
from congeries_core.runtime.json_types import JsonValue, as_json_value
from congeries_core.runtime.schema import SchemaRef
from congeries_core.runtime.scope import ScopeRef

WORKFLOW_NODE_EXECUTE_ACTION = ActionRef("core", "workflow.node.execute", "1")
WORKFLOW_OUTPUT_PERSIST_ACTION = ActionRef("core", "workflow.output.persist", "1")
WORKFLOW_OUTPUT_LOAD_ACTION = ActionRef("core", "workflow.output.load", "1")
WORKFLOW_OUTPUT_RESOURCE_TYPE = "workflow_node_output"


def workflow_actions() -> tuple[ActionRef, ...]:
    return (
        WORKFLOW_NODE_EXECUTE_ACTION,
        WORKFLOW_OUTPUT_PERSIST_ACTION,
        WORKFLOW_OUTPUT_LOAD_ACTION,
    )


@dataclass(frozen=True, slots=True)
class PersistNodeOutputRequest:
    run_id: RunId
    node_id: NodeId
    schema: SchemaRef
    value: JsonValue
    scope: ScopeRef
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", as_json_value(self.value, "persisted node output")
        )


@dataclass(frozen=True, slots=True)
class LoadNodeOutputRequest:
    run_id: RunId
    node_id: NodeId
    schema: SchemaRef
    reference: CheckpointReference
    scope: ScopeRef

    def __post_init__(self) -> None:
        self.reference.scope.require_narrower_than(self.scope)


class NodeOutputStore(Protocol):
    async def persist(
        self, request: PersistNodeOutputRequest, context: RuntimeCallContext
    ) -> CheckpointReference: ...

    async def load(
        self, request: LoadNodeOutputRequest, context: RuntimeCallContext
    ) -> JsonValue: ...


class NodeOutputPersistence(Protocol):
    async def persist(
        self, request: PersistNodeOutputRequest, context: RuntimeCallContext
    ) -> CheckpointReference: ...

    async def load(
        self, request: LoadNodeOutputRequest, context: RuntimeCallContext
    ) -> JsonValue: ...


class AuthorizedNodeOutputPersistence:
    """The Core-owned authorization gateway; the backing store remains injected."""

    def __init__(
        self,
        *,
        store: NodeOutputStore,
        dispatcher: AuthorizedDispatcher[object],
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher

    async def persist(
        self, request: PersistNodeOutputRequest, context: RuntimeCallContext
    ) -> CheckpointReference:
        access = self._access(
            request.run_id,
            request.node_id,
            request.scope,
            context,
            WORKFLOW_OUTPUT_PERSIST_ACTION,
        )

        async def operation(call: AuthorizedCall) -> object:
            return await self._store.persist(request, call.context)

        raw = await self._dispatcher.dispatch(access, operation)
        if not isinstance(raw, CheckpointReference):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_output_reference_invalid",
                "node output persistence returned an invalid reference",
            )
        reference = raw
        self._validate_reference(reference, request.scope, request.schema)
        return reference

    async def load(
        self, request: LoadNodeOutputRequest, context: RuntimeCallContext
    ) -> JsonValue:
        self._validate_reference(request.reference, request.scope, request.schema)
        access = self._access(
            request.run_id,
            request.node_id,
            request.scope,
            context,
            WORKFLOW_OUTPUT_LOAD_ACTION,
        )

        async def operation(call: AuthorizedCall) -> object:
            return await self._store.load(request, call.context)

        raw = await self._dispatcher.dispatch(access, operation)
        return as_json_value(raw, "loaded node output")

    def _access(
        self,
        run_id: RunId,
        node_id: NodeId,
        scope: ScopeRef,
        context: RuntimeCallContext,
        action: ActionRef,
    ) -> AccessRequest:
        if context.run_id != run_id:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "workflow_output_context_mismatch",
                "node output call context does not match WorkflowRun",
            )
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.WORKFLOW_NODE, PrincipalId(node_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core",
                "workflow_node_output",
                ResourceId(f"{run_id.value}:{node_id.value}"),
            ),
            scope=scope,
            context=context,
            constraints={},
        )

    def _validate_reference(
        self, reference: CheckpointReference, scope: ScopeRef, schema: SchemaRef
    ) -> None:
        if reference.resource_type != WORKFLOW_OUTPUT_RESOURCE_TYPE:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "workflow_output_reference_type_invalid",
                "node output reference has the wrong resource type",
            )
        reference.scope.require_narrower_than(scope)
        if reference.version != schema.version:
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "workflow_output_schema_version_mismatch",
                "node output reference version does not match its schema",
            )
