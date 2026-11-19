#!/usr/bin/env python3
"""Temporarily avoid failed VPN entry addresses without discarding profiles."""
import json
import os
from pathlib import Path
import sys
import time

COOLDOWN_SECONDS = 24 * 60 * 60


def load_failures(path):
    try:
        data = json.loads(Path(path).read_text())
        return {str(k): v for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, AttributeError):
        return {}


def remote(path):
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == 'remote':
            return fields[1]
    return ''


def order_candidates(paths, failures, now):
    ready, cooling = [], []
    for path in paths:
        until = failures.get(remote(path), 0)
        (cooling if until > now else ready).append((until, path))
    # Normal operation never retries a known-bad entry during its cooldown.
    # If every entry is cooling, retry oldest failures so recovery is possible.
    return [path for _, path in (ready or sorted(cooling))]


def update_failure(path, address, failed, now):
    path = Path(path)
    failures = {k: v for k, v in load_failures(path).items() if v > now}
    if failed:
        failures[address] = now + COOLDOWN_SECONDS
    else:
        failures.pop(address, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(failures, handle)
    os.replace(temporary, path)


def main():
    action, state = sys.argv[1:3]
    now = time.time()
    if action == 'order':
        paths = [p for p in sys.stdin.read().splitlines() if p]
        print('\n'.join(order_candidates(paths, load_failures(state), now)))
    elif action in ('fail', 'success'):
        update_failure(state, sys.argv[3], action == 'fail', now)
    else:
        raise SystemExit('usage: vpn_candidates.py order|fail|success STATE [ADDRESS]')


if __name__ == '__main__':
    main()
