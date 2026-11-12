"""Supervisor-owned, once-per-request checklist allowance (never acceptance)."""
import json
import time


class FinalizationEnded(RuntimeError):
    category = 'plan_finalization_ended'
    status = 409


def state(store, identity):
    operation = store.get(identity)
    with store.connect() as db:
        rows = db.execute('SELECT id,payload FROM operations WHERE job_id=? ORDER BY started DESC', (operation['job_id'],))
        return next((value for row in rows if (value := json.loads(row['payload']).get('plan_finalization')) and value.get('operation_id') == row['id']), {})


def change(store, identity, action, plan=None):
    from coder_worker_runtime import _boundary
    operation = _boundary(store, identity)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        payload = json.loads(db.execute('SELECT payload FROM operations WHERE id=?', (identity,)).fetchone()[0])
        rows = db.execute('SELECT id,payload FROM operations WHERE job_id=? ORDER BY started DESC', (operation['job_id'],)).fetchall()
        value = next((v for row in rows if (v := json.loads(row['payload']).get('plan_finalization')) and v.get('operation_id') == row['id']), {})
        if action == 'begin':
            if value or payload.get('plan_finalization_version') != 1:
                return {'allowed': False, **value}
            response = db.execute("SELECT payload FROM events WHERE operation_id=? AND json_extract(payload,'$.type')='model_response' ORDER BY seq DESC LIMIT 1", (identity,)).fetchone()
            if not response or 'finish' not in json.loads(response[0]).get('data', {}).get('tool_names', []):
                return {'allowed': False, 'status': 'no_normal_finish'}
            remaining = payload['seconds_remaining'] - (time.time() - payload.get('_active_started', operation['started']))
            if payload['calls_remaining'] - operation['calls'] < 1 or remaining < 15:
                return {'allowed': False, 'status': 'skipped_allowance'}
            value = {'operation_id': identity, 'status': 'active', 'reminded': True,
                     'deadline': time.time() + min(60, remaining - 10), 'turns': 0}
        elif value.get('operation_id') != identity:
            return {'allowed': False}
        elif action == 'turn':
            if value.get('status') != 'active' or value['turns'] >= 2 or time.time() >= value['deadline']:
                raise FinalizationEnded('Checklist finalization limit reached; continue independent verification.')
            value['turns'] += 1
        elif action == 'acknowledge':
            from coder_progress import text
            acknowledged = [{'title': text(r.get('title')), 'status': r.get('status')}
                for r in (plan or [])[:30] if isinstance(r, dict) and r.get('status') in {'todo', 'in_progress', 'done'}]
            if value.get('acknowledged_plan') == acknowledged:
                return {'allowed': False, **value}
            value['acknowledged_plan'] = acknowledged
        elif action == 'end':
            value['status'] = 'ended'
        else:
            raise ValueError('Unknown checklist transition')
        payload['plan_finalization'] = value
        db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(payload), identity))
        db.execute('INSERT INTO events VALUES(?,COALESCE((SELECT MAX(seq)+1 FROM events WHERE operation_id=?),1),?)',
                   (identity, identity, json.dumps({'type': 'plan_finalization', 'at': time.time(),
                    'data': {**value, 'authority': 'supervisor'}})))
        return {'allowed': action == 'begin', **value}


def active(store, identity):
    value = state(store, identity)
    return value if value.get('operation_id') == identity and value.get('status') == 'active' else {}


def request(url, action):
    import requests
    response = requests.post(url.rstrip('/') + '/daedalus/plan-finalization', json={'action': action}, timeout=5)
    response.raise_for_status()
    return response.json()
