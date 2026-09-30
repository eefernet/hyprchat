"""Layers of a policy-7 verification: which failure to fix first, and how far a build got.

The 2026-09-24 proposal asked for four durable build milestones. The controller already has the
boundaries (setup/build preflight between builder passes, checks on an immutable copy, browser guards
after launch); what it lacked was an ORDER. A model that receives "README missing", "audit assertion
failed" and "server exits on start" as one flat list repairs the README first. This module ranks every
check row by the layer it belongs to, so the controller can hand a non-starting application back to the
builder before authoring an audit, and repair feedback can say what to fix first.

Nothing here is evidence: a layer is a label on an existing row.
"""
from __future__ import annotations

import re

LAYERS = {1: 'foundation', 2: 'core behavior', 3: 'interface integration', 4: 'delivery'}
FOUNDATION_PHASES = {'setup', 'build'}
# A requested command that cannot even start (missing entry file, class or module) is a foundation
# failure; one that starts and misbehaves is core behavior.
CANNOT_START = re.compile(r"No such file|can't open file|cannot find|not found|Could not find or load main class|"
                          r"MSB1003|MSB1009|No module named|Cannot find module|command not found|not recognized|"
                          r"no such command|Permission denied", re.I)
DELIVERY_KINDS = {'documentation', 'tests', 'review'}


def layer(check):
    """1 foundation, 2 core behavior, 3 interface integration, 4 delivery."""
    ident = str(check.get('id') or '')
    if check.get('phase') in FOUNDATION_PHASES or ident in {'execution-contract', 'execution-config'} \
            or ident.startswith(('launch:', 'page:', 'probe:test-project')):
        return 1
    if ident.startswith(('probe:invalid:', 'probe:missing:')):
        return 2   # the program started for valid input; how it answers bad input is core behavior, even on exit 127
    if ident.startswith('probe:command:'):
        return 1 if check.get('exit_code') in (126, 127) or CANNOT_START.search(check.get('log_tail') or '') else 2
    if ident.startswith(('form:', 'workflow:', 'interface:labels')) or check.get('origin') == 'independent' or check.get('phase') == 'visual':
        return 3
    if ident.startswith('protected:') or check.get('kind') == 'file' or 'documentation' in (check.get('evidence_types') or []) \
            or (check.get('outcome') and ident.rsplit(':', 1)[-1] in DELIVERY_KINDS):
        return 4
    return 2


def progress(checks):
    """The lowest layer with a blocking failure at this revision, or 4/4 complete when nothing fails."""
    from coder_policy7_evidence import blocking
    rows = list(checks or [])
    failing = [c for c in rows if blocking(c)]
    if not rows:
        return {'phase': 1, 'of': 4, 'label': LAYERS[1], 'blocked_by': [], 'complete': False}
    if not failing:
        return {'phase': 4, 'of': 4, 'label': LAYERS[4], 'blocked_by': [], 'complete': True}
    lowest = min(layer(c) for c in failing)
    return {'phase': lowest, 'of': 4, 'label': LAYERS[lowest], 'complete': False,
            'blocked_by': [str(c.get('id')) for c in failing if layer(c) == lowest][:6]}


ORIGIN_RANK = {'controller': 0, 'project': 1, 'independent': 2}


def ordered(defects):
    """Defects sorted lowest layer first; controller rows, then project rows, then model-written audits within a layer."""
    return sorted(defects or [], key=lambda d: (layer(d), ORIGIN_RANK.get(d.get('origin'), 1), str(d.get('id') or '')))
