"""Current-revision check rows; optional diagnostics are never executed evidence."""
from copy import deepcopy


def normalize(check):
    row = deepcopy(check)
    if row.get('workflow_status') == 'advisory' and not row.get('environment_fault'):
        row.update(passed=False, execution_succeeded=False, optional=True,
                   classification='advisory', outcome='advisory')
        row['reason'] = row.get('reason') or row.get('log_tail') or 'The workflow probe was inconclusive.'
    else:
        row['outcome'] = ('advisory' if row.get('optional') and not row.get('passed') and not row.get('environment_fault')
                          else 'skipped' if row.get('skipped') else 'unverified' if row.get('status') == 'unverified'
                          else 'passed' if row.get('passed') else 'failed')
    if row['outcome'] != 'passed':
        row.update(passed=False, execution_succeeded=False)
    return row


def current(checks, revision):
    latest = {}
    for index, check in enumerate(checks):
        if check.get('revision_id') == revision:
            latest[check.get('id', ('anonymous', index))] = normalize(check)
    return list(latest.values())


def counts(checks):
    return {key: sum(c['outcome'] == key for c in checks)
            for key in ('passed', 'failed', 'advisory', 'skipped', 'unverified')}
