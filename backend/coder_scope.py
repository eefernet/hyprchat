"""Explicit, durable reconciliation of accepted requirements with a follow-up."""
from __future__ import annotations

import re
import json

VERSION = 1


class ScopeClarification(ValueError):
    pass


def effective_task(job):
    original = job.get('user_task', job.get('task', ''))
    notes = job.get('scope_clarifications') or []
    if not notes:
        return original
    return original + '\n\nUSER CLARIFICATIONS (latest takes precedence):\n' + '\n'.join(n['text'] for n in notes)


def parents(inherited):
    return [{**({'text': row} if isinstance(row, str) else row),
             'id': row.get('id', f'o{i+1}') if isinstance(row, dict) else f'o{i+1}'}
            for i, row in enumerate(inherited)]


def reconcile(answer, request, inherited):
    if not inherited:
        return [], []
    prior = parents(inherited)
    rows = answer.get('inheritance')
    if isinstance(rows, dict):
        if any(not isinstance(row, dict) for row in rows.values()):
            raise ValueError('Every inheritance decision must be an object')
        rows = [{**row, 'parent_id': name} for name, row in rows.items()]
    if not isinstance(rows, list):
        raise ValueError('Follow-up brief needs an inheritance disposition for every parent outcome')
    known = {p['id']: p for p in prior}
    decisions = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('parent_id') not in known or row['parent_id'] in decisions:
            raise ValueError('Inheritance must identify each parent outcome exactly once')
        action = row.get('action')
        if action not in {'retain', 'replace', 'remove', 'clarify'}:
            raise ValueError('Inheritance action must be retain, replace, remove, or clarify')
        if action == 'clarify':
            raise ScopeClarification(str(row.get('question') or 'Clarify how this earlier requirement should change: ' + known[row['parent_id']]['text']))
        quote = row.get('request_quote', '')
        if action in {'replace', 'remove'}:
            if not isinstance(quote, str) or not quote.strip() or quote not in request:
                raise ValueError('A replaced or removed requirement needs an exact quote from the current request')
        replacements = row.get('replacement_outcomes', [])
        if action == 'replace' and (not isinstance(replacements, list) or not replacements or any(
                type(i) is not int or not 1 <= i <= len(answer['outcomes']) for i in replacements)):
            raise ValueError('Replacement outcomes must be one-based indexes into the new outcomes list')
        decisions[row['parent_id']] = {'parent_id': row['parent_id'], 'parent_outcome': known[row['parent_id']],
                                     'action': action, 'request_quote': quote,
                                     'replacement_outcomes': replacements if action == 'replace' else []}
    if set(decisions) != set(known):
        raise ValueError('Inheritance omitted parent outcomes: ' + ', '.join(set(known) - set(decisions)))
    kept = [p for p in prior if decisions[p['id']]['action'] == 'retain']
    return kept, [decisions[p['id']] for p in prior]


def plan_schema(request, inherited):
    """Constrain citations to actual user text and dispositions to known parent IDs."""
    quotes = list(dict.fromkeys(['', request, *(s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', request) if s.strip())]))
    row = {'type': 'object', 'required': ['action', 'request_quote', 'replacement_outcomes', 'question'], 'properties': {
        'action': {'type': 'string', 'enum': ['retain', 'replace', 'remove', 'clarify']},
        'request_quote': {'type': 'string', 'enum': quotes},
        'replacement_outcomes': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
        'question': {'type': 'string'}}, 'additionalProperties': False}
    ids = [p['id'] for p in parents(inherited)]
    return {'type': 'object', 'required': ['project_name', 'outcomes', 'batches', 'inheritance'], 'properties': {
        'project_name': {'type': 'string'},
        'outcomes': {'type': 'array', 'items': {'type': 'object', 'required': ['text', 'component', 'evidence_types'], 'properties': {
            'text': {'type': 'string'}, 'component': {'type': 'string'}, 'evidence_types': {'type': 'array', 'items': {
                'type': 'string', 'enum': ['behavior', 'documentation', 'tests', 'preservation']}}}}},
        'batches': {'type': 'array', 'items': {'type': 'object', 'required': ['task', 'files'], 'properties': {
            'task': {'type': 'string'}, 'files': {'type': 'array', 'items': {'type': 'string'}}}}},
        'inheritance': {'type': 'object', 'required': ids, 'properties': {name: row for name in ids}, 'additionalProperties': False}}}


def protected_conflict(task, protected):
    # API protection cannot be waived by a new natural-language request. Only direct
    # requests to edit a named protected file count; constraints ABOUT an API do not.
    for path in protected:
        pattern = r'\b(?:edit|modify|rewrite|delete|remove|update|replace)\s+(?:the\s+)?(?:file\s+)?[`\"\x27]?' + re.escape(path) + r'(?![\w./-])'
        for match in re.finditer(pattern, task, re.I):
            prefix = task[max(0, match.start() - 18):match.start()]
            if not re.search(r"(?:not|never|don\x27t)\s*$", prefix, re.I):
                return 'The request changes protected file ' + path + '. Clarify a solution that leaves this file unchanged.'
    return ''


def active_interfaces(job):
    """Retire old interface names only through a reviewed scope disposition."""
    from coder_policy7_evidence import requested_interfaces
    wanted = requested_interfaces(job['task'])
    brief = job.get('brief', {})
    supported = {r['parent_id'] for r in brief.get('scope_review', []) if r.get('supported') is True}
    retired = requested_interfaces('\n'.join(r['parent_outcome']['text'] for r in brief.get('requirement_history', [])
        if r['action'] in {'replace', 'remove'} and r['parent_id'] in supported))
    active = requested_interfaces('\n'.join(o['text'] for o in brief.get('outcomes', [])))
    return {kind: list(dict.fromkeys([*(v for v in values if v not in retired[kind] or v in active[kind]), *active[kind]]))
            for kind, values in wanted.items()}


def review_scope(brief, request, chat, retained):
    """Recover response formatting separately from semantic scope disagreements."""
    from coder_policy7 import json_object
    ids = [r['parent_id'] for r in brief['requirement_history']]
    decision = {'type': 'object', 'required': ['supported', 'reason'], 'properties': {
        'supported': {'type': 'boolean'}, 'reason': {'type': 'string'}}, 'additionalProperties': False}
    schema = {'type': 'object', 'required': ['decisions'], 'properties': {'decisions': {
        'type': 'object', 'required': ids, 'properties': {name: decision for name in ids}, 'additionalProperties': False}},
        'additionalProperties': False}
    active = {o['id']: o for o in brief['outcomes']}
    proposals = {r['parent_id']: {
        'original_requirement': r['parent_outcome']['text'], 'proposed_action': r['action'],
        'user_authorization': r['request_quote'],
        'resulting_requirements': [{k: v for k, v in active[name].items() if k != 'id'}
                                   for name in r['active_outcome_ids']]}
        for r in brief['requirement_history']}
    prompt = ('Check this follow-up scope reconciliation BEFORE editing. Return JSON with decisions keyed by PARENT IDs: '
        '{"decisions":{"o1":{"supported":true,"reason":"why"}}}. Required parent IDs: ' + ', '.join(ids) + '. '
        'Review every proposed action exactly once, including retained ones. supported means the PROPOSED ACTION '
        'is justified by the current request, NOT that the original requirement must remain unchanged. '
        'An explicitly requested replacement is supported when its resulting requirements implement that change. '
        'Retain unrelated requirements; replace/remove only what the current request explicitly changes. '
        'Replacement outcomes must preserve unaffected clauses. Mark supported false for contradictory retained requirements, '
        'ungrounded removals, missing replacement detail or ambiguous changes. Do not judge application correctness.\n' +
        json.dumps({'current_request': request, 'proposals_by_parent': proposals}))
    correction = ''
    for attempt in range(2):
        raw = None
        try:
            raw = chat(prompt + correction, schema)
            retained.append(raw)
            rows = json_object(raw).get('decisions')
            if not isinstance(rows, dict) or set(rows) != set(ids) or any(not isinstance(v, dict) or
                    type(v.get('supported')) is not bool or not isinstance(v.get('reason'), str) for v in rows.values()):
                raise ValueError('decisions must contain each required parent ID once, with boolean supported and text reason')
        except (ValueError, TypeError) as error:
            if raw is None:
                retained.append(getattr(error, 'response', '') or str(error))
            if 'rejected response format' in str(error).lower():
                schema = None  # Same semantic checks for runtimes without structured output.
            correction = '\nCorrect only the response format: ' + str(error) + '. Required keys: ' + ', '.join(ids)
            continue
        unsupported = [name + ': ' + rows[name]['reason'] for name in ids if not rows[name]['supported']]
        if unsupported:
            raise ScopeClarification('Clarify the intended change: ' + '; '.join(unsupported))
        return [{'parent_id': name, **rows[name]} for name in ids]
    raise ScopeClarification('Scope review could not verify every inherited requirement. Restate the intended change and what should remain unchanged.')
