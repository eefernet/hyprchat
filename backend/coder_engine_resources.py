"""Delegated cgroup-v2 operation limits inside the isolated worker service."""
import os
from pathlib import Path
import re


def group_root():
    if os.environ.get('DAEDALUS_CGROUP_LIMITS') != '1': return None
    record = next(line for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    current = Path('/sys/fs/cgroup') / record[3:].lstrip('/')
    # The worker creates only these two levels below its delegated service.
    if current.name == 'control': return current.parent
    if current.parent.name == 'operations': return current.parent.parent
    return current


def initialize():
    root = group_root()
    if root is None: return
    control = root / 'control'; control.mkdir(exist_ok=True)
    (control / 'cgroup.procs').write_text(str(os.getpid()))
    (root / 'cgroup.subtree_control').write_text('+memory +pids')
    operations = root / 'operations'; operations.mkdir(exist_ok=True)
    (operations / 'cgroup.subtree_control').write_text('+memory +pids')


def enter_operation(identity):
    root = group_root()
    if root is None: return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', identity): raise ValueError('Invalid operation cgroup identity')
    group = root / 'operations' / identity; group.mkdir(exist_ok=True)
    for key, value in {'memory.max': str(3*1024**3), 'memory.high': str(2560*1024**2),
                       'pids.max': '256', 'memory.oom.group': '1'}.items():
        (group / key).write_text(value)
    (group / 'cgroup.procs').write_text(str(os.getpid()))
    return str(group)


def failure(group):
    """Read kernel evidence even if the operation could not write its final result."""
    if not group: return None
    path = Path(group)
    if not path.is_relative_to('/sys/fs/cgroup'): return None
    events = {}
    for name in ('memory.events', 'pids.events'):
        try:
            events[name] = dict((key, int(value)) for key, value in
                                (line.split() for line in (path / name).read_text().splitlines()))
        except FileNotFoundError: continue
    if events.get('memory.events', {}).get('oom_kill', 0):
        return {'failure_category': 'resource_limit', 'error': 'Operation exceeded its memory limit; its process group was terminated', 'resource_events': events}
    if events.get('pids.events', {}).get('max', 0):
        return {'failure_category': 'resource_limit', 'error': 'Operation exceeded its process limit', 'resource_events': events}
    return None
