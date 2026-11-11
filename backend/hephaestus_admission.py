"""Bounded waiting for the VM admission lock.

New work takes the lock shared; maintenance takes it exclusive. Both used to try once and fail at once,
so a job that arrived during a short maintenance step died with a bare "Errno 11" before anything ran.
Waiting here is safe because nothing has been dispatched yet: no container, no reservation, no ledger row.
"""
from contextlib import ExitStack, contextmanager
import time

from hephaestus_inventory import lock


class AdmissionBusy(BlockingIOError):
    """The lock stayed held for the whole allowance. Nothing was dispatched."""


class AdmissionAbandoned(RuntimeError):
    """The caller went away or was cancelled while waiting. Nothing was dispatched."""


@contextmanager
def admitted(root, *, name='admission', exclusive=False, seconds=60.0, alive=None, poll=.2,
             clock=time.monotonic, sleep=time.sleep):
    """Hold the lock, having waited at most `seconds` for it. Yields the seconds spent waiting.

    `alive` is polled between attempts; when it returns False the wait ends with AdmissionAbandoned.
    The wait never extends a caller's own deadline: pass the time that deadline has left.
    """
    started = clock(); deadline = started + max(0.0, seconds)
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(lock(root, name, exclusive=exclusive, blocking=False))
                break
            except BlockingIOError:
                if alive is not None and not alive():
                    raise AdmissionAbandoned('Stopped while waiting for VM maintenance') from None
                remaining = deadline - clock()
                if remaining <= 0:
                    raise AdmissionBusy(11, f'VM maintenance held admission for {clock() - started:.0f}s; nothing was dispatched') from None
                sleep(min(poll, remaining))
        yield clock() - started
