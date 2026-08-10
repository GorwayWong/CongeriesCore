from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import cast

import pytest

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    Grant,
    PolicyDecision,
    ResourceRef,
)
from congeries_core.provider.memory import (
    MEMORY_CAPABILITIES_ACTION,
    MEMORY_CONSOLIDATE_ACTION,
    MEMORY_FORGET_ACTION,
    MEMORY_OPERATION_COMPLETED,
    MEMORY_OPERATION_FAILED,
    MEMORY_OPERATION_STARTED,
    MEMORY_REMEMBER_ACTION,
    MEMORY_RETRIEVE_ACTION,
    ConsolidateRequest,
    ConsolidationOutcome,
    ConsolidationReport,
    ForgetOutcome,
    ForgetRequest,
    ForgetResult,
    MemoryCapabilities,
    MemoryCompleteness,
    MemoryCursor,
    MemoryGateway,
    MemoryItem,
    MemoryOperation,
    MemoryPage,
    MemoryProvider,
    MemoryProviderRegistry,
    MemoryQuery,
    MemoryRecord,
    MemoryRef,
    MemoryWarning,
    memory_actions,
)
from congeries_core.runtime.content import ContentBlock, ContentKind
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Deadline
from congeries_core.runtime.errors import CoreError, ErrorCategory, core_error
from congeries_core.runtime.ids import IdempotencyKey, MemoryId, ProviderId, ResourceId
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.schema import SchemaRef, SchemaRegistry
from congeries_core.runtime.scope import ScopeRef

from .provider_support import (
    AuditRecorder,
    ProviderEventRecorder,
    RecordingPolicy,
    StringObjectValidator,
    authorized_dispatcher,
)
from .support import NOW, FixedClock, call_context, child_scope, root_scope

QUERY_SCHEMA = SchemaRef("test", "memory-query", "1")
ITEM_SCHEMA = SchemaRef("test", "memory-item", "1")
POLICY = ResourceRef("test", "memory_policy", ResourceId("compact"))
PROVIDER = ProviderId("memory-a")


def query(
    *, scope: ScopeRef | None = None, cursor: MemoryCursor | None = None
) -> MemoryQuery:
    return MemoryQuery(
        scope=scope or root_scope(),
        content=ContentBlock.json({"value": "find"}),
        schema=QUERY_SCHEMA,
        filters={"test.kind": "note"},
        limit=10,
        cursor=cursor,
        projection=("core.content", "core.metadata"),
    )


def memory_ref(
    provider_id: ProviderId = PROVIDER,
    *,
    scope: ScopeRef | None = None,
    version: int = 1,
) -> MemoryRef:
    return MemoryRef(provider_id, MemoryId("memory-1"), scope or root_scope(), version)


def record(
    provider_id: ProviderId = PROVIDER, *, scope: ScopeRef | None = None
) -> MemoryRecord:
    return MemoryRecord(
        memory_ref(provider_id, scope=scope),
        ContentBlock.json({"value": "remembered"}),
        ITEM_SCHEMA,
        {"test.source": "fake"},
        ("fake-memory",),
    )


def page(
    actual_query: MemoryQuery,
    provider_id: ProviderId = PROVIDER,
    *,
    partial: bool = False,
    with_cursor: bool = False,
) -> MemoryPage:
    cursor = (
        MemoryCursor(provider_id, "1", "page-2", actual_query.query_fingerprint)
        if with_cursor
        else None
    )
    return MemoryPage(
        provider_id=provider_id,
        contract_version="1",
        records=(record(provider_id, scope=actual_query.scope),),
        next_cursor=cursor,
        completeness=(
            MemoryCompleteness.PARTIAL if partial else MemoryCompleteness.COMPLETE
        ),
        warnings=(MemoryWarning("fake_warning", "fake warning"),) if partial else (),
        provenance=("fake-provider",),
    )


def item(*, scope: ScopeRef | None = None, value: str = "remembered") -> MemoryItem:
    return MemoryItem(
        scope=scope or root_scope(),
        content=ContentBlock.json({"value": value}),
        schema=ITEM_SCHEMA,
        idempotency_key=IdempotencyKey("idempotency-1"),
        metadata={"test.source": "input", "test.private": True},
        provenance=("caller",),
        retention=ResourceRef("test", "retention", ResourceId("standard")),
        max_bytes=1_000,
    )


def capabilities(
    provider_id: ProviderId = PROVIDER,
    *,
    consolidate: bool = True,
) -> MemoryCapabilities:
    operations = {
        MemoryOperation.RETRIEVE,
        MemoryOperation.REMEMBER,
        MemoryOperation.FORGET,
    }
    if consolidate:
        operations.add(MemoryOperation.CONSOLIDATE)
    return MemoryCapabilities(
        provider_id=provider_id,
        contract_version="1",
        operations=frozenset(operations),
        query_schemas=frozenset({QUERY_SCHEMA}),
        item_schemas=frozenset({ITEM_SCHEMA}),
        record_schemas=frozenset({ITEM_SCHEMA}),
        content_kinds=frozenset({ContentKind.JSON}),
        maximum_result_limit=100,
        projections=frozenset({"core.content", "core.metadata"}),
        versioned=True,
        consolidation_policies=(POLICY,) if consolidate else (),
        maximum_item_bytes=2_000,
    )


def report(
    provider_id: ProviderId = PROVIDER,
    *,
    scope: ScopeRef | None = None,
    partial: bool = False,
) -> ConsolidationReport:
    actual_scope = scope or root_scope()
    return ConsolidationReport(
        provider_id=provider_id,
        contract_version="1",
        policy=POLICY,
        scope=actual_scope,
        affected=(memory_ref(provider_id, scope=actual_scope),),
        skipped=(MemoryRef(provider_id, MemoryId("memory-2"), actual_scope, 1),)
        if partial
        else (),
        outcome=(
            ConsolidationOutcome.PARTIAL if partial else ConsolidationOutcome.COMPLETE
        ),
        warnings=(MemoryWarning("partial", "some records skipped"),) if partial else (),
    )


@dataclass(slots=True)
class FakeMemoryProvider(MemoryProvider):
    declared_capabilities: MemoryCapabilities
    page_result: MemoryPage | Exception
    consolidation_result: ConsolidationReport | Exception
    capability_calls: int = 0
    retrieve_calls: list[MemoryQuery] = field(default_factory=lambda: [])
    remember_calls: list[MemoryItem] = field(default_factory=lambda: [])
    forget_calls: list[ForgetRequest] = field(default_factory=lambda: [])
    consolidate_calls: list[ConsolidateRequest] = field(default_factory=lambda: [])
    remembered: dict[tuple[tuple[str, str, str], str], tuple[object, MemoryRef]] = (
        field(default_factory=lambda: {})
    )
    absent: set[MemoryId] = field(default_factory=lambda: set())

    async def capabilities(self, context: RuntimeCallContext) -> MemoryCapabilities:
        del context
        self.capability_calls += 1
        return self.declared_capabilities

    async def retrieve(
        self, query: MemoryQuery, context: RuntimeCallContext
    ) -> MemoryPage:
        del context
        self.retrieve_calls.append(query)
        if isinstance(self.page_result, Exception):
            raise self.page_result
        return self.page_result

    async def remember(
        self, item: MemoryItem, context: RuntimeCallContext
    ) -> MemoryRef:
        del context
        self.remember_calls.append(item)
        key = item.scope.key, item.idempotency_key.value
        payload = item.to_data()
        existing = self.remembered.get(key)
        if existing is not None:
            if existing[0] != payload:
                raise core_error(
                    ErrorCategory.CONFLICT,
                    "memory_idempotency_conflict",
                    "idempotency key was reused with another payload",
                )
            return existing[1]
        ref = memory_ref(self.declared_capabilities.provider_id, scope=item.scope)
        self.remembered[key] = payload, ref
        return ref

    async def forget(
        self, request: ForgetRequest, context: RuntimeCallContext
    ) -> ForgetResult:
        del context
        self.forget_calls.append(request)
        if request.ref.memory_id in self.absent:
            return ForgetResult(request.ref, ForgetOutcome.ALREADY_ABSENT)
        self.absent.add(request.ref.memory_id)
        return ForgetResult(request.ref, ForgetOutcome.DELETED)

    async def consolidate(
        self, request: ConsolidateRequest, context: RuntimeCallContext
    ) -> ConsolidationReport:
        del context
        self.consolidate_calls.append(request)
        if isinstance(self.consolidation_result, Exception):
            raise self.consolidation_result
        return self.consolidation_result


class AlternateFakeMemoryProvider(FakeMemoryProvider):
    async def retrieve(
        self, query: MemoryQuery, context: RuntimeCallContext
    ) -> MemoryPage:
        self.retrieve_calls.append(query)
        context.cancellation.raise_if_cancelled()
        if isinstance(self.page_result, Exception):
            raise self.page_result
        return self.page_result


@dataclass(slots=True)
class BlockingMemoryProvider(FakeMemoryProvider):
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: bool = False

    async def retrieve(
        self, query: MemoryQuery, context: RuntimeCallContext
    ) -> MemoryPage:
        del context
        self.retrieve_calls.append(query)
        self.started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("blocking Provider wait unexpectedly completed")
        except asyncio.CancelledError:
            return cast(MemoryPage, self.page_result)
        finally:
            self.cleaned = True


@dataclass(slots=True)
class ActionConstraintPolicy:
    constraints: dict[str, Mapping[str, JsonValue]] = field(default_factory=lambda: {})
    denied_actions: set[str] = field(default_factory=lambda: set())
    requests: list[AccessRequest] = field(default_factory=lambda: [])

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        if request.action.name in self.denied_actions:
            return PolicyDecision.deny("test_denied")
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints=self.constraints.get(request.action.name, {}),
                issued_at=NOW,
                expires_at=None,
                policy_version="memory-test-1",
                audit_correlation="memory-audit",
            )
        )


def registry(
    provider_id: ProviderId, provider: MemoryProvider
) -> MemoryProviderRegistry:
    providers = MemoryProviderRegistry()
    providers.register(provider_id, provider)
    return providers


def schemas() -> SchemaRegistry:
    result = SchemaRegistry()
    result.register(QUERY_SCHEMA, StringObjectValidator())
    result.register(ITEM_SCHEMA, StringObjectValidator())
    return result


def gateway(
    provider_id: ProviderId,
    provider: MemoryProvider,
    *,
    policy: RecordingPolicy | ActionConstraintPolicy | None = None,
    audit: AuditRecorder | None = None,
    events: ProviderEventRecorder | None = None,
    clock: FixedClock | None = None,
    known_actions: bool = True,
    default_deny: bool = False,
) -> MemoryGateway:
    return MemoryGateway(
        providers=registry(provider_id, provider),
        schemas=schemas(),
        dispatcher=authorized_dispatcher(
            None if default_deny else (policy or RecordingPolicy()),
            audit=audit,
            known_actions=known_actions,
        ),
        clock=clock or FixedClock(),
        events=events,
    )


def fake_provider(
    actual_query: MemoryQuery,
    provider_id: ProviderId = PROVIDER,
    *,
    provider_type: type[FakeMemoryProvider] = FakeMemoryProvider,
    consolidate: bool = True,
) -> FakeMemoryProvider:
    return provider_type(
        capabilities(provider_id, consolidate=consolidate),
        page(actual_query, provider_id),
        report(provider_id, scope=actual_query.scope),
    )


def test_memory_value_models_round_trip_and_validation() -> None:
    actual_query = query()
    cursor = MemoryCursor(PROVIDER, "1", "next", actual_query.query_fingerprint)
    paged_query = replace(actual_query, cursor=cursor)
    actual_ref = memory_ref()
    actual_record = record()
    actual_page = page(actual_query, partial=True, with_cursor=True)
    actual_item = item()
    forget = ForgetRequest(actual_ref, actual_ref.scope, actual_ref.version)
    forget_result = ForgetResult(actual_ref, ForgetOutcome.ALREADY_ABSENT)
    consolidate = ConsolidateRequest(root_scope(), POLICY, {"test.partition": "recent"})
    actual_report = report(partial=True)
    actual_capabilities = capabilities()

    warning = MemoryWarning("warning", "message")
    assert MemoryCursor.from_data(cast(dict[str, object], cursor.to_data())) == cursor
    assert MemoryQuery.from_data(paged_query.to_data()) == paged_query
    assert MemoryRef.from_data(actual_ref.to_data()) == actual_ref
    assert MemoryRecord.from_data(actual_record.to_data()) == actual_record
    assert MemoryPage.from_data(actual_page.to_data()) == actual_page
    assert MemoryItem.from_data(actual_item.to_data()) == actual_item
    assert ForgetRequest.from_data(forget.to_data()) == forget
    assert ForgetResult.from_data(forget_result.to_data()) == forget_result
    assert ConsolidateRequest.from_data(consolidate.to_data()) == consolidate
    assert ConsolidationReport.from_data(actual_report.to_data()) == actual_report
    assert (
        MemoryCapabilities.from_data(actual_capabilities.to_data())
        == actual_capabilities
    )
    assert (
        MemoryWarning.from_data(cast(dict[str, object], warning.to_data())) == warning
    )

    assert not actual_report.success
    assert report().success
    assert paged_query.query_fingerprint == actual_query.query_fingerprint
    assert actual_item.content_bytes > 0
    assert {action.name for action in memory_actions()} == {
        "memory.capabilities",
        "memory.retrieve",
        "memory.remember",
        "memory.forget",
        "memory.consolidate",
    }
    assert all(action.version == "1" for action in memory_actions())
    assert all(action.name != "store" for action in memory_actions())

    with pytest.raises(ValueError):
        MemoryQuery(root_scope(), ContentBlock.text("x"), QUERY_SCHEMA, {"plain": 1})
    with pytest.raises(ValueError):
        replace(actual_query, limit=0)
    with pytest.raises(ValueError):
        replace(actual_query, projection=("same", "same"))
    with pytest.raises(ValueError):
        MemoryCursor(PROVIDER, "1", "next", "bad")
    with pytest.raises(ValueError):
        replace(actual_ref, version=0)
    with pytest.raises(ValueError):
        ForgetRequest(actual_ref, child_scope(), 1)
    with pytest.raises(ValueError):
        ForgetRequest(actual_ref, actual_ref.scope, 2)
    with pytest.raises(ValueError):
        replace(actual_item, max_bytes=1)
    with pytest.raises(ValueError):
        MemoryPage(PROVIDER, "1", (actual_record, actual_record))
    with pytest.raises(ValueError):
        MemoryCapabilities(
            PROVIDER,
            "1",
            frozenset({MemoryOperation.RETRIEVE}),
            frozenset({QUERY_SCHEMA}),
            frozenset({ITEM_SCHEMA}),
            frozenset({ITEM_SCHEMA}),
            frozenset({ContentKind.JSON}),
            1,
            frozenset(),
            True,
            (POLICY,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_type", [FakeMemoryProvider, AlternateFakeMemoryProvider]
)
async def test_two_memory_providers_pass_complete_contract(
    provider_type: type[FakeMemoryProvider],
) -> None:
    provider_id = ProviderId(provider_type.__name__.lower())
    actual_query = query()
    provider = fake_provider(actual_query, provider_id, provider_type=provider_type)
    events = ProviderEventRecorder()
    memory = gateway(provider_id, provider, events=events)
    context = call_context()

    discovered = await memory.capabilities(provider_id, context)
    retrieved = await memory.retrieve(provider_id, actual_query, context)
    remembered = await memory.remember(provider_id, item(), context)
    forgotten = await memory.forget(
        provider_id,
        ForgetRequest(remembered, remembered.scope, remembered.version),
        context,
    )
    forgotten_again = await memory.forget(
        provider_id,
        ForgetRequest(remembered, remembered.scope, remembered.version),
        context,
    )
    consolidated = await memory.consolidate(
        provider_id,
        ConsolidateRequest(root_scope(), POLICY, {"test.partition": "all"}),
        context,
    )

    assert discovered.provider_id == provider_id
    assert retrieved.records[0].content.value == {"value": "remembered"}
    assert forgotten.outcome is ForgetOutcome.DELETED
    assert forgotten_again.outcome is ForgetOutcome.ALREADY_ABSENT
    assert consolidated.success
    assert provider.capability_calls == 6
    assert len(provider.retrieve_calls) == 1
    assert len(provider.remember_calls) == 1
    assert len(provider.forget_calls) == 2
    assert len(provider.consolidate_calls) == 1

    event_types = [event_type for event_type, _ in events.events]
    assert event_types.count(MEMORY_OPERATION_STARTED) == 5
    assert event_types.count(MEMORY_OPERATION_COMPLETED) == 5
    assert MEMORY_OPERATION_FAILED not in event_types
    serialized_events = repr(events.events)
    assert "find" not in serialized_events
    assert "test.source" not in serialized_events
    assert "page-2" not in serialized_events


@pytest.mark.asyncio
async def test_remember_idempotency_and_payload_conflict() -> None:
    actual_query = query()
    provider = fake_provider(actual_query)
    memory = gateway(PROVIDER, provider)
    context = call_context()
    first = await memory.remember(PROVIDER, item(), context)
    second = await memory.remember(PROVIDER, item(), context)
    assert first == second

    with pytest.raises(CoreError) as conflict:
        await memory.remember(PROVIDER, item(value="different"), context)
    assert conflict.value.detail.category is ErrorCategory.CONFLICT

    with pytest.raises(CoreError) as mismatch:
        await memory.remember(
            PROVIDER,
            item(),
            replace(context, idempotency_key=IdempotencyKey("another-key")),
        )
    assert mismatch.value.detail.code == "memory_idempotency_key_mismatch"


@pytest.mark.asyncio
async def test_grants_narrow_retrieve_and_remember_without_broadening() -> None:
    actual_query = query()
    provider = fake_provider(actual_query)
    policy = ActionConstraintPolicy(
        constraints={
            MEMORY_RETRIEVE_ACTION.name: {
                "schema": {
                    "namespace": "test",
                    "name": "memory-query",
                    "version": "1",
                },
                "filter_keys": ["test.kind"],
                "projection": ["core.content"],
                "limit": 5,
            },
            MEMORY_REMEMBER_ACTION.name: {
                "schema": {
                    "namespace": "test",
                    "name": "memory-item",
                    "version": "1",
                },
                "metadata_keys": ["test.source"],
                "max_bytes": 500,
            },
        }
    )
    memory = gateway(PROVIDER, provider, policy=policy)

    await memory.retrieve(PROVIDER, actual_query, call_context())
    await memory.remember(PROVIDER, item(), call_context())

    assert provider.retrieve_calls[0].projection == ("core.content",)
    assert provider.retrieve_calls[0].limit == 5
    assert set(provider.remember_calls[0].metadata) == {"test.source"}
    assert provider.remember_calls[0].max_bytes == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "constraints", "invoke", "counter"),
    [
        (
            MEMORY_RETRIEVE_ACTION,
            {"projection": ["core.secret"]},
            "retrieve",
            "retrieve_calls",
        ),
        (
            MEMORY_REMEMBER_ACTION,
            {"metadata_keys": ["test.new"]},
            "remember",
            "remember_calls",
        ),
        (
            MEMORY_FORGET_ACTION,
            {"memory_id": "another"},
            "forget",
            "forget_calls",
        ),
        (
            MEMORY_CONSOLIDATE_ACTION,
            {"selection_keys": ["test.changed"]},
            "consolidate",
            "consolidate_calls",
        ),
    ],
)
async def test_invalid_grants_do_not_invoke_business_operation(
    action: ActionRef,
    constraints: dict[str, JsonValue],
    invoke: str,
    counter: str,
) -> None:
    actual_query = query()
    provider = fake_provider(actual_query)
    memory = gateway(
        PROVIDER,
        provider,
        policy=ActionConstraintPolicy(constraints={action.name: constraints}),
    )
    with pytest.raises(CoreError) as rejected:
        if invoke == "retrieve":
            await memory.retrieve(PROVIDER, actual_query, call_context())
        elif invoke == "remember":
            await memory.remember(PROVIDER, item(), call_context())
        elif invoke == "forget":
            ref = memory_ref()
            await memory.forget(
                PROVIDER, ForgetRequest(ref, ref.scope, ref.version), call_context()
            )
        else:
            await memory.consolidate(
                PROVIDER,
                ConsolidateRequest(root_scope(), POLICY, {"test.partition": "all"}),
                call_context(),
            )
    assert rejected.value.detail.code == "invalid_grant"
    assert len(cast(list[object], getattr(provider, counter))) == 0


@pytest.mark.asyncio
async def test_default_deny_unknown_action_and_action_deny_have_no_bypass() -> None:
    actual_query = query()
    denied_provider = fake_provider(actual_query)
    with pytest.raises(CoreError) as denied:
        await gateway(PROVIDER, denied_provider, default_deny=True).capabilities(
            PROVIDER, call_context()
        )
    assert denied.value.detail.category is ErrorCategory.DENIED
    assert denied_provider.capability_calls == 0

    unknown_provider = fake_provider(actual_query)
    with pytest.raises(CoreError) as unknown:
        await gateway(
            PROVIDER,
            unknown_provider,
            policy=RecordingPolicy(),
            known_actions=False,
        ).capabilities(PROVIDER, call_context())
    assert unknown.value.detail.code == "unknown_action"
    assert unknown_provider.capability_calls == 0

    operation_provider = fake_provider(actual_query)
    policy = ActionConstraintPolicy(denied_actions={MEMORY_RETRIEVE_ACTION.name})
    with pytest.raises(CoreError):
        await gateway(PROVIDER, operation_provider, policy=policy).retrieve(
            PROVIDER, actual_query, call_context()
        )
    assert operation_provider.capability_calls == 1
    assert not operation_provider.retrieve_calls


@pytest.mark.asyncio
async def test_narrow_scope_uses_actual_scope_and_emits_cross_scope_audit() -> None:
    parent = root_scope()
    narrower = child_scope(parent)
    actual_query = query(scope=narrower)
    provider = fake_provider(actual_query)
    audit = AuditRecorder()
    policy = ActionConstraintPolicy()
    await gateway(PROVIDER, provider, policy=policy, audit=audit).retrieve(
        PROVIDER, actual_query, call_context(scope=parent)
    )
    assert [request.action.name for request in audit.cross_scope] == [
        MEMORY_CAPABILITIES_ACTION.name,
        MEMORY_RETRIEVE_ACTION.name,
    ]
    assert all(request.scope.key == narrower.key for request in audit.cross_scope)


@pytest.mark.asyncio
async def test_cursor_drift_and_contract_version_are_rejected() -> None:
    first = query()
    valid_cursor = MemoryCursor(PROVIDER, "1", "next", first.query_fingerprint)
    provider = fake_provider(first)
    memory = gateway(PROVIDER, provider)

    drifted = replace(first, limit=9, cursor=valid_cursor)
    with pytest.raises(CoreError) as drift:
        await memory.retrieve(PROVIDER, drifted, call_context())
    assert drift.value.detail.code == "memory_cursor_query_drift"
    assert provider.capability_calls == 1
    assert not provider.retrieve_calls

    wrong_provider = replace(
        first,
        cursor=MemoryCursor(ProviderId("other"), "1", "next", first.query_fingerprint),
    )
    with pytest.raises(CoreError) as identity:
        await memory.retrieve(PROVIDER, wrong_provider, call_context())
    assert identity.value.detail.code == "memory_cursor_provider_mismatch"

    wrong_version = replace(
        first, cursor=MemoryCursor(PROVIDER, "2", "next", first.query_fingerprint)
    )
    with pytest.raises(CoreError) as version:
        await memory.retrieve(PROVIDER, wrong_version, call_context())
    assert version.value.detail.category is ErrorCategory.VERSION_MISMATCH
    assert not provider.retrieve_calls


@pytest.mark.asyncio
async def test_cursor_fingerprint_tracks_the_grant_constrained_query() -> None:
    original = query()
    constrained = replace(original, projection=("core.content",), limit=5)
    provider = FakeMemoryProvider(
        capabilities(), page(constrained, with_cursor=True), report()
    )
    policy = ActionConstraintPolicy(
        constraints={
            MEMORY_RETRIEVE_ACTION.name: {
                "projection": ["core.content"],
                "limit": 5,
            }
        }
    )
    memory = gateway(PROVIDER, provider, policy=policy)
    first = await memory.retrieve(PROVIDER, original, call_context())
    assert first.next_cursor is not None
    assert first.next_cursor.query_fingerprint == constrained.query_fingerprint

    provider.page_result = replace(first, next_cursor=None)
    await memory.retrieve(
        PROVIDER,
        replace(original, cursor=first.next_cursor),
        call_context(),
    )
    assert provider.retrieve_calls[-1].projection == ("core.content",)
    assert provider.retrieve_calls[-1].limit == 5


@pytest.mark.asyncio
async def test_partial_results_optional_capability_and_provider_failure() -> None:
    actual_query = query()
    partial_provider = FakeMemoryProvider(
        capabilities(),
        page(actual_query, partial=True),
        report(partial=True),
    )
    memory = gateway(PROVIDER, partial_provider)
    retrieved = await memory.retrieve(PROVIDER, actual_query, call_context())
    consolidated = await memory.consolidate(
        PROVIDER,
        ConsolidateRequest(root_scope(), POLICY, {"test.partition": "all"}),
        call_context(),
    )
    assert retrieved.completeness is MemoryCompleteness.PARTIAL
    assert consolidated.outcome is ConsolidationOutcome.PARTIAL
    assert not consolidated.success
    assert consolidated.affected and consolidated.skipped

    unsupported = fake_provider(actual_query, consolidate=False)
    with pytest.raises(CoreError) as unsupported_error:
        await gateway(PROVIDER, unsupported).consolidate(
            PROVIDER,
            ConsolidateRequest(root_scope(), POLICY),
            call_context(),
        )
    assert (
        unsupported_error.value.detail.category is ErrorCategory.UNSUPPORTED_CAPABILITY
    )
    assert not unsupported.consolidate_calls

    failed = FakeMemoryProvider(capabilities(), RuntimeError("offline"), report())
    events = ProviderEventRecorder()
    with pytest.raises(CoreError) as unavailable:
        await gateway(PROVIDER, failed, events=events).retrieve(
            PROVIDER, actual_query, call_context()
        )
    assert unavailable.value.detail.category is ErrorCategory.UNAVAILABLE
    assert events.events[-1][0] == MEMORY_OPERATION_FAILED


@pytest.mark.asyncio
async def test_protocol_schema_version_and_identity_failures() -> None:
    actual_query = query()
    bad_schema_query = replace(actual_query, content=ContentBlock.json({"value": 3}))
    provider = fake_provider(actual_query)
    with pytest.raises(CoreError) as schema_failure:
        await gateway(PROVIDER, provider).retrieve(
            PROVIDER, bad_schema_query, call_context()
        )
    assert schema_failure.value.detail.category is ErrorCategory.PROTOCOL_FAILURE
    assert not provider.retrieve_calls

    identity_provider = FakeMemoryProvider(
        capabilities(), page(actual_query, ProviderId("other")), report()
    )
    with pytest.raises(CoreError) as page_identity:
        await gateway(PROVIDER, identity_provider).retrieve(
            PROVIDER, actual_query, call_context()
        )
    assert page_identity.value.detail.category is ErrorCategory.PROTOCOL_FAILURE

    wrong_capabilities = FakeMemoryProvider(
        capabilities(ProviderId("other")), page(actual_query), report()
    )
    with pytest.raises(CoreError) as capability_identity:
        await gateway(PROVIDER, wrong_capabilities).capabilities(
            PROVIDER, call_context()
        )
    assert capability_identity.value.detail.category is ErrorCategory.PROTOCOL_FAILURE

    ref = memory_ref(ProviderId("other"))
    with pytest.raises(CoreError) as forget_identity:
        await gateway(PROVIDER, provider).forget(
            PROVIDER, ForgetRequest(ref, ref.scope, ref.version), call_context()
        )
    assert forget_identity.value.detail.category is ErrorCategory.PROTOCOL_FAILURE


@pytest.mark.asyncio
async def test_version_conflict_and_partial_mutation_are_preserved() -> None:
    class MutationFailureProvider(FakeMemoryProvider):
        async def forget(
            self, request: ForgetRequest, context: RuntimeCallContext
        ) -> ForgetResult:
            del request, context
            raise core_error(
                ErrorCategory.VERSION_MISMATCH,
                "memory_version_conflict",
                "memory version changed",
            )

        async def remember(
            self, item: MemoryItem, context: RuntimeCallContext
        ) -> MemoryRef:
            del item, context
            raise core_error(
                ErrorCategory.PARTIAL_RESULT,
                "memory_partial_mutation",
                "memory mutation was partial",
            )

    actual_query = query()
    provider = MutationFailureProvider(capabilities(), page(actual_query), report())
    memory = gateway(PROVIDER, provider)
    ref = memory_ref()
    with pytest.raises(CoreError) as version:
        await memory.forget(
            PROVIDER, ForgetRequest(ref, ref.scope, ref.version), call_context()
        )
    assert version.value.detail.category is ErrorCategory.VERSION_MISMATCH
    with pytest.raises(CoreError) as partial:
        await memory.remember(PROVIDER, item(), call_context())
    assert partial.value.detail.category is ErrorCategory.PARTIAL_RESULT


@pytest.mark.asyncio
async def test_running_cancellation_deadline_cleanup_and_late_result_discard() -> None:
    actual_query = query()
    provider = BlockingMemoryProvider(capabilities(), page(actual_query), report())
    memory = gateway(PROVIDER, provider)
    context = call_context()
    task = asyncio.create_task(memory.retrieve(PROVIDER, actual_query, context))
    await provider.started.wait()
    context.cancellation.cancel()
    with pytest.raises(CoreError) as cancelled:
        await task
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert provider.cleaned

    deadline_provider = BlockingMemoryProvider(
        capabilities(), page(actual_query), report()
    )
    deadline_context = replace(
        call_context(), deadline=Deadline(NOW + timedelta(milliseconds=1))
    )
    with pytest.raises(CoreError) as timed_out:
        await gateway(PROVIDER, deadline_provider).retrieve(
            PROVIDER, actual_query, deadline_context
        )
    assert timed_out.value.detail.category is ErrorCategory.TIMEOUT
    assert deadline_provider.cleaned

    pre_cancelled = call_context()
    pre_cancelled.cancellation.cancel()
    untouched = fake_provider(actual_query)
    with pytest.raises(CoreError) as preflight:
        await gateway(PROVIDER, untouched).retrieve(
            PROVIDER, actual_query, pre_cancelled
        )
    assert preflight.value.detail.category is ErrorCategory.CANCELLED
    assert untouched.capability_calls == 0


@pytest.mark.asyncio
async def test_observability_failure_does_not_change_result() -> None:
    actual_query = query()
    provider = fake_provider(actual_query)
    events = ProviderEventRecorder(fail=True)
    result = await gateway(PROVIDER, provider, events=events).retrieve(
        PROVIDER, actual_query, call_context()
    )
    assert result.records


def test_registry_conflicts_missing_provider_and_capability_validation() -> None:
    actual_query = query()
    provider = fake_provider(actual_query)
    providers = registry(PROVIDER, provider)
    with pytest.raises(CoreError) as duplicate:
        providers.register(PROVIDER, provider)
    assert duplicate.value.detail.category is ErrorCategory.CONFLICT
    with pytest.raises(CoreError) as missing:
        providers.get(ProviderId("missing"))
    assert missing.value.detail.category is ErrorCategory.UNAVAILABLE

    with pytest.raises(ValueError):
        replace(capabilities(), maximum_result_limit=0)
    with pytest.raises(ValueError):
        replace(capabilities(), maximum_item_bytes=0)
