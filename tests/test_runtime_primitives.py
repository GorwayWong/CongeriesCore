from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from congeries_core.runtime.codec import dumps, loads
from congeries_core.runtime.context import RuntimeCallContext, SessionRef
from congeries_core.runtime.control import (
    CancellationToken,
    Deadline,
    SystemClock,
    TraceContext,
    require_utc,
)
from congeries_core.runtime.errors import CoreError, ErrorCategory, ErrorDetail
from congeries_core.runtime.ids import Identifier, RunId
from congeries_core.runtime.json_types import as_array, as_int, as_json_value, as_object
from congeries_core.runtime.scope import CoreScopeKind, ScopeRef

from .support import NOW, FixedClock, call_context, child_scope, root_scope


@pytest.mark.parametrize("value", ["", " padded", "padded ", "a\x01b", "x" * 256])
def test_identifier_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        Identifier(value)


def test_identifier_and_time_primitives() -> None:
    generated = RunId.new()
    assert str(generated) == generated.value
    assert require_utc(NOW.astimezone(timezone(timedelta(hours=8))), "time") == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        require_utc(datetime(2026, 1, 1), "time")
    assert SystemClock().now().tzinfo is UTC


def test_deadline_cancellation_and_trace() -> None:
    clock = FixedClock()
    future = Deadline(NOW + timedelta(seconds=1))
    assert not future.expired(clock)
    future.raise_if_expired(clock)
    clock.advance(1)
    with pytest.raises(CoreError) as timeout:
        future.raise_if_expired(clock)
    assert timeout.value.detail.category is ErrorCategory.TIMEOUT

    token = CancellationToken()
    assert token.cancel()
    assert not token.cancel()
    with pytest.raises(CoreError) as cancelled:
        token.raise_if_cancelled()
    assert cancelled.value.detail.category is ErrorCategory.CANCELLED

    trace = TraceContext.new()
    child = trace.child()
    assert child.trace_id == trace.trace_id
    assert child.span_id != trace.span_id


def test_scope_and_call_context_round_trip() -> None:
    parent = root_scope()
    child = child_scope(parent)
    assert child.is_equal_or_descendant_of(parent)
    assert ScopeRef.from_data(child.to_data()) == child
    assert loads(ScopeRef, dumps(child)) == child
    context = call_context(scope=parent)
    deadline = Deadline(NOW + timedelta(minutes=1))
    narrowed = context.narrow(scope=child, deadline=deadline)
    assert narrowed.scope == child
    assert narrowed.cancellation is context.cancellation
    assert narrowed.trace.span_id != context.trace.span_id
    derived = narrowed.for_child_run(RunId("child-run"))
    assert derived.parent_run_id == context.run_id
    assert (
        RuntimeCallContext.from_data(derived.to_data()).to_data() == derived.to_data()
    )

    with pytest.raises(CoreError, match="broaden"):
        narrowed.narrow(scope=parent)
    with pytest.raises(CoreError, match="extend"):
        narrowed.narrow(deadline=Deadline(deadline.at + timedelta(seconds=1)))


def test_call_context_active_checks() -> None:
    context = call_context()
    context.check_active(FixedClock())
    context.cancellation.cancel()
    with pytest.raises(CoreError):
        context.check_active(FixedClock())


def test_session_and_scope_validation() -> None:
    with pytest.raises(ValueError):
        SessionRef(" ", call_context().session_ref.session_id)  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        ScopeRef("Bad", "kind", "id")
    with pytest.raises(ValueError):
        ScopeRef("core", "Bad", "id")
    with pytest.raises(ValueError):
        ScopeRef("core", CoreScopeKind.RUN.value, " ")
    with pytest.raises(ValueError):
        ScopeRef.from_data(
            {"namespace": "core", "kind": "run", "id": "x", "parent": []}
        )


def test_error_detail_and_json_helpers() -> None:
    detail = ErrorDetail(
        ErrorCategory.UNAVAILABLE,
        "downstream",
        "downstream unavailable",
        retryable=True,
        cause_id="cause",
        metadata={"attempt": 2, "optional": None},
    )
    assert ErrorDetail.from_data(detail.to_data()) == detail
    with pytest.raises(ValueError):
        ErrorDetail(ErrorCategory.CONFLICT, "", "message")
    with pytest.raises(ValueError):
        ErrorDetail.from_data({**detail.to_data(), "metadata": {"bad": object()}})

    assert as_object({"a": 1}, "x") == {"a": 1}
    assert as_array([1], "x") == [1]
    assert as_int(2, "x") == 2
    assert as_json_value({"nested": [1, None]}) == {"nested": [1, None]}
    for value, helper in [([], as_object), ({}, as_array), (True, as_int)]:
        with pytest.raises(ValueError):
            helper(value, "x")
    with pytest.raises(ValueError):
        as_json_value(object())
    with pytest.raises(ValueError):
        loads(ScopeRef, "[]")


def test_deadline_constructor_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        Deadline(datetime(2026, 1, 1))
