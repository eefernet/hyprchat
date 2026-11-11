"""Bound the builder's own activity summary before it crosses from the sandboxed child to the parent.

The summary is written by code running beside the model, so the parent treats it as untrusted: only this
fixed shape is forwarded, and nothing in it can set usage, status, acceptance, budgets or identity.
"""
import re

TOOLS, NAME, RECENT, COUNT = 32, 64, 12, 1_000_000
_NAME = re.compile(r'[^A-Za-z0-9_.:-]')


def _name(value):
    return _NAME.sub('_', str(value))[:NAME]


def _count(value):
    return min(value, COUNT) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def bounded(activity):
    """{'tool_counts', 'recent_tools', 'trailing_task_tracker'} with fixed sizes, or None when unusable."""
    if not isinstance(activity, dict):
        return None
    counts = activity.get('tool_counts')
    recent = activity.get('recent_tools')
    result = {
        'tool_counts': {_name(k): _count(v) for k, v in list(counts.items())[:TOOLS]} if isinstance(counts, dict) else {},
        'recent_tools': [_name(v) for v in recent[-RECENT:]] if isinstance(recent, list) else [],
        'trailing_task_tracker': _count(activity.get('trailing_task_tracker')),
    }
    return result if result['tool_counts'] or result['recent_tools'] else None
