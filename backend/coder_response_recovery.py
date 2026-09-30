"""Restart-persistent response-format attempts, scoped to an authoring step."""
import json

from coder_verification import digest


class ResponseFormatError(ValueError):
    def __init__(self, message, *, response=None, failure_category='response_format'):
        super().__init__(message)
        self.response = response
        self.failure_category = failure_category


def contract_identity(operation):
    """Logical inputs survive Continue, cache reuse and regenerated log paths."""
    payload = operation['payload']
    return {'kind': operation['kind'], 'task': payload.get('original_task', payload['task']),
        'revision': payload.get('revision_id'), 'baseline': payload.get('baseline_revision'),
        'requirements': payload.get('requirements'), 'previous': payload.get('diagnose_probes'),
        'audit_key': (payload.get('evidence') or {}).get('audit_key')}


class Recovery:
    modes = ('schema', 'schema', 'json', 'text')

    def __init__(self, store, operation_id, instruction, extra, schema):
        self.store, self.operation_id = store, operation_id
        op = store.get(operation_id)
        payload = op['payload']
        identity = ({'contract': contract_identity(op), 'step': extra['_response_step']}
            if '_response_step' in extra else {'kind': op['kind'],
                'task': payload.get('original_task', payload['task']), 'revision': payload.get('revision_id'),
                'baseline': payload.get('baseline_revision'), 'extra': extra})
        key = digest({**identity, 'instruction': instruction, 'schema': schema})
        directory = store.root / 'jobs' / op['job_id'] / 'response-steps'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (key + '.json')
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'failures': 0, 'observations': [], 'inspections': [], 'repeats': {}, 'checkpoint': ''}
        # An interrupted request consumes its slot; Continue cannot reset it.
        if self.state.pop('in_flight', False):
            self.reject('Response interrupted before validation', None)

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state))
        temporary.replace(self.path)

    def before_call(self):
        index = self.state['failures']
        if index >= len(self.modes):
            raise ResponseFormatError('Response-format recovery exhausted: schema, corrective schema, JSON and text attempts retained')
        mode = self.modes[index]
        payload = self.store.get(self.operation_id)['payload']
        self.store.update(self.operation_id, payload={**payload, 'structured_mode': mode})
        self.state['in_flight'] = True
        self.save()

    def reject(self, error, response, failure_category='response_format'):
        self.state.pop('in_flight', None)
        self.state['failures'] += 1
        self.state['observations'].append({'invalid_response': response, 'error': str(error),
            'failure_category': failure_category,
            'instruction': 'Correct the response while preserving requirements. Return only required fields without repetition. A valid negative verdict is allowed.'})
        self.save()
        index = self.state['failures']
        self.store.event(self.operation_id, 'response_format_recovery', step_id=self.path.stem,
            failure_category=failure_category, rejected_response=response, error=str(error),
            attempt=index, next_mode=self.modes[index] if index < len(self.modes) else 'exhausted')

    def observed(self):
        self.state.pop('in_flight', None)
        self.save()

    def accepted(self, answer):
        self.state['answer'] = answer
        self.observed()
