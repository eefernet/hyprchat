"""Bounded public progress; never projects SDK thoughts, arguments or source text."""
import copy
import hashlib
from pathlib import PurePosixPath

VERSION = 1
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', 'vendor', '__pycache__', '.pytest_cache', 'dist', 'build', '.cache'}


def text(value, limit=240):
    return ''.join(c for c in str(value or '') if c >= ' ' and c != '\x7f')[:limit]


def relative(value, root=''):
    value = text(value, 1024)
    if root and value.startswith(root.rstrip('/') + '/'): value = value[len(root.rstrip('/'))+1:]
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or any(p in EXCLUDED or p.startswith('.daedalus-publish-') for p in path.parts): return ''
    return str(path)


def empty():
    return {'version': VERSION, 'plan': [], 'current': None, 'files': [], 'files_changed_at': None}


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
                identity = operation + ':' + text(event.get('id') or event.get('tool_call_id'), 180)
                if tool == 'terminal' and not action.get('command') and current.get('kind') == 'command':
                    identity = current['id']
                activity = 'command' if tool == 'terminal' else 'preparation' if tool == 'prepare_environment' else 'interface' if tool in {'ui', 'ui_tool'} else 'plan' if tool == 'task_tracker' else 'tool'
                path = relative(action.get('path'), root)
                if path and action.get('command') in {'create', 'str_replace', 'insert', 'undo_edit'}:
                    activity = 'file'
                progress['current'] = {'id': identity, 'call_id': text(event.get('tool_call_id'), 180),
                    'kind': activity, 'stage': stage, 'path': path if activity == 'file' else '',
                    'action': 'create' if action.get('command') == 'create' else 'edit', 'started_at': current.get('started_at', at) if current.get('id') == identity else at}
            elif event_kind in {'ObservationEvent', 'AgentErrorEvent', 'UserRejectObservation'}:
                if tool == 'task_tracker' and not observation.get('is_error') and isinstance(observation.get('task_list'), list):
                    progress['plan'] = [{'id': str(i), 'title': text(row.get('title')), 'status': row.get('status') if row.get('status') in {'todo','in_progress','done'} else 'todo'}
                        for i, row in enumerate(observation['task_list'][:30]) if isinstance(row, dict) and text(row.get('title'))]
                    progress['plan_updated_at'] = at
                    records.append({'id': operation + ':' + text(event.get('id') or entry['seq'], 180), 'kind': 'plan', 'at': at, 'plan': progress['plan'], 'authority': 'agent_reported'})
                matched = current and current.get('call_id') == event.get('tool_call_id')
                pending = tool == 'terminal' and observation.get('exit_code') in {None, -1} and not observation.get('is_error') and event_kind == 'ObservationEvent'
                if matched and not pending:
                    if current.get('kind') == 'file' and (observation.get('is_error') or event_kind != 'ObservationEvent'):
                        records.append({'id': current['id'], 'kind': 'file', 'path': current['path'], 'action': current['action'], 'outcome': 'failed', 'at': at})
                    progress['current'] = None
        elif kind == 'source_activity':
            path = relative(data.get('path'))
            if path and data.get('action') in {'create', 'edit', 'delete'}:
                records.append({'id': text(data.get('id'), 240) or event_id, 'kind': 'file', 'path': path,
                    'action': data['action'], 'outcome': data.get('outcome') if data.get('outcome') in {'succeeded','failed','interrupted'} else 'succeeded', 'at': at})
        elif kind == 'preparation_started':
            progress['suspended'] = current or None
            progress['current'] = {'id': event_id, 'kind': 'preparation', 'stage': stage, 'started_at': at}
        elif kind == 'environment_preparation':
            progress['current'] = progress.pop('suspended', None)
        elif kind == 'plan_finalization' and data.get('authority') == 'supervisor':
            progress['finalization'] = {k: data[k] for k in ('status','reminded','turns','deadline','acknowledged_plan') if k in data}
            records.append({'id': event_id, 'kind': 'plan_finalization', 'at': at, **progress['finalization']})
        elif kind == 'finished':
            if current.get('kind') == 'file':
                records.append({'id': current['id'], 'kind': 'file', 'path': current['path'], 'action': current['action'], 'outcome': 'interrupted', 'at': at})
            progress['current'] = None
            progress.pop('suspended', None)
        if progress != previous: progress['updated_at'] = at
    for row in records:
        if row['kind'] != 'file': continue
        progress['files'] = [row] + [old for old in progress.get('files', []) if old['id'] != row['id']]
        progress['files'] = progress['files'][:50]
        if row['outcome'] != 'failed': progress['files_changed_at'] = row['at']
    return progress, records
