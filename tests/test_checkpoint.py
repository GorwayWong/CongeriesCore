from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from congeries_core.checkpoint import (
    ApprovalCheckpointState,
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Checkpoint,
    CheckpointCoordinator,
    CheckpointGateway,
    CheckpointMigrationRequest,
    CheckpointMigratorRegistry,
    CheckpointPage,
    CheckpointQuery,
    CheckpointReference,
    CheckpointStoreRegistry,
    DeleteCheckpointRequest,
    InMemoryCheckpointStore,
    NodeCheckpointState,
    NodeOutcome,
    RecoveryCoordinator,
    RecoveryPolicy,
    RecoveryRequest,
    SideEffectOutcome,
    SideEffectRecord,
    checkpoint_actions,
)
from congeries_core.checkpoint.coordinator import CheckpointEventPublisher
from congeries_core.event.integration import RuntimeEventPublisher
from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    Grant,
    PolicyDecision,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.policy.integration import RunAuditFailureHandler
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import CancellationToken, Deadline
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import (
    ApprovalId,
    CheckpointRef,
    CorrelationId,
    DefinitionId,
    IdempotencyKey,
    NodeId,
    PrincipalId,
    ProviderId,
    ResourceId,
    WorkflowId,
    WorkspaceId,
)
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.run import RunStatus, WorkflowRun, create_root_workflow_run
from congeries_core.state.repository import InMemoryRunRepository
from congeries_core.state.service import RunService

from .provider_support import AuditRecorder, FailureRecorder
from .support import NOW, FixedClock, call_context, child_scope, root_scope, session_ref

PROVIDER = ProviderId("checkpoint-store")


@dataclass(slots=True)
class EchoPolicy:
    override: Mapping[str, JsonValue] | None = None
    deny: bool = False
    requests: list[AccessRequest] = field(default_factory=list)

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        self.requests.append(request)
        if self.deny:
            return PolicyDecision.deny("test_denied")
        return PolicyDecision.allow(
            Grant(
                principal=request.principal,
                action=request.action,
                resource=request.resource,
                source_scope=request.context.scope,
                effective_scope=request.scope,
                constraints=(
                    dict(self.override)
                    if self.override is not None
                    else dict(request.constraints)
                ),
                issued_at=NOW,
                expires_at=None,
                policy_version="checkpoint-test-1",
                audit_correlation="checkpoint-audit",
            )
        )


@dataclass(slots=True)
class EventRecorder(CheckpointEventPublisher):
    order: list[str] = field(default_factory=list)
    fail_on: str | None = None

    async def _record(self, name: str) -> None:
        self.order.append(name)
        if self.fail_on == name:
            raise core_error(ErrorCategory.UNAVAILABLE, "audit_failed", "audit failed")

    async def checkpoint_saved(
        self, checkpoint: Checkpoint, run: WorkflowRun, context: RuntimeCallContext
    ) -> None:
        del checkpoint, run, context
        await self._record("checkpoint.saved")

    async def checkpoint_failed(
        self, checkpoint: Checkpoint, error: ErrorDetail, context: RuntimeCallContext
    ) -> None:
        del checkpoint, error, context
        await self._record("checkpoint.failed")

    async def checkpoint_migration_authorized(
        self,
        source: Checkpoint,
        migrated: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        del source, migrated, context
        await self._record("checkpoint.migration_authorized")

    async def checkpoint_fallback_authorized(
        self,
        source_ref: CheckpointRef,
        fallback: Checkpoint,
        context: RuntimeCallContext,
    ) -> None:
        del source_ref, fallback, context
        await self._record("checkpoint.fallback_authorized")

    async def approval_requested(
        self, request: ApprovalRequest, context: RuntimeCallContext
    ) -> None:
        del request, context
        await self._record("approval.requested")

    async def approval_decided(
        self, decision: ApprovalDecision, context: RuntimeCallContext
    ) -> None:
        del decision, context
        await self._record("approval.decided")


@dataclass(slots=True)
class Restorer:
    calls: list[CheckpointRef] = field(default_factory=list)
    failure: Exception | None = None

    async def restore(
        self, checkpoint: Checkpoint, context: RuntimeCallContext
    ) -> None:
        del context
        self.calls.append(checkpoint.ref)
        if self.failure is not None:
            raise self.failure


@dataclass(slots=True)
class Migrator:
    async def migrate(
        self,
        checkpoint: Checkpoint,
        request: CheckpointMigrationRequest,
        context: RuntimeCallContext,
    ) -> Checkpoint:
        del context
        return Checkpoint.create(
            checkpoint_id=CheckpointRef("checkpoint-migrated"),
            run_id=checkpoint.run_id,
            workflow_id=checkpoint.workflow_id,
            definition_id=request.target_definition_id,
            graph_version=request.target_graph_version,
            scope=checkpoint.scope,
            sequence=checkpoint.sequence + 1,
            attempt=checkpoint.attempt,
            previous_checkpoint_ref=checkpoint.ref,
            node_states=checkpoint.node_states,
            pending_nodes=checkpoint.pending_nodes,
            external_refs=checkpoint.external_refs,
            side_effects=checkpoint.side_effects,
            approvals=checkpoint.approvals,
            created_at=NOW,
        )


def reference(name: str = "artifact-1") -> CheckpointReference:
    return CheckpointReference(
        "artifact",
        ResourceRef("core", "artifact", ResourceId(name)),
        root_scope(),
        "1",
    )


def approval_request(run_id: object) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=ApprovalId("approval-1"),
        run_id=run_id,  # type: ignore[arg-type]
        node_id=NodeId("approval-node"),
        correlation_id=CorrelationId("approval-correlation"),
        scope=root_scope(),
        allowed_outcomes=(ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED),
        expires_at=NOW + timedelta(hours=1),
        prompt_ref=reference("approval-prompt"),
    )


def decision_for(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        node_id=request.node_id,
        correlation_id=request.correlation_id,
        scope=request.scope,
        actor=RuntimePrincipal.core(
            CorePrincipalKind.CORE_SERVICE, PrincipalId("approver")
        ),
        outcome=ApprovalOutcome.APPROVED,
        decided_at=NOW,
    )


def checkpoint_for(
    run: WorkflowRun,
    *,
    ref: str = "checkpoint-1",
    sequence: int = 1,
    previous: CheckpointRef | None = None,
    definition_id: DefinitionId | None = None,
    graph_version: str | None = None,
    approval: ApprovalCheckpointState | None = None,
    include_contract_data: bool = False,
) -> Checkpoint:
    node_states = ()
    approvals = ()
    external_refs = ()
    side_effects = ()
    if approval is not None:
        node_states = (
            NodeCheckpointState(
                approval.request.node_id,
                NodeOutcome.WAITING_APPROVAL,
                approval_state=approval,
            ),
        )
        approvals = (approval,)
    elif include_contract_data:
        external_refs = (reference(),)
        node_states = (
            NodeCheckpointState(
                NodeId("node-1"), NodeOutcome.SUCCEEDED, output_ref=reference()
            ),
        )
        side_effects = (
            SideEffectRecord(
                ResourceRef("core", "operation", ResourceId("operation-1")),
                IdempotencyKey("operation-key"),
                "a" * 64,
                reference(),
                SideEffectOutcome.SUCCEEDED,
            ),
        )
    return Checkpoint.create(
        checkpoint_id=CheckpointRef(ref),
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        definition_id=definition_id or run.definition_id,
        graph_version=graph_version or run.graph_version,
        scope=run.scope,
        sequence=sequence,
        attempt=run.attempt,
        previous_checkpoint_ref=previous,
        node_states=node_states,
        pending_nodes=(NodeId("pending-node"),),
        external_refs=external_refs,
        side_effects=side_effects,
        approvals=approvals,
        created_at=NOW,
    )


async def running_runtime(
    *,
    store: InMemoryCheckpointStore | None = None,
    policy: EchoPolicy | None = None,
    publisher: EventRecorder | None = None,
) -> tuple[
    WorkflowRun,
    RuntimeCallContext,
    RunService,
    CheckpointGateway,
    CheckpointCoordinator,
    InMemoryCheckpointStore,
    EchoPolicy,
    EventRecorder,
]:
    repository = InMemoryRunRepository()
    runs = RunService(repository, FixedClock())
    run = create_root_workflow_run(
        definition_id=DefinitionId("workflow-definition"),
        workflow_id=WorkflowId("workflow-1"),
        graph_version="v1",
        workspace_id=WorkspaceId("workspace-1"),
        scope=root_scope(),
        created_at=NOW,
        session_ref=session_ref(),
    )
    await runs.create(run)
    current = await runs.start(run.run_id, run.state_version)
    current = await runs.advance(
        run.run_id, current.state_version, RunStatus.CONTEXT_LOADING
    )
    current = await runs.advance(run.run_id, current.state_version, RunStatus.RUNNING)
    assert isinstance(current, WorkflowRun)
    actual_store = store or InMemoryCheckpointStore(PROVIDER)
    registry = CheckpointStoreRegistry()
    registry.register(PROVIDER, actual_store)
    actual_policy = policy or EchoPolicy()
    dispatcher = AuthorizedDispatcher(
        action_registry=ActionRegistry(checkpoint_actions()),
        audit_publisher=AuditRecorder(),
        audit_failure_handler=FailureRecorder(),
        clock=FixedClock(),
        policy=actual_policy,
    )
    gateway = CheckpointGateway(registry, dispatcher, FixedClock())
    actual_publisher = publisher or EventRecorder()
    coordinator = CheckpointCoordinator(gateway, runs, actual_publisher)
    context = call_context(run_id=run.run_id, scope=run.scope)
    return (
        current,
        context,
        runs,
        gateway,
        coordinator,
        actual_store,
        actual_policy,
        actual_publisher,
    )


def test_checkpoint_round_trip_integrity_and_validation() -> None:
    run = create_root_workflow_run(
        definition_id=DefinitionId("workflow-definition"),
        workflow_id=WorkflowId("workflow-1"),
        graph_version="v1",
        workspace_id=WorkspaceId("workspace-1"),
        scope=root_scope(),
        created_at=NOW,
    )
    checkpoint = checkpoint_for(run, include_contract_data=True)
    checkpoint.verify_integrity()
    assert Checkpoint.from_data(checkpoint.to_data()) == checkpoint
    assert checkpoint.integrity.digest == checkpoint.canonical_digest()

    tampered = checkpoint.to_data()
    tampered["graph_version"] = "v2"
    with pytest.raises(CoreError) as integrity:
        Checkpoint.from_data(tampered)
    assert integrity.value.detail.code == "checkpoint_integrity_failure"

    sensitive = checkpoint.to_data()
    sensitive["secret"] = "must-not-enter-checkpoint"
    with pytest.raises(ValueError, match="unknown or missing"):
        Checkpoint.from_data(sensitive)
    reference_data = reference().to_data()
    reference_data["content"] = "sensitive body"
    with pytest.raises(ValueError, match="unknown or missing"):
        CheckpointReference.from_data(reference_data)

    with pytest.raises(ValueError, match="predecessor"):
        replace(checkpoint, sequence=2)
    with pytest.raises(ValueError, match="unique"):
        replace(checkpoint, pending_nodes=(NodeId("x"), NodeId("x")))
    with pytest.raises(ValueError, match="result reference"):
        SideEffectRecord(
            ResourceRef("core", "operation", ResourceId("bad")),
            IdempotencyKey("key"),
            "b" * 64,
            None,
            SideEffectOutcome.SUCCEEDED,
        )


@pytest.mark.asyncio
async def test_store_atomic_sequence_pagination_cursor_and_delete() -> None:
    run, context, _, _, _, store, _, _ = await running_runtime()
    first = checkpoint_for(run)
    assert await store.save(first, context) == first.ref
    assert await store.save(first, context) == first.ref
    assert await store.load(first.ref, context) == first

    different = checkpoint_for(run, ref="different")
    with pytest.raises(CoreError) as sequence:
        await store.save(different, context)
    assert sequence.value.detail.code == "checkpoint_sequence_conflict"

    second = checkpoint_for(run, ref="checkpoint-2", sequence=2, previous=first.ref)
    await store.save(second, context)
    query = CheckpointQuery(PROVIDER, run.run_id, run.scope, limit=1)
    page = await store.list(query, context)
    assert page.items == (second,)
    assert page.next_cursor is not None
    assert CheckpointPage.from_data(page.to_data()) == page
    next_page = await store.list(replace(query, cursor=page.next_cursor), context)
    assert next_page.items == (first,)
    with pytest.raises(CoreError) as drift:
        await store.list(
            replace(query, graph_version="v2", cursor=page.next_cursor), context
        )
    assert drift.value.detail.code == "checkpoint_cursor_drift"

    deleted = await store.delete(
        DeleteCheckpointRequest(PROVIDER, run.run_id, second.ref, run.scope), context
    )
    assert deleted.deleted
    assert not (
        await store.delete(
            DeleteCheckpointRequest(PROVIDER, run.run_id, second.ref, run.scope),
            context,
        )
    ).deleted

    effect_runtime = await running_runtime()
    erun, econtext, _, _, _, effect_store, _, _ = effect_runtime
    effect_first = checkpoint_for(erun, include_contract_data=True)
    await effect_store.save(effect_first, econtext)
    changed_effect = replace(effect_first.side_effects[0], request_fingerprint="b" * 64)
    effect_second = Checkpoint.create(
        checkpoint_id=CheckpointRef("effect-checkpoint-2"),
        run_id=erun.run_id,
        workflow_id=erun.workflow_id,
        definition_id=erun.definition_id,
        graph_version=erun.graph_version,
        scope=erun.scope,
        sequence=2,
        attempt=erun.attempt,
        previous_checkpoint_ref=effect_first.ref,
        node_states=effect_first.node_states,
        pending_nodes=effect_first.pending_nodes,
        external_refs=effect_first.external_refs,
        side_effects=(changed_effect,),
        created_at=NOW,
    )
    with pytest.raises(CoreError) as idempotency:
        await effect_store.save(effect_second, econtext)
    assert idempotency.value.detail.code == "checkpoint_idempotency_conflict"


@pytest.mark.asyncio
async def test_gateway_default_deny_invalid_grant_and_cancellation_cleanup() -> None:
    run, context, _, gateway, _, store, policy, _ = await running_runtime(
        policy=EchoPolicy(deny=True)
    )
    checkpoint = checkpoint_for(run)
    with pytest.raises(CoreError) as denied:
        await gateway.save(PROVIDER, checkpoint, context)
    assert denied.value.detail.category is ErrorCategory.DENIED
    with pytest.raises(CoreError):
        await store.load(checkpoint.ref, context)

    policy.deny = False
    policy.override = {"run_id": run.run_id.value}
    with pytest.raises(CoreError) as invalid:
        await gateway.save(PROVIDER, checkpoint, context)
    assert invalid.value.detail.code == "invalid_grant"

    registry = CheckpointStoreRegistry()
    registry.register(PROVIDER, store)
    unknown_gateway = CheckpointGateway(
        registry,
        AuthorizedDispatcher(
            action_registry=ActionRegistry(),
            audit_publisher=AuditRecorder(),
            audit_failure_handler=FailureRecorder(),
            clock=FixedClock(),
            policy=EchoPolicy(),
        ),
        FixedClock(),
    )
    with pytest.raises(CoreError) as unknown:
        await unknown_gateway.save(PROVIDER, checkpoint, context)
    assert unknown.value.detail.code == "unknown_action"

    narrow_scope = child_scope(run.scope)
    narrow = Checkpoint.create(
        checkpoint_id=CheckpointRef("narrow-checkpoint"),
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        definition_id=run.definition_id,
        graph_version=run.graph_version,
        scope=narrow_scope,
        sequence=1,
        attempt=run.attempt,
        previous_checkpoint_ref=None,
        created_at=NOW,
    )

    class ExpandingPolicy(EchoPolicy):
        async def authorize(self, request: AccessRequest) -> PolicyDecision:
            decision = await super().authorize(request)
            assert decision.grant is not None
            return PolicyDecision.allow(
                replace(decision.grant, effective_scope=request.context.scope)
            )

    expanding_gateway = CheckpointGateway(
        registry,
        AuthorizedDispatcher(
            action_registry=ActionRegistry(checkpoint_actions()),
            audit_publisher=AuditRecorder(),
            audit_failure_handler=FailureRecorder(),
            clock=FixedClock(),
            policy=ExpandingPolicy(),
        ),
        FixedClock(),
    )
    with pytest.raises(CoreError) as expansion:
        await expanding_gateway.save(PROVIDER, narrow, context)
    assert expansion.value.detail.code == "invalid_grant"

    class FailingCrossScopeAudit(AuditRecorder):
        async def cross_scope_granted(
            self, request: AccessRequest, grant: Grant
        ) -> None:
            del request, grant
            raise core_error(
                ErrorCategory.UNAVAILABLE, "cross_scope_audit_failed", "audit failed"
            )

    failures = FailureRecorder()
    cross_gateway = CheckpointGateway(
        registry,
        AuthorizedDispatcher(
            action_registry=ActionRegistry(checkpoint_actions()),
            audit_publisher=FailingCrossScopeAudit(),
            audit_failure_handler=failures,
            clock=FixedClock(),
            policy=EchoPolicy(),
        ),
        FixedClock(),
    )
    with pytest.raises(CoreError) as cross:
        await cross_gateway.save(PROVIDER, narrow, context)
    assert cross.value.detail.code == "cross_scope_audit_failed"
    assert failures.errors[0].code == "cross_scope_audit_failed"

    class SlowStore(InMemoryCheckpointStore):
        def __init__(self) -> None:
            super().__init__(PROVIDER)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def save(
            self, checkpoint: Checkpoint, context: RuntimeCallContext
        ) -> CheckpointRef:
            del checkpoint, context
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            raise AssertionError("unreachable")

    slow = SlowStore()
    runtime = await running_runtime(store=slow)
    run2, context2, _, gateway2, _, _, _, _ = runtime
    token = CancellationToken()
    context2 = replace(context2, cancellation=token)
    task = asyncio.create_task(gateway2.save(PROVIDER, checkpoint_for(run2), context2))
    await slow.started.wait()
    token.cancel()
    with pytest.raises(CoreError) as cancelled:
        await task
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED
    assert slow.cancelled.is_set()

    expired = replace(
        context2,
        cancellation=CancellationToken(),
        deadline=Deadline(NOW - timedelta(seconds=1)),
    )
    with pytest.raises(CoreError) as timeout:
        await gateway2.save(PROVIDER, checkpoint_for(run2), expired)
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_checkpoint_commit_cas_orphan_and_orphan_only_delete() -> None:
    runtime = await running_runtime()
    run, context, _runs, _, coordinator, store, _, publisher = runtime
    checkpoint = checkpoint_for(run)
    committed = await coordinator.save(PROVIDER, checkpoint, run.state_version, context)
    assert committed.latest_checkpoint_ref == checkpoint.ref
    assert publisher.order == ["checkpoint.saved"]
    assert (
        await coordinator.save(PROVIDER, checkpoint, run.state_version, context)
    ) == committed
    assert publisher.order == ["checkpoint.saved"]
    with pytest.raises(CoreError) as protected:
        await coordinator.delete_orphan(
            DeleteCheckpointRequest(PROVIDER, run.run_id, checkpoint.ref, run.scope),
            context,
        )
    assert protected.value.detail.code == "checkpoint_not_orphan"

    class RacingStore(InMemoryCheckpointStore):
        raced = False
        callback: object | None = None

        async def save(
            self, checkpoint: Checkpoint, context: RuntimeCallContext
        ) -> CheckpointRef:
            result = await super().save(checkpoint, context)
            if not self.raced:
                self.raced = True
                assert self.callback is not None
                await self.callback()  # type: ignore[operator]
            return result

    race_store = RacingStore(PROVIDER)
    race_runtime = await running_runtime(store=race_store)
    rrun, rcontext, rruns, _, race_coordinator, _, _, _ = race_runtime

    async def race() -> None:
        await rruns.pause(rrun.run_id, rrun.state_version)

    race_store.callback = race
    orphan = checkpoint_for(rrun, ref="orphan")
    with pytest.raises(CoreError) as stale:
        await race_coordinator.save(PROVIDER, orphan, rrun.state_version, rcontext)
    assert stale.value.detail.code == "stale_state_version"
    assert (await race_store.load(orphan.ref, rcontext)).ref == orphan.ref
    result = await race_coordinator.delete_orphan(
        DeleteCheckpointRequest(PROVIDER, rrun.run_id, orphan.ref, rrun.scope),
        rcontext,
    )
    assert result.deleted
    assert (await store.load(checkpoint.ref, context)).ref == checkpoint.ref


@pytest.mark.asyncio
async def test_exact_recovery_attempt_source_and_restore_failure_policy() -> None:
    runtime = await running_runtime()
    run, context, runs, gateway, coordinator, _, _, publisher = runtime
    checkpoint = checkpoint_for(run)
    committed = await coordinator.save(PROVIDER, checkpoint, run.state_version, context)
    restorer = Restorer()
    recovery = RecoveryCoordinator(
        gateway=gateway,
        runs=runs,
        migrators=CheckpointMigratorRegistry(),
        restorer=restorer,
        audit_failure_handler=RunAuditFailureHandler(runs),
        publisher=publisher,
    )
    result = await recovery.recover(
        RecoveryRequest(
            run.run_id,
            committed.state_version,
            PROVIDER,
            run.definition_id,
            run.graph_version,
        ),
        context,
    )
    assert result.run.status is RunStatus.RUNNING
    assert result.run.attempt == 2
    assert result.run.attempt_history[-1].checkpoint_ref == checkpoint.ref
    assert restorer.calls == [checkpoint.ref]
    terminal = await runs.complete(result.run.run_id, result.run.state_version)
    with pytest.raises(CoreError) as terminal_recovery:
        await recovery.recover(
            RecoveryRequest(
                run.run_id,
                terminal.state_version,
                PROVIDER,
                run.definition_id,
                run.graph_version,
            ),
            context,
        )
    assert terminal_recovery.value.detail.code == "illegal_recovery_state"

    runtime2 = await running_runtime()
    run2, context2, runs2, gateway2, coordinator2, _, _, _ = runtime2
    committed2 = await coordinator2.save(
        PROVIDER, checkpoint_for(run2), run2.state_version, context2
    )
    failing = RecoveryCoordinator(
        gateway=gateway2,
        runs=runs2,
        migrators=CheckpointMigratorRegistry(),
        restorer=Restorer(failure=RuntimeError("restore failed")),
        audit_failure_handler=RunAuditFailureHandler(runs2),
    )
    with pytest.raises(RuntimeError, match="restore failed"):
        await failing.recover(
            RecoveryRequest(
                run2.run_id,
                committed2.state_version,
                PROVIDER,
                run2.definition_id,
                run2.graph_version,
            ),
            context2,
        )
    assert (await runs2.get(run2.run_id)).status is RunStatus.RECOVERING


@pytest.mark.asyncio
async def test_migration_missing_migrator_and_explicit_corrupt_fallback() -> None:
    runtime = await running_runtime()
    run, context, runs, gateway, coordinator, store, _, publisher = runtime
    first = checkpoint_for(run)
    committed = await coordinator.save(PROVIDER, first, run.state_version, context)
    missing = RecoveryCoordinator(
        gateway=gateway,
        runs=runs,
        migrators=CheckpointMigratorRegistry(),
        restorer=Restorer(),
        audit_failure_handler=RunAuditFailureHandler(runs),
        publisher=publisher,
    )
    with pytest.raises(CoreError) as no_migrator:
        await missing.recover(
            RecoveryRequest(
                run.run_id,
                committed.state_version,
                PROVIDER,
                DefinitionId("workflow-definition-v2"),
                "v2",
            ),
            context,
        )
    assert no_migrator.value.detail.category is ErrorCategory.VERSION_MISMATCH

    registry = CheckpointMigratorRegistry()
    migration_request = CheckpointMigrationRequest(
        run.workflow_id,
        run.definition_id,
        run.graph_version,
        DefinitionId("workflow-definition-v2"),
        "v2",
    )
    registry.register(migration_request, Migrator())
    recovery = RecoveryCoordinator(
        gateway=gateway,
        runs=runs,
        migrators=registry,
        restorer=Restorer(),
        audit_failure_handler=RunAuditFailureHandler(runs),
        publisher=publisher,
    )
    migrated = await recovery.recover(
        RecoveryRequest(
            run.run_id,
            committed.state_version,
            PROVIDER,
            migration_request.target_definition_id,
            migration_request.target_graph_version,
        ),
        context,
    )
    assert migrated.migrated
    assert migrated.run.definition_id == migration_request.target_definition_id
    assert await store.load(first.ref, context) == first
    assert "checkpoint.migration_authorized" in publisher.order

    fallback_runtime = await running_runtime()
    frun, fcontext, fruns, fgateway, fcoordinator, _, _, fpublisher = fallback_runtime
    valid = checkpoint_for(frun)
    first_commit = await fcoordinator.save(
        PROVIDER, valid, frun.state_version, fcontext
    )
    corrupt = checkpoint_for(
        first_commit,
        ref="checkpoint-2",
        sequence=2,
        previous=valid.ref,
    )
    second_commit = await fcoordinator.save(
        PROVIDER, corrupt, first_commit.state_version, fcontext
    )
    object.__setattr__(corrupt.integrity, "digest", "f" * 64)
    fallback = RecoveryCoordinator(
        gateway=fgateway,
        runs=fruns,
        migrators=CheckpointMigratorRegistry(),
        restorer=Restorer(),
        audit_failure_handler=RunAuditFailureHandler(fruns),
        publisher=fpublisher,
    )
    base_request = RecoveryRequest(
        frun.run_id,
        second_commit.state_version,
        PROVIDER,
        frun.definition_id,
        frun.graph_version,
    )
    with pytest.raises(CoreError) as disabled:
        await fallback.recover(base_request, fcontext)
    assert disabled.value.detail.code == "checkpoint_integrity_failure"
    recovered = await fallback.recover(
        replace(
            base_request,
            policy=RecoveryPolicy(allow_corrupt_fallback=True),
        ),
        fcontext,
    )
    assert recovered.fell_back
    assert recovered.checkpoint.ref == valid.ref
    assert "checkpoint.fallback_authorized" in fpublisher.order


@pytest.mark.asyncio
async def test_approval_checkpoint_order_idempotency_conflict_and_audit_failure() -> (
    None
):
    runtime = await running_runtime()
    run, context, runs, gateway, coordinator, _, _, publisher = runtime
    approval = approval_request(run.run_id)
    pending = ApprovalCheckpointState(approval)
    pre = checkpoint_for(run, approval=pending)
    approval_coordinator = ApprovalCoordinator(
        checkpoint_coordinator=coordinator,
        checkpoint_gateway=gateway,
        runs=runs,
        dispatcher=AuthorizedDispatcher(
            action_registry=ActionRegistry(checkpoint_actions()),
            audit_publisher=AuditRecorder(),
            audit_failure_handler=FailureRecorder(),
            clock=FixedClock(),
            policy=EchoPolicy(),
        ),
        audit_failure_handler=RunAuditFailureHandler(runs),
        clock=FixedClock(),
        publisher=publisher,
    )
    waiting = await approval_coordinator.request_approval(
        PROVIDER, approval, pre, run.state_version, context
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert publisher.order[-2:] == ["checkpoint.saved", "approval.requested"]

    decision = decision_for(approval)
    decided_state = ApprovalCheckpointState(approval, decision)
    post = checkpoint_for(
        waiting,
        ref="checkpoint-2",
        sequence=2,
        previous=pre.ref,
        approval=decided_state,
    )
    forged = replace(decision, correlation_id=CorrelationId("forged-correlation"))
    with pytest.raises(CoreError) as forged_error:
        await approval_coordinator.decide(
            PROVIDER, forged, post, waiting.state_version, context
        )
    assert forged_error.value.detail.code == "approval_identity_mismatch"

    denying_coordinator = ApprovalCoordinator(
        checkpoint_coordinator=coordinator,
        checkpoint_gateway=gateway,
        runs=runs,
        dispatcher=AuthorizedDispatcher(
            action_registry=ActionRegistry(checkpoint_actions()),
            audit_publisher=AuditRecorder(),
            audit_failure_handler=FailureRecorder(),
            clock=FixedClock(),
            policy=EchoPolicy(deny=True),
        ),
        audit_failure_handler=RunAuditFailureHandler(runs),
        clock=FixedClock(),
        publisher=publisher,
    )
    with pytest.raises(CoreError) as actor_denied:
        await denying_coordinator.decide(
            PROVIDER, decision, post, waiting.state_version, context
        )
    assert actor_denied.value.detail.category is ErrorCategory.DENIED
    running = await approval_coordinator.decide(
        PROVIDER, decision, post, waiting.state_version, context
    )
    assert running.status is RunStatus.RUNNING
    assert publisher.order[-2:] == ["checkpoint.saved", "approval.decided"]
    assert (
        await approval_coordinator.decide(
            PROVIDER, decision, post, waiting.state_version, context
        )
    ) == running
    conflict = replace(decision, outcome=ApprovalOutcome.REJECTED)
    with pytest.raises(CoreError) as different:
        await approval_coordinator.decide(
            PROVIDER, conflict, post, running.state_version, context
        )
    assert different.value.detail.code == "approval_decision_conflict"

    failed_runtime = await running_runtime(
        publisher=EventRecorder(fail_on="approval.requested")
    )
    frun, fcontext, fruns, fgateway, fcoordinator, _, _, fpublisher = failed_runtime
    failed_request = approval_request(frun.run_id)
    failed_pre = checkpoint_for(frun, approval=ApprovalCheckpointState(failed_request))
    failed_coordinator = ApprovalCoordinator(
        checkpoint_coordinator=fcoordinator,
        checkpoint_gateway=fgateway,
        runs=fruns,
        dispatcher=AuthorizedDispatcher(
            action_registry=ActionRegistry(checkpoint_actions()),
            audit_publisher=AuditRecorder(),
            audit_failure_handler=FailureRecorder(),
            clock=FixedClock(),
            policy=EchoPolicy(),
        ),
        audit_failure_handler=RunAuditFailureHandler(fruns),
        clock=FixedClock(),
        publisher=fpublisher,
    )
    with pytest.raises(CoreError, match="audit failed"):
        await failed_coordinator.request_approval(
            PROVIDER, failed_request, failed_pre, frun.state_version, fcontext
        )
    assert (await fruns.get(frun.run_id)).status is RunStatus.PAUSED


@pytest.mark.asyncio
async def test_runtime_event_publisher_checkpoint_and_approval_delivery_classes() -> (
    None
):
    class Dispatcher:
        def __init__(self) -> None:
            self.created: list[dict[str, object]] = []

        async def create_event(self, **values: object) -> object:
            self.created.append(values)
            return values

        async def publish(
            self, event: object, context: object, principal: object
        ) -> None:
            del event, context, principal

    dispatcher = Dispatcher()
    publisher = RuntimeEventPublisher(dispatcher)  # type: ignore[arg-type]
    runtime = await running_runtime()
    run, context, _, _, _, _, _, _ = runtime
    checkpoint = checkpoint_for(run)
    request = approval_request(run.run_id)
    decision = decision_for(request)
    await publisher.checkpoint_saved(checkpoint, run, context)
    await publisher.checkpoint_failed(
        checkpoint,
        ErrorDetail(ErrorCategory.CONFLICT, "failed", "failed"),
        context,
    )
    migrated = checkpoint_for(
        run,
        ref="checkpoint-2",
        sequence=2,
        previous=checkpoint.ref,
        graph_version="v2",
    )
    await publisher.checkpoint_migration_authorized(checkpoint, migrated, context)
    await publisher.checkpoint_fallback_authorized(migrated.ref, checkpoint, context)
    await publisher.approval_requested(request, context)
    await publisher.approval_decided(decision, context)
    assert [item["delivery_class"].value for item in dispatcher.created] == [
        "observability",
        "observability",
        "audit",
        "audit",
        "audit",
        "audit",
    ]
