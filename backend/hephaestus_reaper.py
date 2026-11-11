"""Containers whose stop could not be confirmed.

`docker rm -f` can fail or time out. The container may then still be alive and still writing, so its
memory reservation is kept: releasing it would admit new work past the 5 GiB ceiling. Before this module
the reservation was only ever recovered by restarting the broker, and later work queued until its
deadline. Here the broker retries the removal and frees the reservation once, and only once, the
container is confirmed gone.
"""
import subprocess
import time

INTERVAL = 10.0


def remember(broker, identity, *, group, memory, unit, reserved, outcome):
    broker.unconfirmed[identity] = {'group': group, 'memory': memory, 'unit': unit, 'reserved': reserved, 'outcome': outcome}


def reap(broker, *, run=subprocess.run, clock=time.monotonic, force=False):
    """Retry removal of unconfirmed containers; returns the identities confirmed gone on this pass."""
    with broker.condition:                       # claim the entries so two threads never release one reservation twice
        if not broker.unconfirmed or (not force and clock() - broker.reaped_at < INTERVAL):
            return []
        broker.reaped_at = clock()
        claimed = dict(broker.unconfirmed); broker.unconfirmed.clear()
    confirmed = []
    for identity, entry in claimed.items():
        gone = False
        try:
            removal = run(['docker', 'rm', '-f', 'hephaestus-' + identity], capture_output=True, text=True, timeout=30)
            gone = not removal.returncode or 'No such container' in removal.stderr
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not gone:
            with broker.condition: broker.unconfirmed[identity] = entry
            continue
        # Confirmed absent: only now may the ledger close and the reservation return.
        broker.update(identity, 'failed', {**entry['outcome'], 'exit_code': entry['outcome'].get('exit_code') or 125,
                                           'stop_confirmed_late': True})
        if entry['reserved']:
            broker.release(entry['group'], entry['memory'], entry['unit'])
        confirmed.append(identity)
    return confirmed
