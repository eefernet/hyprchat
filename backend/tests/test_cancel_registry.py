"""Offline unit tests for cancel_registry.await_cancellable.

The critical regression: EXTERNAL cancellation (asyncio.wait_for timeout in
the scheduler's _execute_task, or shutdown) must cancel the wrapped work task.
Before the fix, only the internal cancel-event waiter was cancelled and the
work task (a live model generation) kept running detached while the
scheduler's serial slot was already released.
"""
import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import cancel_registry  # noqa: E402


def test_external_timeout_cancels_the_work_task():
    async def scenario():
        run_id = "unit-ext-cancel"
        cancel_registry.register(run_id)
        started = asyncio.Event()
        work_cancelled = asyncio.Event()

        async def work():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                work_cancelled.set()
                raise
            return "never"

        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    cancel_registry.await_cancellable(work(), run_id), timeout=0.1)
            assert started.is_set()
            # The work task must be dead, not detached-and-running.
            await asyncio.wait_for(work_cancelled.wait(), timeout=1.0)
        finally:
            cancel_registry.cleanup(run_id)

    asyncio.run(scenario())


def test_internal_cancel_event_raises_runcancelled_and_kills_work():
    async def scenario():
        run_id = "unit-int-cancel"
        cancel_registry.register(run_id)
        work_cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                work_cancelled.set()
                raise

        async def fire_soon():
            await asyncio.sleep(0.05)
            cancel_registry.signal(run_id)

        try:
            fire = asyncio.ensure_future(fire_soon())
            with pytest.raises(cancel_registry.RunCancelled):
                await cancel_registry.await_cancellable(work(), run_id)
            await asyncio.wait_for(work_cancelled.wait(), timeout=1.0)
            await fire
        finally:
            cancel_registry.cleanup(run_id)

    asyncio.run(scenario())


def test_unregistered_run_id_passes_through():
    async def scenario():
        async def work():
            return 42
        assert await cancel_registry.await_cancellable(work(), "no-such-run") == 42

    asyncio.run(scenario())


def test_already_cancelled_raises_without_running():
    async def scenario():
        run_id = "unit-pre-cancel"
        cancel_registry.register(run_id)
        cancel_registry.signal(run_id)
        ran = []

        async def work():
            ran.append(True)

        try:
            with pytest.raises(cancel_registry.RunCancelled):
                await cancel_registry.await_cancellable(work(), run_id)
            assert not ran
        finally:
            cancel_registry.cleanup(run_id)

    asyncio.run(scenario())
