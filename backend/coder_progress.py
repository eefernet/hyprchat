"""Bounded public progress; never projects SDK thoughts, file contents or source text.
Terminal commands are projected only through display_command (secret-stripped, first line, <=120 chars)."""
import copy
import hashlib
import re
from pathlib import PurePosixPath

VERSION = 1
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', 'vendor', '__pycache__', '.pytest_cache', 'dist', 'build', '.cache'}
VERBS = {'terminal': 'run', 'task_tracker': 'plan', 'prepare_environment': 'prepare', 'ui': 'check_ui', 'ui_tool': 'check_ui'}
FILE_VERBS = {'view': 'read', 'create': 'create', 'str_replace': 'edit', 'insert': 'edit', 'undo_edit': 'undo'}
RECENT = 20
MASK = '•••'
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
SPACES = re.compile(r'\s+')
URL_USERINFO = re.compile(r'(\w+://)[^/\s:@]+:[^/\s@]+@')
LEADING_ENV = re.compile(r'^(?:(?:export|env)\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S*)\s+)+')
SECRET_ASSIGN = re.compile(r'\b([A-Za-z_]*(?:TOKEN|SECRET|KEY|PASS(?:WORD|WD)?|AUTH|COOKIE|CREDENTIAL)[A-Za-z0-9_]*)=(?:"[^"]*"|\'[^\']*\'|\S+)', re.I)
# Word flags match any case; the bare curl header flag only as -H so `pytest -h` keeps its help flag.
SECRET_FLAG = re.compile(r'(--?(?i:password|passwd|pass|token|api[-_]?key|secret|auth(?:orization)?|bearer|credentials?|cookie|header)\b|-H\b)[\s=].*$')
TOKENS = tuple(re.compile(p) for p in (
    r'\b(?:ghp|gho|ghu|ghs|glpat|xox[abprs]|sk|rk|pk)[-_][A-Za-z0-9_\-]{8,}', r'AKIA[0-9A-Z]{16}',
    r'eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}', r'\b[A-Fa-f0-9]{32,}\b',
    # Long base64-ish runs, but never a path segment: no slashes inside and not glued to one.
    r'(?<![\w/.\-])[A-Za-z0-9+_\-]{40,}={0,2}(?![\w/.\-])'))


def text(value, limit=240):
    return ''.join(c for c in str(value or '') if c >= ' ' and c != '\x7f')[:limit]


def relative(value, root=''):
    value = text(value, 1024)
    if root and value.startswith(root.rstrip('/') + '/'): value = value[len(root.rstrip('/'))+1:]
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or any(p in EXCLUDED or p.startswith('.daedalus-publish-') for p in path.parts): return ''
    return str(path)


def display_command(value, limit=120):
    """First line only, control/ANSI-free, env prefix dropped, secrets masked, bounded."""
    line = text(ANSI.sub('', str(value or '').replace('\r', '\n').split('\n', 1)[0]), 4000)
    line = SPACES.sub(' ', line).strip()
    line = URL_USERINFO.sub(r'\1' + MASK + '@', line)
    line = LEADING_ENV.sub('', line)
    line = SECRET_ASSIGN.sub(r'\1=' + MASK, line)
    line = SECRET_FLAG.sub(r'\1 ' + MASK, line)
    for pattern in TOKENS: line = pattern.sub(MASK, line)
    return line if len(line) <= limit else line[:limit-1] + '…'


def empty():
    return {'version': VERSION, 'plan': [], 'current': None, 'files': [], 'files_changed_at': None, 'recent': []}


def _result(ref, at, outcome, exit_code=None):
    return {'id': ref + ':done', 'kind': 'tool_result', 'ref': ref, 'at': at, 'outcome': outcome, 'exit_code': exit_code if isinstance(exit_code, int) else None}


def project(previous, batch, operation, root='', stage='coding'):
    progress = copy.deepcopy(previous or empty())
    records = []
    for entry in batch:
        kind, data = entry.get('type'), entry.get('data') or {}
        at = entry.get('created_at') or entry.get('at') or entry.get('time')
        event_id = operation + ':' + str(entry['seq'])
        current = progress.get('current') or {}
        if kind == 'agent':
            event = data.get('event') or {}
            event_kind = event.get('kind')
            action = event.get('action') or {}
            observation = event.get('observation') or {}
            tool = text(event.get('tool_name'), 80)
            if event_kind == 'ActionEvent':
                call_id = text(event.get('tool_call_id'), 180)
                if tool == 'terminal' and not action.get('command') and current.get('kind') == 'command':
                    # Polling the running command keeps its identity and command; it is not a new call.
                    progress['current'] = {**current, 'call_id': call_id}
                else:
                    identity = operation + ':' + text(event.get('id') or event.get('tool_call_id'), 180)
                    activity = 'ui_preflight' if action.get('action') == 'check_flows' and tool in {'ui', 'ui_tool'} else 'command' if tool == 'terminal' else 'preparation' if tool == 'prepare_environment' else 'interface' if tool in {'ui', 'ui_tool'} else 'plan' if tool == 'task_tracker' else 'tool'
                    path = relative(action.get('path'), root)
                    if path and action.get('command') in {'create', 'str_replace', 'insert', 'undo_edit'}:
                        activity = 'file'
                    verb = FILE_VERBS.get(action.get('command'), 'edit') if tool == 'file_editor' else VERBS.get(tool, 'inspect')
                    shown = path if tool == 'file_editor' or activity == 'file' else ''
                    progress['current'] = {'id': identity, 'call_id': call_id, 'kind': activity, 'tool': tool, 'verb': verb, 'stage': stage, 'path': shown,
                        'action': 'create' if action.get('command') == 'create' else 'edit', 'started_at': current.get('started_at', at) if current.get('id') == identity else at}
                    record = {'id': identity, 'kind': 'tool_call', 'at': at, 'stage': stage, 'tool': tool, 'verb': verb}
                    if verb == 'run': progress['current']['command'] = record['command'] = display_command(action.get('command'))
                    if shown: record['path'] = shown
                    records.append(record)
            elif event_kind in {'ObservationEvent', 'AgentErrorEvent', 'UserRejectObservation'}:
                if tool == 'task_tracker' and not observation.get('is_error') and isinstance(observation.get('task_list'), list):
                    progress['plan'] = [{'id': str(i), 'title': text(row.get('title')), 'status': row.get('status') if row.get('status') in {'todo','in_progress','done'} else 'todo'}
                        for i, row in enumerate(observation['task_list'][:30]) if isinstance(row, dict) and text(row.get('title'))]
                    progress['plan_updated_at'] = at
                    records.append({'id': operation + ':' + text(event.get('id') or entry['seq'], 180), 'kind': 'plan', 'at': at, 'plan': progress['plan'], 'authority': 'agent_reported'})
                matched = current and current.get('call_id') == event.get('tool_call_id')
                pending = tool == 'terminal' and observation.get('exit_code') in {None, -1} and not observation.get('is_error') and event_kind == 'ObservationEvent'
                if matched and not pending:
                    failed = bool(observation.get('is_error')) or event_kind != 'ObservationEvent'
                    if current.get('kind') == 'file' and failed:
                        records.append({'id': current['id'], 'kind': 'file', 'path': current['path'], 'action': current['action'], 'outcome': 'failed', 'at': at})
                    records.append(_result(current['id'], at, 'failed' if failed else 'succeeded', observation.get('exit_code') if (current.get('tool') or tool) == 'terminal' else None))
                    progress['current'] = None
        elif kind == 'source_activity':
            path = relative(data.get('path'))
            if path and data.get('action') in {'create', 'edit', 'delete'}:
                records.append({'id': text(data.get('id'), 240) or event_id, 'kind': 'file', 'path': path,
                    'action': data['action'], 'outcome': data.get('outcome') if data.get('outcome') in {'succeeded','failed','interrupted'} else 'succeeded', 'at': at})
        elif kind == 'check_started':
            check_id = text(data.get('id'), 120)
            progress['current'] = {'id': event_id, 'kind': 'command', 'tool': 'check', 'verb': 'check', 'stage': stage, 'command': display_command(data.get('command')),
                'check_id': check_id, 'phase': text(data.get('phase'), 20), 'started_at': at}
            records.append({'id': event_id, 'kind': 'tool_call', 'at': at, **{k: progress['current'][k] for k in ('stage', 'tool', 'verb', 'command', 'check_id', 'phase')}})
            # The worker reports every check row after the whole run, so remember started checks beyond `current`.
            progress['check_calls'] = {**dict(list((progress.get('check_calls') or {}).items())[-49:]), check_id: event_id}
        elif kind == 'check':
            row = data.get('check') if isinstance(data.get('check'), dict) else data
            check_id = text(row.get('id'), 120)
            calls = progress.get('check_calls') or {}
            ref = current['id'] if current.get('verb') == 'check' and check_id == current.get('check_id') else calls.get(check_id)
            if ref:
                records.append(_result(ref, at, 'succeeded' if row.get('passed') else 'failed', row.get('exit_code')))
                calls.pop(check_id, None); progress['check_calls'] = calls
                if ref == current.get('id'): progress['current'] = None
        elif kind == 'model_call':
            # Never replaces a pending command or file: the poll loop calls the model between polls.
            if not current or current.get('kind') == 'model':
                progress['current'] = {'id': event_id, 'kind': 'model', 'tool': 'model', 'verb': 'think', 'stage': stage, 'started_at': at}
        elif kind in {'model_response', 'model_failure'}:
            if current.get('kind') == 'model': progress['current'] = None
        elif kind == 'preparation_started':
            progress['suspended'] = current or None
            progress['current'] = {'id': event_id, 'kind': 'preparation', 'stage': stage, 'started_at': at}
        elif kind == 'environment_preparation':
            progress['current'] = progress.pop('suspended', None)
        elif kind == 'plan_finalization' and data.get('authority') == 'supervisor':
            progress['finalization'] = {k: data[k] for k in ('status','reminded','turns','deadline','acknowledged_plan') if k in data}
            records.append({'id': event_id, 'kind': 'plan_finalization', 'at': at, **progress['finalization']})
        elif kind == 'ui_preflight':
            # Child diagnostics never enter delivered checks or establish acceptance.
            status = data.get('status')
            if status == 'running':
                progress['current'] = {'id': event_id, 'kind': 'ui_preflight', 'stage': stage, 'started_at': at}
            else:
                if current.get('kind') == 'ui_preflight': progress['current'] = None
                progress['ui_preflight'] = {k: data[k] for k in ('status', 'passed', 'fingerprint', 'diagnostic_only', 'reused', 'reason', 'flows', 'errors') if k in data}
                records.append({'id': event_id, 'kind': 'ui_preflight', 'at': at, **progress['ui_preflight']})
        elif kind == 'finished':
            if current.get('kind') == 'file':
                records.append({'id': current['id'], 'kind': 'file', 'path': current['path'], 'action': current['action'], 'outcome': 'interrupted', 'at': at})
            if current and current.get('kind') != 'model':
                records.append(_result(current['id'], at, 'interrupted'))
            progress['current'] = None
            progress.pop('suspended', None)
            progress.pop('check_calls', None)
        if progress != previous: progress['updated_at'] = at
    for row in records:
        if row['kind'] != 'file': continue
        progress['files'] = [row] + [old for old in progress.get('files', []) if old['id'] != row['id']]
        progress['files'] = progress['files'][:50]
        if row['outcome'] != 'failed': progress['files_changed_at'] = row['at']
    recent = [r for r in reversed(records) if r['kind'] in {'tool_call', 'tool_result'}] + list(progress.get('recent') or [])
    seen = set()
    progress['recent'] = [r for r in recent if not (r['id'] in seen or seen.add(r['id']))][:RECENT]
    return progress, records
