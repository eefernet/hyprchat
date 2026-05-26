"""Cancel registry — per-run asyncio.Event wired to the Stop button.

When the user presses Stop in the UI, the frontend POSTs to
`/api/runs/{run_id}/cancel`. That endpoint calls `signal(run_id)` here, which
sets the registered Event for the run. Anywhere an agent (architect, reviewer,
fixer, builder) is waiting on a long-running call, it should wrap that call in
`await_cancellable(coro, run_id)` so the wait races the cancel event and aborts
the in-flight work cleanly when Stop fires.

Lives in-process — restarts clear the registry. That's fine because the routes
also mark the DB row `cancelled` on signal, so frontend polling sees the right
state regardless of whether the in-process wait actually returned.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Optional


class RunCancelled(Exception):
    """Raised by `await_cancellable` when the registered cancel event fires."""

    def __init__(self, run_id: str = ""):
        self.run_id = run_id
        super().__init__(f"run cancelled: {run_id}" if run_id else "run cancelled")


_events: dict[str, asyncio.Event] = {}


def register(run_id: str) -> asyncio.Event:
    """Register a cancel Event for `run_id`. Idempotent — returns the existing
    event if already registered. Empty `run_id` returns a detached event."""
    if not run_id:
        return asyncio.Event()
    ev = _events.get(run_id)
    if ev is None:
        ev = asyncio.Event()
        _events[run_id] = ev
    return ev


def get_event(run_id: str) -> Optional[asyncio.Event]:
    return _events.get(run_id) if run_id else None


def is_cancelled(run_id: str) -> bool:
    ev = _events.get(run_id) if run_id else None
    return bool(ev and ev.is_set())


def signal(run_id: str) -> bool:
    """Set the cancel event. Returns True if a registered event was found.

    A False return doesn't mean the cancel was useless — the run may have
    finished before we got here, or it may not have registered yet (rare race
    on very-early cancel). The route will still mark the DB row cancelled.
    """
    ev = _events.get(run_id) if run_id else None
    if ev is None:
        return False
    if not ev.is_set():
        ev.set()
    return True


def cleanup(run_id: str) -> None:
    """Remove the event for a finished run. Safe on unknown ids."""
    if run_id:
        _events.pop(run_id, None)


def active_run_ids() -> list[str]:
    """All run_ids with a registered event. Useful for diagnostics."""
    return list(_events.keys())


async def await_cancellable(coro: Awaitable, run_id: str):
    """Await `coro` but raise `RunCancelled` if the run's cancel event fires.

    If `run_id` has no registered event, behaves exactly like `await coro`.
    On cancel, the underlying coroutine task is cancelled — for httpx calls
    that drops the connection cleanly so the upstream (Ollama / Codebox /
    OpenHands worker) stops the in-flight request.
    """
    ev = _events.get(run_id) if run_id else None
    if ev is None:
        return await coro

    if ev.is_set():
        # Already cancelled before we entered — close the unused coro and bail.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RunCancelled(run_id)

    work = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(ev.wait())
    try:
        done, _pending = await asyncio.wait(
            {work, waiter}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not waiter.done():
            waiter.cancel()
            try:
                await waiter
            except (asyncio.CancelledError, BaseException):
                pass

    if work in done:
        return work.result()

    # Cancel fired first — abort the work and surface RunCancelled
    work.cancel()
    try:
        await work
    except (asyncio.CancelledError, BaseException):
        pass
    raise RunCancelled(run_id)
