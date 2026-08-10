from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from congeries_core.policy.authorization import (
    AccessRequest,
    ActionRef,
    ActionRegistry,
    AuthorizedDispatcher,
    CorePrincipalKind,
    DenyAllPolicy,
    Grant,
    PolicyDecision,
    PolicyEffect,
    ResourceRef,
    RuntimePrincipal,
)
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import PrincipalId, ResourceId

from .support import (
    NOW,
    FixedClock,
    MatchingAllowPolicy,
    call_context,
    child_scope,
    root_scope,
)


class AuditRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.denied = 0
        self.cross_scope = 0
        self.fail = fail

    async def authorization_denied(
        self, request: AccessRequest, decision: PolicyDecision
    ) -> None:
        del request, decision
        self.denied += 1
        if self.fail:
            raise core_error(ErrorCategory.UNAVAILABLE, "audit_failed", "audit failed")

    async def cross_scope_granted(self, request: AccessRequest, grant: Grant) -> None:
        del request, grant
        self.cross_scope += 1
        if self.fail:
            raise core_error(ErrorCategory.UNAVAILABLE, "audit_failed", "audit failed")


class FailureRecorder:
    def __init__(self) -> None:
        self.errors: list[ErrorDetail] = []

    async def handle(self, run_id: object, error: ErrorDetail) -> None:
        del run_id
        self.errors.append(error)


class IndeterminatePolicy:
    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        del request
        return PolicyDecision.indeterminate("uncertain")


class ReplacingPolicy:
    def __init__(self, field: str, value: object) -> None:
        self.field = field
        self.value = value

    async def authorize(self, request: AccessRequest) -> PolicyDecision:
        base = (await MatchingAllowPolicy().authorize(request)).grant
        assert base is not None
        return PolicyDecision.allow(replace(base, **{self.field: self.value}))


def make_request(*, destination=None) -> AccessRequest:
    context = call_context(scope=root_scope())
    scope = destination or context.scope
    return AccessRequest(
        principal=RuntimePrincipal.core(CorePrincipalKind.RUN, PrincipalId("run-1")),
        action=ActionRef("core", "model.generate", "1"),
        resource=ResourceRef("core", "model", ResourceId("model-1")),
        scope=scope,
        context=context,
    )


def make_dispatcher(
    request: AccessRequest,
    *,
    policy=None,
    audit: AuditRecorder | None = None,
    failures: FailureRecorder | None = None,
) -> AuthorizedDispatcher[str]:
    return AuthorizedDispatcher(
        action_registry=ActionRegistry((request.action,)),
        audit_publisher=audit or AuditRecorder(),
        audit_failure_handler=failures or FailureRecorder(),
        clock=FixedClock(),
        policy=policy,
    )


@pytest.mark.asyncio
async def test_default_deny_unknown_action_and_indeterminate() -> None:
    request = make_request()
    for dispatcher in (
        make_dispatcher(request),
        make_dispatcher(request, policy=DenyAllPolicy()),
        make_dispatcher(request, policy=IndeterminatePolicy()),
    ):
        with pytest.raises(CoreError) as denied:
            await dispatcher.dispatch(
                request, lambda call: _return(call.context.scope.id)
            )
        assert denied.value.detail.category is ErrorCategory.DENIED

    unknown = replace(request, action=ActionRef("core", "unknown", "1"))
    with pytest.raises(CoreError) as denied:
        await make_dispatcher(request, policy=MatchingAllowPolicy()).dispatch(
            unknown, lambda call: _return(call.context.scope.id)
        )
    assert denied.value.detail.code == "unknown_action"


@pytest.mark.asyncio
async def test_authorized_dispatch_and_cross_scope_audit_gate() -> None:
    destination = child_scope(root_scope())
    request = make_request(destination=destination)
    audit = AuditRecorder()
    result = await make_dispatcher(
        request, policy=MatchingAllowPolicy(), audit=audit
    ).dispatch(request, lambda call: _return(call.context.scope.id))
    assert result == destination.id
    assert audit.cross_scope == 1

    failing_audit = AuditRecorder(fail=True)
    failures = FailureRecorder()
    with pytest.raises(CoreError, match="audit failed"):
        await make_dispatcher(
            request,
            policy=MatchingAllowPolicy(),
            audit=failing_audit,
            failures=failures,
        ).dispatch(request, lambda call: _return("not-called"))
    assert failures.errors[0].code == "audit_failed"


@pytest.mark.asyncio
async def test_denial_audit_failure_is_reported() -> None:
    request = make_request()
    failures = FailureRecorder()
    with pytest.raises(CoreError, match="audit failed"):
        await make_dispatcher(
            request,
            policy=DenyAllPolicy(),
            audit=AuditRecorder(fail=True),
            failures=failures,
        ).dispatch(request, lambda call: _return("not-called"))
    assert len(failures.errors) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["principal", "action", "resource", "source_scope", "effective_scope"]
)
async def test_invalid_grants_are_denied(field: str) -> None:
    request = make_request()
    replacements = {
        "principal": RuntimePrincipal.core(
            CorePrincipalKind.AGENT, PrincipalId("other")
        ),
        "action": ActionRef("core", "other", "1"),
        "resource": ResourceRef("core", "model", ResourceId("other")),
        "source_scope": child_scope(request.context.scope),
        "effective_scope": child_scope(request.scope),
    }
    with pytest.raises(CoreError) as denied:
        await make_dispatcher(
            request, policy=ReplacingPolicy(field, replacements[field])
        ).dispatch(request, lambda call: _return("not-called"))
    assert denied.value.detail.code == "invalid_grant"


@pytest.mark.asyncio
async def test_expired_grant_and_action_registry() -> None:
    request = make_request()

    class ExpiredPolicy:
        async def authorize(self, access: AccessRequest) -> PolicyDecision:
            grant = (await MatchingAllowPolicy().authorize(access)).grant
            assert grant is not None
            return PolicyDecision.allow(
                replace(
                    grant,
                    issued_at=NOW - timedelta(minutes=2),
                    expires_at=NOW - timedelta(minutes=1),
                )
            )

    with pytest.raises(CoreError) as denied:
        await make_dispatcher(request, policy=ExpiredPolicy()).dispatch(
            request, lambda call: _return("not-called")
        )
    assert denied.value.detail.code == "grant_expired"

    registry = ActionRegistry()
    registry.register(request.action)
    assert registry.contains(request.action)
    with pytest.raises(CoreError):
        registry.register(request.action)


def test_authorization_value_validation() -> None:
    with pytest.raises(ValueError):
        ActionRef("Bad", "action", "1")
    with pytest.raises(ValueError):
        ResourceRef("core", "Bad", ResourceId("r"))
    with pytest.raises(ValueError):
        RuntimePrincipal("core", "Bad", PrincipalId("p"))
    with pytest.raises(ValueError):
        PolicyDecision(PolicyEffect.ALLOW)
    with pytest.raises(ValueError):
        PolicyDecision(PolicyEffect.DENY, grant=(MatchingAllowPolicy()))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PolicyDecision(PolicyEffect.DENY)


async def _return(value: str) -> str:
    return value
