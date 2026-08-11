"""Deadline and cancellation enforcement around provider awaits."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress

from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import ErrorCategory, core_error


async def await_provider[ResultT](
    operation: Awaitable[ResultT],
    context: RuntimeCallContext,
    clock: Clock,
) -> ResultT:
    context.check_active(clock)
    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(context.cancellation.wait_cancelled())
    timeout: float | None = None
    if context.deadline is not None:
        timeout = max(0.0, (context.deadline.at - clock.now()).total_seconds())
    try:
        done, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            result = await operation_task
            context.check_active(clock)
            return result
        await _cancel_and_wait(operation_task)
        if cancellation_task in done:
            context.cancellation.raise_if_cancelled()
        raise core_error(
            ErrorCategory.TIMEOUT,
            "deadline_exceeded",
            "runtime call deadline has expired",
            retryable=True,
        )
    except BaseException:
        # Cancellation of this wrapper (for example by an enclosing Tool
        # deadline) must not orphan the provider/transport Task. Await teardown
        # before propagating so a late result cannot escape its lease boundary.
        await _cancel_and_wait(operation_task)
        raise
    finally:
        cancellation_task.cancel()
        await _ignore_cancellation(cancellation_task)


async def _cancel_and_wait[ResultT](task: asyncio.Future[ResultT]) -> None:
    if not task.done():
        task.cancel()
    await _ignore_cancellation(task)


async def _ignore_cancellation[ResultT](task: asyncio.Future[ResultT]) -> None:
    with suppress(BaseException):
        await task
