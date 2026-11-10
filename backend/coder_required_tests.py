"""Required tests: named project tests that passed once must keep passing for the rest of the job.

The set starts with the passing tests of the revision an edit starts from (its baseline check) and
grows with every newly passing test at each checkpoint. It never shrinks on its own: a required test
may be edited, but not removed, skipped or broken. A revision that keeps every required test passing
is eligible for verification. The modern controller repairs missing/failed tests within its existing
allowance and retains an incomplete candidate if unresolved; acceptance remains revision-bound.

Keys are ``<package cwd>::<test id>`` (ids from coder_test_ids), so a renamed check row or a changed
command line does not hide a lost test. Worker-shared: imports nothing from the backend.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import hashlib
import json

MAX_REQUIRED = 4000


def ui_flow_signature(flow, *, expectations=True):
    """Identity follows interactions, not array position; expectations stay reviewable.

    An assertion-value edit may be legitimate, so it retains the scenario identity
    but changes its full signature. Dropping a step or weakening equals to contains
    changes the scenario itself. A desktop label can use its changing text as locator.
    """
    value = {k: flow[k] for k in ('kind', 'command', 'file_path', 'url', 'toolkit') if k in flow}
    steps = []
    for original in flow.get('steps', []):
        step = {k: original[k] for k in ('action', 'selector', 'name', 'role', 'value', 'equals', 'contains') if k in original}
        if not expectations:
            if (flow.get('kind') == 'desktop' and step.get('action') == 'text'
                    and 'equals' in step and step.get('name') == step['equals'] and not step.get('selector')):
                step['name'] = {'expected_label': True}
            for comparison in ('equals', 'contains'):
                if comparison in step:
                    step[comparison] = {'reviewed_separately': True}
        steps.append(step)
    value['steps'] = steps
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ui_flow_changes(before, after):
    def indexed(flows):
        result = {}
        for flow in flows:
            result.setdefault(ui_flow_signature(flow, expectations=False), set()).add(ui_flow_signature(flow))
        return result
    old, new = indexed(before), indexed(after)
    return {'removed_scenarios': sorted(old.keys() - new.keys()),
            'added_scenarios': sorted(new.keys() - old.keys()),
            'changed_expectations': sorted(k for k in old.keys() & new.keys() if not old[k] <= new[k])}


def key(row, ident):
    return f"{row.get('cwd') or '.'}::{ident}"


def package(required_key):
    return required_key.split('::', 1)[0]


def project_tests(checks):
    return [row for row in checks if isinstance(row, dict) and row.get('is_test')
            and row.get('origin', 'project') in {'project', 'project_interaction'} and row.get('phase') != 'setup']


def observed(checks, placeholder_hashes=(), field='ids'):
    """{key: outcome} over every project test row.

    A row that runs an unreplaced skeleton placeholder file is ignored, even beside a real test file: the
    placeholder must be replaced, so its test can never become required. ``field='aggregates'`` reads the
    suite/parent results instead, which never become required themselves.
    """
    found = {}
    for row in project_tests(checks):
        files = {f.get('sha256') for f in row.get('test_files') or [] if isinstance(f, dict) and f.get('sha256')}
        if placeholder_hashes and files & set(placeholder_hashes):
            continue
        for ident, outcome in ((row.get('test_ids') or {}).get(field) or {}).items():
            name = key(row, ident)
            if found.get(name) != 'failed':
                found[name] = outcome
    return found


def passing(checks, placeholder_hashes=()):
    return sorted(name for name, outcome in observed(checks, placeholder_hashes).items() if outcome == 'passed')


def grow(required, checks, placeholder_hashes=()):
    return sorted(set(required) | set(passing(checks, placeholder_hashes)))[:MAX_REQUIRED]


def file_of(required_key):
    """The file a test id names, where the id says (pytest ``path::name``, cargo ``tests/x.rs::name``)."""
    cwd, _, ident = required_key.partition('::')
    head = ident.split('::', 1)[0] if '::' in ident else ''
    if not head or not PurePosixPath(head).suffix:
        return ''
    return head if cwd in ('', '.') else f'{cwd}/{head}'


GUARDS = ('page:', 'form:', 'workflow:', 'interface:')


def unbuilt(checks):
    """Packages whose tests never ran, read from the rows (results without run_revision's ``untested`` record).

    Only a failure that stops the test stage counts: the package's own setup, build or CLI launch, or for
    every package ('*') a main-copy service that did not start or an invalid .daedalus.json (it voids every
    package's configured commands, and a package only it named is gone). Guard rows on the disposable
    probe copy (page/form/workflow/labels) never stop the tests, so they excuse no deleted suite.
    """
    broken = set()
    for row in checks:
        ident = str(row.get('id') or '') if isinstance(row, dict) else ''
        if not ident or row.get('passed') or row.get('is_test') or row.get('probe_context') or ident.startswith(GUARDS):
            continue
        if ident.startswith('launch:') or ident == 'execution-config':
            broken.add('*')
        elif row.get('origin', 'project') == 'project' and row.get('phase') in ('setup', 'build', 'launch'):
            broken.add(row.get('cwd') or '.')
    return broken


def regressions(required, checks, superseded=(), untested=None):
    """Required tests this revision lost: ``[{'id', 'status'}]`` with status failed, skipped or missing.

    A package whose tests never ran (its setup, build or config failed, or a service did not launch) has
    no test run to judge: that failure belongs to the build gate. ``untested`` is run_revision's record of
    those packages ('*': none ran); without it the rows decide (unbuilt). Any other package without the test
    is missing it, whether the suite was deleted, the package moved, the test command was removed or
    narrowed, or its runner no longer reports test names. A test row that failed without reporting names
    fails all of its tests. A test that gained subtests of its own is still there (its aggregate result).
    """
    rows = project_tests(checks)
    ran = {row.get('cwd') or '.' for row in rows}
    seen, parents = observed(checks), observed(checks, field='aggregates')
    # A child the flaky re-run credited failed first, which is what failed its parent.
    credited = {key(row, ident) for row in rows for ident in row.get('flaky_tests') or []}
    unreported_failure = {row.get('cwd') or '.' for row in rows
                          if not ((row.get('test_ids') or {}).get('ids')) and not row.get('passed')}
    broken = unbuilt(checks) if untested is None else set(untested)
    excused = set(superseded)
    lost = []
    for name in required:
        if name in excused:
            continue
        if package(name) not in ran:
            if package(name) not in broken and '*' not in broken:
                lost.append({'id': name, 'status': 'missing'})
            continue
        outcome = seen.get(name)
        if outcome is None and name in parents:
            # Present while it passes, or while a test under it fails (judged by itself, and failing_parents keeps
            # the revision ineligible); failing with every test under it passing means its own body failed.
            under = name + ' > '
            child_failed = any(n.startswith(under) and (o == 'failed' or n in credited) for n, o in seen.items())
            outcome = 'passed' if parents[name] == 'passed' or parents[name] == 'failed' and child_failed else parents[name]
        if outcome == 'passed':
            continue
        if outcome is None and package(name) in unreported_failure:
            outcome = 'failed'
        lost.append({'id': name, 'status': outcome or 'missing'})
    return lost


def failing_parents(required, checks, superseded=()):
    """Required tests present only as a failing parent: not lost (a failing test under it may explain it), not passing.

    The parsed ids do not say whether the parent's own body failed as well (one bug failing both), so such a
    revision never becomes eligible: a later rollback must not restore a tree where a required test fails.
    """
    seen, parents = observed(checks), observed(checks, field='aggregates')
    return sorted(name for name in set(required) - set(superseded) if name not in seen and parents.get(name) == 'failed')


def judged(required, checks):
    """Whether every package holding a required test ran its tests on this revision (else it cannot become eligible)."""
    ran = {row.get('cwd') or '.' for row in project_tests(checks)}
    return all(package(name) in ran for name in required)


def rerun_packages(lost):
    """Packages with a newly FAILING required test get one re-run before it counts (flaky tests)."""
    return sorted({package(row['id']) for row in lost if row['status'] == 'failed'})


def describe(lost, limit=8):
    shown = ', '.join(f"{row['id'].split('::', 1)[1]} ({row['status']})" for row in lost[:limit])
    return shown + (f' and {len(lost) - limit} more' if len(lost) > limit else '')
