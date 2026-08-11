from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from congeries_core.adapter import SqliteStorageProvider
from congeries_core.policy.authorization import (
    AccessRequest,
    Grant,
    PolicyDecision,
)
from congeries_core.provider import provider_actions
from congeries_core.provider.storage import (
    ARTIFACT_GET_ACTION,
    ArtifactCursor,
    ArtifactPage,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactReference,
    ArtifactValue,
    InMemoryStorageProvider,
    StorageCapabilities,
    StorageGateway,
    StorageProvider,
    StorageProviderRegistry,
    storage_actions,
)
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory
from congeries_core.runtime.ids import ArtifactId, ProviderId, WorkspaceId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.scope import ScopeRef
from congeries_core.state import WorkspaceState

from .provider_support import ProviderEventRecorder, authorized_dispatcher
from .support import NOW, FixedClock, call_context, child_scope, root_scope

PROVIDER_ID = ProviderId("storage.test")


class EchoPolicy:
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints=request.constraints,
                issued_at=NOW,
                expires_at=None,
                policy_version="storage-test-1",
                audit_correlation="storage-audit",
            )
        )


class InvalidArtifactGrantPolicy(EchoPolicy):
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        decision = await super().authorize(request)
        if request.action == ARTIFACT_GET_ACTION:
            assert decision.grant is not None
            constraints = dict(decision.grant.constraints)
            constraints["artifact_id"] = "other-artifact"
            return PolicyDecision.allow(
                replace(decision.grant, constraints=constraints)
            )
        return decision


class NarrowListPolicy(EchoPolicy):
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        decision = await super().authorize(request)
        if request.action.name == "storage.artifact.list":
            assert decision.grant is not None
            constraints = dict(decision.grant.constraints)
            constraints["limit"] = 1
            return PolicyDecision.allow(
                replace(decision.grant, constraints=constraints)
            )
        return decision


class BlockingStorageProvider(InMemoryStorageProvider):
    def __init__(self) -> None:
        super().__init__(PROVIDER_ID, max_artifact_bytes=1024)
        self.started = asyncio.Event()
        self.cleaned = False

    async def list_artifacts(
        self, query: ArtifactQuery, context: RuntimeCallContext
    ) -> ArtifactPage:
        del query, context
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned = True
        raise AssertionError("unreachable")


class CountingStorageProvider(InMemoryStorageProvider):
    def __init__(self) -> None:
        super().__init__(PROVIDER_ID, max_artifact_bytes=1024)
        self.capability_calls = 0

    async def capabilities(self, context: RuntimeCallContext) -> StorageCapabilities:
        self.capability_calls += 1
        return await super().capabilities(context)


class MismatchedStorageProvider(InMemoryStorageProvider):
    async def get_workspace(
        self,
        workspace_id: WorkspaceId,
        scope: ScopeRef,
        context: RuntimeCallContext,
    ) -> WorkspaceState:
        result = await super().get_workspace(workspace_id, scope, context)
        return replace(result, workspace_id=WorkspaceId("wrong-workspace"))


def workspace() -> WorkspaceState:
    return WorkspaceState(
        workspace_id=WorkspaceId("workspace-1"),
        scope=root_scope(),
        values={"phase": "created"},
    )


def artifact(
    artifact_id: str = "artifact-1",
    *,
    content: bytes = b"artifact content",
    seconds: int = 0,
    scope: ScopeRef | None = None,
) -> ArtifactValue:
    actual_scope = scope if scope is not None else root_scope()
    record = ArtifactRecord(
        provider_id=PROVIDER_ID,
        artifact_id=ArtifactId(artifact_id),
        workspace_id=WorkspaceId("workspace-1"),
        scope=actual_scope,
        media_type="application/octet-stream",
        byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_at=NOW + timedelta(seconds=seconds),
        name="result.bin",
        metadata={"safe": True},
    )
    return ArtifactValue(record, content)


def provider_factories(
    tmp_path: Path,
) -> tuple[Callable[[], StorageProvider], Callable[[], StorageProvider]]:
    database = tmp_path / "storage.sqlite3"
    return (
        lambda: InMemoryStorageProvider(PROVIDER_ID, max_artifact_bytes=1024),
        lambda: SqliteStorageProvider(PROVIDER_ID, database, max_artifact_bytes=1024),
    )


def gateway(
    provider: StorageProvider,
    *,
    policy: object | None = None,
    events: ProviderEventRecorder | None = None,
) -> StorageGateway:
    registry = StorageProviderRegistry()
    registry.register(PROVIDER_ID, provider)
    return StorageGateway(
        providers=registry,
        dispatcher=authorized_dispatcher(policy or EchoPolicy()),
        clock=FixedClock(),
        events=events,
    )


def assert_error(error: CoreError, category: ErrorCategory, code: str) -> None:
    assert error.detail.category is category
    assert error.detail.code == code


def test_storage_models_round_trip_and_validate_strictly() -> None:
    state = workspace()
    assert WorkspaceState.from_data(state.to_data()) == state

    value = artifact()
    assert ArtifactRecord.from_data(value.record.to_data()) == value.record
    assert ArtifactValue.from_data(value.to_data()) == value
    reference = ArtifactReference.from_record(value.record)
    assert ArtifactReference.from_data(reference.to_data()) == reference

    query = ArtifactQuery(PROVIDER_ID, state.workspace_id, state.scope, limit=2)
    cursor = ArtifactCursor(
        provider_id=PROVIDER_ID,
        workspace_id=state.workspace_id,
        scope=state.scope,
        limit=2,
        query_fingerprint=query.query_fingerprint,
        before_created_at=NOW,
        before_artifact_id=value.record.artifact_id,
    )
    query_with_cursor = replace(query, cursor=cursor)
    page = ArtifactPage((value.record,), cursor)
    assert ArtifactQuery.from_data(query_with_cursor.to_data()) == query_with_cursor
    assert ArtifactPage.from_data(page.to_data()) == page

    capabilities = StorageCapabilities(PROVIDER_ID, 1024)
    assert StorageCapabilities.from_data(capabilities.to_data()) == capabilities
    assert tuple(action.name for action in storage_actions()) == (
        "storage.capabilities",
        "storage.workspace.create",
        "storage.workspace.get",
        "storage.workspace.compare_and_set",
        "storage.artifact.put",
        "storage.artifact.get",
        "storage.artifact.list",
    )
    assert all(action.version == "1" for action in storage_actions())
    assert all(action in provider_actions() for action in storage_actions())

    invalid = value.to_data()
    invalid["content_base64"] = "***"
    with pytest.raises(ValueError, match="base64"):
        ArtifactValue.from_data(invalid)

    noncanonical = value.to_data()
    noncanonical["content_base64"] = "YXJ0aWZhY3QgY29udGVudB=="
    with pytest.raises(ValueError, match="canonical"):
        ArtifactValue.from_data(noncanonical)

    invalid_record = value.record.to_data()
    invalid_record["contract_version"] = "2"
    with pytest.raises(ValueError, match="unsupported"):
        ArtifactRecord.from_data(invalid_record)

    with pytest.raises(ValueError, match="digest"):
        ArtifactValue(value.record, b"ARTIFACT CONTENT")
    with pytest.raises(ValueError, match="length"):
        ArtifactValue(value.record, b"short")


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_index", [0, 1])
async def test_two_storage_providers_pass_workspace_and_artifact_contract(
    tmp_path: Path, factory_index: int
) -> None:
    provider = provider_factories(tmp_path)[factory_index]()
    context = call_context()
    state = workspace()

    assert await provider.capabilities(context) == StorageCapabilities(
        PROVIDER_ID, 1024
    )
    assert await provider.create_workspace(state, context) == state
    assert (
        await provider.get_workspace(state.workspace_id, state.scope, context) == state
    )

    updated = state.update(0, {"phase": "running"})
    assert await provider.compare_and_set_workspace(updated, 0, context) == updated
    with pytest.raises(CoreError) as stale:
        await provider.compare_and_set_workspace(updated, 0, context)
    assert_error(stale.value, ErrorCategory.CONFLICT, "stale_state_version")

    values = (
        artifact("artifact-1", seconds=1),
        artifact("artifact-2", seconds=2),
        artifact("artifact-3", seconds=3),
    )
    for value in values:
        assert await provider.put_artifact(value, context) == value.record
        assert await provider.put_artifact(value, context) == value.record
    assert (
        await provider.get_artifact(
            ArtifactId("artifact-2"), state.workspace_id, state.scope, context
        )
        == values[1]
    )

    first = await provider.list_artifacts(
        ArtifactQuery(PROVIDER_ID, state.workspace_id, state.scope, limit=2), context
    )
    assert [item.artifact_id.value for item in first.items] == [
        "artifact-3",
        "artifact-2",
    ]
    assert first.next_cursor is not None
    second = await provider.list_artifacts(
        ArtifactQuery(
            PROVIDER_ID,
            state.workspace_id,
            state.scope,
            limit=2,
            cursor=first.next_cursor,
        ),
        context,
    )
    assert [item.artifact_id.value for item in second.items] == ["artifact-1"]
    assert second.next_cursor is None

    assert first.next_cursor is not None
    drifted = replace(first.next_cursor, limit=1)
    with pytest.raises(CoreError) as drift:
        ArtifactQuery(
            PROVIDER_ID,
            state.workspace_id,
            state.scope,
            limit=2,
            cursor=drifted,
        )
    assert_error(drift.value, ErrorCategory.CONFLICT, "artifact_cursor_drift")

    conflicting = artifact("artifact-1", content=b"different", seconds=1)
    with pytest.raises(CoreError) as conflict:
        await provider.put_artifact(conflicting, context)
    assert_error(conflict.value, ErrorCategory.CONFLICT, "artifact_identity_conflict")

    narrowed_scope = child_scope()
    narrowed_value = artifact("artifact-4", scope=narrowed_scope)
    await provider.put_artifact(narrowed_value, context)
    narrowed_page = await provider.list_artifacts(
        ArtifactQuery(PROVIDER_ID, state.workspace_id, narrowed_scope), context
    )
    assert narrowed_page.items == (narrowed_value.record,)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_index", [0, 1])
async def test_storage_compare_and_set_has_one_concurrent_winner(
    tmp_path: Path, factory_index: int
) -> None:
    provider = provider_factories(tmp_path)[factory_index]()
    context = call_context()
    state = workspace()
    await provider.create_workspace(state, context)
    candidates = (
        state.update(0, {"winner": "a"}),
        state.update(0, {"winner": "b"}),
    )
    results = await asyncio.gather(
        *(provider.compare_and_set_workspace(item, 0, context) for item in candidates),
        return_exceptions=True,
    )
    assert sum(isinstance(item, WorkspaceState) for item in results) == 1
    errors = [item for item in results if isinstance(item, CoreError)]
    assert len(errors) == 1
    assert_error(errors[0], ErrorCategory.CONFLICT, "stale_state_version")


@pytest.mark.asyncio
async def test_sqlite_storage_persists_across_provider_restart_and_rolls_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.sqlite3"
    context = call_context()
    initial = SqliteStorageProvider(PROVIDER_ID, path, max_artifact_bytes=1024)

    missing_workspace_value = artifact("artifact-orphan")
    with pytest.raises(CoreError) as missing:
        await initial.put_artifact(missing_workspace_value, context)
    assert_error(missing.value, ErrorCategory.INVALID_REQUEST, "workspace_not_found")

    state = workspace()
    value = artifact()
    await initial.create_workspace(state, context)
    with pytest.raises(CoreError) as rolled_back:
        await initial.get_artifact(
            missing_workspace_value.record.artifact_id,
            state.workspace_id,
            state.scope,
            context,
        )
    assert_error(rolled_back.value, ErrorCategory.INVALID_REQUEST, "artifact_not_found")
    await initial.put_artifact(value, context)

    reopened = SqliteStorageProvider(PROVIDER_ID, path, max_artifact_bytes=1024)
    assert (
        await reopened.get_workspace(state.workspace_id, state.scope, context) == state
    )
    assert (
        await reopened.get_artifact(
            value.record.artifact_id, state.workspace_id, state.scope, context
        )
        == value
    )
    page = await reopened.list_artifacts(
        ArtifactQuery(PROVIDER_ID, state.workspace_id, state.scope), context
    )
    assert page.items == (value.record,)


@pytest.mark.asyncio
async def test_storage_gateway_authorizes_narrows_and_emits_redacted_events() -> None:
    provider = InMemoryStorageProvider(PROVIDER_ID, max_artifact_bytes=1024)
    events = ProviderEventRecorder()
    storage = gateway(provider, events=events)
    context = call_context()
    state = workspace()
    value = artifact()

    assert await storage.capabilities(PROVIDER_ID, context) == StorageCapabilities(
        PROVIDER_ID, 1024
    )
    await storage.create_workspace(PROVIDER_ID, state, context)
    updated = state.update(0, {"phase": "running"})
    await storage.compare_and_set_workspace(PROVIDER_ID, updated, 0, context)
    await storage.put_artifact(value, context)
    assert (
        await storage.get_artifact(
            PROVIDER_ID,
            value.record.artifact_id,
            state.workspace_id,
            state.scope,
            context,
        )
        == value
    )
    page = await storage.list_artifacts(
        ArtifactQuery(PROVIDER_ID, state.workspace_id, state.scope), context
    )
    assert page.items == (value.record,)

    payloads: list[Mapping[str, JsonValue]] = [payload for _, payload in events.events]
    assert payloads
    assert all(
        "content" not in payload and "metadata" not in payload for payload in payloads
    )
    assert {name for name, _ in events.events} == {
        "core.storage.operation_started",
        "core.storage.operation_completed",
    }

    narrowed = gateway(provider, policy=NarrowListPolicy())
    narrowed_page = await narrowed.list_artifacts(
        ArtifactQuery(PROVIDER_ID, state.workspace_id, state.scope, limit=10), context
    )
    assert len(narrowed_page.items) == 1
    assert narrowed_page.next_cursor is None


@pytest.mark.asyncio
async def test_storage_gateway_rejects_invalid_grant_before_provider_read() -> None:
    provider = InMemoryStorageProvider(PROVIDER_ID, max_artifact_bytes=1024)
    context = call_context()
    state = workspace()
    value = artifact()
    allowed = gateway(provider)
    await allowed.create_workspace(PROVIDER_ID, state, context)
    await allowed.put_artifact(value, context)

    invalid = gateway(provider, policy=InvalidArtifactGrantPolicy())
    with pytest.raises(CoreError) as denied:
        await invalid.get_artifact(
            PROVIDER_ID,
            value.record.artifact_id,
            state.workspace_id,
            state.scope,
            context,
        )
    assert_error(denied.value, ErrorCategory.DENIED, "invalid_grant")


@pytest.mark.asyncio
async def test_storage_default_denial_unknown_action_and_pre_cancel_have_no_call() -> (
    None
):
    provider = CountingStorageProvider()
    registry = StorageProviderRegistry()
    registry.register(PROVIDER_ID, provider)

    denied = StorageGateway(
        providers=registry,
        dispatcher=authorized_dispatcher(None),
        clock=FixedClock(),
    )
    with pytest.raises(CoreError) as missing_policy:
        await denied.capabilities(PROVIDER_ID, call_context())
    assert missing_policy.value.detail.category is ErrorCategory.DENIED
    assert provider.capability_calls == 0

    unknown = StorageGateway(
        providers=registry,
        dispatcher=authorized_dispatcher(EchoPolicy(), known_actions=False),
        clock=FixedClock(),
    )
    with pytest.raises(CoreError) as unknown_action:
        await unknown.capabilities(PROVIDER_ID, call_context())
    assert_error(unknown_action.value, ErrorCategory.DENIED, "unknown_action")
    assert provider.capability_calls == 0

    pre_cancelled = call_context()
    pre_cancelled.cancellation.cancel()
    with pytest.raises(CoreError) as cancelled:
        await gateway(provider).capabilities(PROVIDER_ID, pre_cancelled)
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert provider.capability_calls == 0


@pytest.mark.asyncio
async def test_storage_running_cancellation_deadline_and_protocol_failure() -> None:
    provider = BlockingStorageProvider()
    context = call_context()
    query = ArtifactQuery(PROVIDER_ID, context.workspace_id, context.scope)
    task = asyncio.create_task(gateway(provider).list_artifacts(query, context))
    await provider.started.wait()
    context.cancellation.cancel()
    with pytest.raises(CoreError) as cancelled:
        await task
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert provider.cleaned

    deadline_provider = BlockingStorageProvider()
    deadline_context = replace(
        call_context(), deadline=Deadline(NOW + timedelta(milliseconds=1))
    )
    with pytest.raises(CoreError) as timed_out:
        await gateway(deadline_provider).list_artifacts(query, deadline_context)
    assert timed_out.value.detail.category is ErrorCategory.TIMEOUT
    assert deadline_provider.cleaned

    mismatched = MismatchedStorageProvider(PROVIDER_ID)
    state = workspace()
    await mismatched.create_workspace(state, call_context())
    with pytest.raises(CoreError) as protocol:
        await gateway(mismatched).get_workspace(
            PROVIDER_ID, state.workspace_id, state.scope, call_context()
        )
    assert_error(
        protocol.value, ErrorCategory.PROTOCOL_FAILURE, "storage_protocol_failure"
    )


def test_storage_provider_registry_conflicts_and_missing_provider() -> None:
    registry = StorageProviderRegistry()
    provider = InMemoryStorageProvider(PROVIDER_ID)
    registry.register(PROVIDER_ID, provider)
    assert registry.get(PROVIDER_ID) is provider
    with pytest.raises(CoreError) as duplicate:
        registry.register(PROVIDER_ID, provider)
    assert_error(
        duplicate.value,
        ErrorCategory.CONFLICT,
        "storage_provider_already_registered",
    )
    with pytest.raises(CoreError) as missing:
        registry.get(ProviderId("missing"))
    assert_error(
        missing.value,
        ErrorCategory.UNAVAILABLE,
        "storage_provider_not_registered",
    )
