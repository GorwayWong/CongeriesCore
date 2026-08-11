"""Authorized CheckpointStore gateway and operation-specific grant narrowing."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import replace

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.provider._control import await_provider
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import (
    CheckpointRef,
    PrincipalId,
    ProviderId,
    ResourceId,
    RunId,
)
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.scope import ScopeRef

from .model import (
    Checkpoint,
    CheckpointPage,
    CheckpointQuery,
    DeleteCheckpointRequest,
    DeleteCheckpointResult,
)
from .store import CheckpointStoreRegistry

CHECKPOINT_SAVE_ACTION = ActionRef("core", "checkpoint.save", "1")
CHECKPOINT_LOAD_ACTION = ActionRef("core", "checkpoint.load", "1")
CHECKPOINT_LIST_ACTION = ActionRef("core", "checkpoint.list", "1")
CHECKPOINT_DELETE_ACTION = ActionRef("core", "checkpoint.delete", "1")
APPROVAL_DECIDE_ACTION = ActionRef("core", "approval.decide", "1")


def checkpoint_actions() -> tuple[ActionRef, ...]:
    return (
        CHECKPOINT_SAVE_ACTION,
        CHECKPOINT_LOAD_ACTION,
        CHECKPOINT_LIST_ACTION,
        CHECKPOINT_DELETE_ACTION,
        APPROVAL_DECIDE_ACTION,
    )


class CheckpointGateway:
    def __init__(
        self,
        registry: CheckpointStoreRegistry,
        dispatcher: AuthorizedDispatcher[object],
        clock: Clock,
    ) -> None:
        self._registry = registry
        self._dispatcher = dispatcher
        self._clock = clock

    async def save(
        self,
        provider_id: ProviderId,
        checkpoint: Checkpoint,
        context: RuntimeCallContext,
    ) -> CheckpointRef:
        checkpoint.scope.require_narrower_than(context.scope)
        checkpoint.verify_integrity()
        constraints: dict[str, JsonValue] = {
            "run_id": checkpoint.run_id.value,
            "workflow_id": checkpoint.workflow_id.value,
            "definition_id": checkpoint.definition_id.value,
            "graph_version": checkpoint.graph_version,
            "sequence": checkpoint.sequence,
            "previous_checkpoint_ref": (
                checkpoint.previous_checkpoint_ref.value
                if checkpoint.previous_checkpoint_ref
                else None
            ),
        }
        request = self._request(
            provider_id, CHECKPOINT_SAVE_ACTION, checkpoint.scope, context, constraints
        )

        async def operation(call: AuthorizedCall) -> CheckpointRef:
            _require_exact_constraints(call.grant.constraints, constraints)
            store = self._registry.get(provider_id)
            result = await self._invoke(
                store.save(checkpoint, call.context), call.context
            )
            if result != checkpoint.ref:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "invalid_checkpoint_save_result",
                    "CheckpointStore returned an unexpected reference",
                )
            return result

        result = await self._dispatcher.dispatch(request, operation)
        if not isinstance(result, CheckpointRef):
            raise AssertionError("checkpoint save dispatcher result is invalid")
        return result

    async def load(
        self,
        provider_id: ProviderId,
        checkpoint_ref: CheckpointRef,
        run_id: RunId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> Checkpoint:
        scope.require_narrower_than(context.scope)
        constraints: dict[str, JsonValue] = {
            "checkpoint_ref": checkpoint_ref.value,
            "run_id": run_id.value,
        }
        request = self._request(
            provider_id, CHECKPOINT_LOAD_ACTION, scope, context, constraints
        )

        async def operation(call: AuthorizedCall) -> Checkpoint:
            _require_exact_constraints(call.grant.constraints, constraints)
            store = self._registry.get(provider_id)
            result = await self._invoke(
                store.load(checkpoint_ref, call.context), call.context
            )
            if (
                result.ref != checkpoint_ref
                or result.run_id != run_id
                or result.scope.key != call.context.scope.key
            ):
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "checkpoint_identity_mismatch",
                    "loaded checkpoint identity does not match the authorized request",
                )
            result.verify_integrity()
            return result

        result = await self._dispatcher.dispatch(request, operation)
        if not isinstance(result, Checkpoint):
            raise AssertionError("checkpoint load dispatcher result is invalid")
        return result

    async def list(
        self, query: CheckpointQuery, context: RuntimeCallContext
    ) -> CheckpointPage:
        query.scope.require_narrower_than(context.scope)
        constraints: dict[str, JsonValue] = {
            "run_id": query.run_id.value,
            "graph_version": query.graph_version,
            "limit": query.limit,
        }
        request = self._request(
            query.provider_id, CHECKPOINT_LIST_ACTION, query.scope, context, constraints
        )

        async def operation(call: AuthorizedCall) -> CheckpointPage:
            effective = _constrain_list(query, call.grant.constraints)
            store = self._registry.get(query.provider_id)
            result = await self._invoke(
                store.list(effective, call.context), call.context
            )
            _validate_page(result, effective)
            return result

        result = await self._dispatcher.dispatch(request, operation)
        if not isinstance(result, CheckpointPage):
            raise AssertionError("checkpoint list dispatcher result is invalid")
        return result

    async def delete(
        self,
        request_value: DeleteCheckpointRequest,
        context: RuntimeCallContext,
    ) -> DeleteCheckpointResult:
        request_value.scope.require_narrower_than(context.scope)
        constraints: dict[str, JsonValue] = {
            "checkpoint_ref": request_value.checkpoint_ref.value,
            "run_id": request_value.run_id.value,
        }
        request = self._request(
            request_value.provider_id,
            CHECKPOINT_DELETE_ACTION,
            request_value.scope,
            context,
            constraints,
        )

        async def operation(call: AuthorizedCall) -> DeleteCheckpointResult:
            _require_exact_constraints(call.grant.constraints, constraints)
            store = self._registry.get(request_value.provider_id)
            result = await self._invoke(
                store.delete(request_value, call.context), call.context
            )
            if result.checkpoint_ref != request_value.checkpoint_ref:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "invalid_checkpoint_delete_result",
                    "CheckpointStore returned an invalid delete result",
                )
            return result

        result = await self._dispatcher.dispatch(request, operation)
        if not isinstance(result, DeleteCheckpointResult):
            raise AssertionError("checkpoint delete dispatcher result is invalid")
        return result

    def _request(
        self,
        provider_id: ProviderId,
        action: ActionRef,
        scope: ScopeRef,
        context: RuntimeCallContext,
        constraints: Mapping[str, JsonValue],
    ) -> AccessRequest:
        return AccessRequest(
            principal=RuntimePrincipal.core(
                CorePrincipalKind.RUN, PrincipalId(context.run_id.value)
            ),
            action=action,
            resource=ResourceRef(
                "core", "checkpoint_store", ResourceId(provider_id.value)
            ),
            scope=scope,
            context=context,
            constraints=constraints,
        )

    async def _invoke[ResultT](
        self, operation: Awaitable[ResultT], context: RuntimeCallContext
    ) -> ResultT:
        try:
            return await await_provider(operation, context, self._clock)
        except CoreError:
            raise
        except Exception as error:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "checkpoint_store_failure",
                "CheckpointStore operation failed",
                retryable=True,
            ) from error


def _require_exact_constraints(
    granted: Mapping[str, JsonValue], requested: Mapping[str, JsonValue]
) -> None:
    if dict(granted) != dict(requested):
        _invalid_grant("checkpoint grant changed immutable constraints")


def _constrain_list(
    query: CheckpointQuery, granted: Mapping[str, JsonValue]
) -> CheckpointQuery:
    expected_keys = {"run_id", "graph_version", "limit"}
    if set(granted) != expected_keys:
        _invalid_grant("checkpoint list grant has unknown or missing constraints")
    if granted["run_id"] != query.run_id.value:
        _invalid_grant("checkpoint list grant changed Run identity")
    if granted["graph_version"] != query.graph_version:
        _invalid_grant("checkpoint list grant changed graph version")
    limit = granted["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        _invalid_grant("checkpoint list grant limit is malformed")
    assert isinstance(limit, int)
    if limit < 1 or limit > query.limit:
        _invalid_grant("checkpoint list grant expanded limit")
    if query.cursor is not None and limit != query.limit:
        _invalid_grant("checkpoint list cursor forbids limit drift")
    return replace(query, limit=limit)


def _validate_page(page: CheckpointPage, query: CheckpointQuery) -> None:
    if len(page.items) > query.limit:
        raise core_error(
            ErrorCategory.PROTOCOL_FAILURE,
            "checkpoint_page_limit_exceeded",
            "CheckpointStore returned more items than requested",
        )
    previous_sequence: int | None = None
    for checkpoint in page.items:
        if (
            checkpoint.run_id != query.run_id
            or checkpoint.scope.key != query.scope.key
            or (
                query.graph_version is not None
                and checkpoint.graph_version != query.graph_version
            )
        ):
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_page_identity_mismatch",
                "CheckpointStore page escaped the authorized query",
            )
        if previous_sequence is not None and checkpoint.sequence >= previous_sequence:
            raise core_error(
                ErrorCategory.PROTOCOL_FAILURE,
                "checkpoint_page_order_invalid",
                "CheckpointStore page is not in descending sequence order",
            )
        previous_sequence = checkpoint.sequence
    if page.next_cursor is not None and (
        page.next_cursor.provider_id != query.provider_id
        or page.next_cursor.run_id != query.run_id
        or page.next_cursor.scope.key != query.scope.key
        or page.next_cursor.graph_version != query.graph_version
        or page.next_cursor.limit != query.limit
        or page.next_cursor.query_fingerprint != query.query_fingerprint
    ):
        raise core_error(
            ErrorCategory.PROTOCOL_FAILURE,
            "checkpoint_cursor_identity_mismatch",
            "CheckpointStore returned a cursor for a different query",
        )


def _invalid_grant(message: str) -> None:
    raise core_error(ErrorCategory.DENIED, "invalid_grant", message)
