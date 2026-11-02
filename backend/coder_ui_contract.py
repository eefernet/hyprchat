"""Versioned, controller-owned UI outcomes. This is deliberately a closed grammar.

Only user text supplies expectations. Project flows may supply launch/setup
recipes after the controller has executed them, never verdicts or expectations.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import time

VERSION = 1
_UI = re.compile(r'\b(button|UI|web|desktop|Qt|GTK|Swing|browser)\b', re.I)
_NEGATION = re.compile(r"\b(not|never|don't|doesn't|without|unless|except)\b", re.I)
_NAME = r'(?:["`\']([^"`\']+)["`\']|([A-Z][\w -]*?))'


def _id(value):
    return 'ui-contract:' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def _action(clause):
    # Return only a named activation; vague actions ("reset it") stay unverified.
    clause = re.sub(r'^(?:Make|Change)\s+', '', clause)
    patterns = [r'\b(?:a|the|an)\s+' + _NAME + r'\s+button\b',
                r'\b(?:[Cc]licking|[Cc]lick|[Pp]ressing|[Pp]ress)\s+(?:the\s+)?' + _NAME + r'(?=\s+(?:button|clears?|sets?|shows?)\b|[,.;]|$)',
                r'\b' + _NAME + r'\s+(?:clears?|sets?|shows?)\b']
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, clause):
            value = next(v for v in match.groups() if v).strip()
            if value.lower() not in {'add', 'make', 'the', 'it'}:
                found.append(value)
        if found:
            break
    names = list(dict.fromkeys(found))
    return names[0] if len(names) == 1 else ''


def _targets(text):
    rows = []
    for part in re.split(r'\s+and\s+|\s*,\s*', text.strip(), flags=re.I):
        part = re.sub(r'^(?:all\s+)?(?:the\s+)?', '', part, flags=re.I).strip()
        if re.fullmatch(r'(?:inputs?|fields?)', part, re.I):
            rows.append({'kind': 'input', 'all': part.lower().endswith('s'), 'name': ''})
        elif re.fullmatch(r'(?:result|output)(?:\s+(?:text|label|display))?', part, re.I):
            rows.append({'kind': 'result', 'all': False, 'name': ''})
        else:
            match = re.fullmatch(r'(?:(input|field|result|output|label)\s+)?(?:with\s+id\s+|id\s+)?#([\w-]+)', part, re.I)
            named = re.fullmatch(r'(input|field|result|output|label)\s+(?:named\s+)?["`\']([^"`\']+)["`\']', part, re.I)
            if match:
                rows.append({'kind': 'input' if (match[1] or '').lower() in {'input', 'field'} else 'element',
                             'all': False, 'selector': '#' + match[2]})
            elif named:
                rows.append({'kind': 'input' if named[1].lower() in {'input', 'field'} else 'result',
                             'all': False, 'name': named[2]})
            else:
                return []
    return rows


def freeze(task, source=None, *, mode='edit_project', source_revision=''):
    """Called before the queued job is visible. Never reparse on restart/resume."""
    source = source or {}
    previous = source.get('ui_contract') or {}
    criteria = deepcopy(previous.get('criteria', []))
    history = deepcopy(previous.get('history', []))
    entrypoint_task = previous.get('entrypoint_task') or source.get('user_task') or task
    ui_source = any(c.get('test_interfaces') and set(c['test_interfaces']) & {'web', 'desktop'}
                    for c in source.get('checks', []))
    named_action = any(_action(clause) for clause in re.split(r'(?<=[.!?])\s+|\n+', task.strip()))
    if mode != 'edit_project' or not (ui_source or _UI.search(task) or named_action):
        return {'version': VERSION, 'criteria': criteria, 'history': history, 'entrypoint_task': entrypoint_task}
    for excerpt in re.split(r'(?<=[.!?])\s+|\n+', task.strip()):
        if not excerpt.strip():
            continue
        clause = excerpt.strip().rstrip('.!?')
        action = _action(clause)
        parsed = []
        if action and not _NEGATION.search(clause):
            for outcome in re.split(r'\s+and\s+(?=(?:sets?|shows?|clears?|empties)\b)', clause, flags=re.I):
                clear = re.search(r'\b(?:clears?|empties|empty)\s+(.+)$', outcome, re.I)
                exact = re.search(r'\bsets?\s+(.+?)\s+to\s+(?:["`\']([^"`\']*)["`\']|([A-Z][\w-]*|-?\d+(?:\.\d+)?))$', outcome)
                show = re.search(r'\bshows?\s+["`\']([^"`\']*)["`\']\s+(?:in|on)\s+(.+)$', outcome, re.I)
                target_text = clear[1] if clear else exact[1] if exact else show[2] if show else ''
                expected = '' if clear else (exact[2] if exact[2] is not None else exact[3]) if exact else show[1] if show else None
                targets = _targets(target_text) if target_text else []
                if not targets:
                    parsed.append({'supported': False, 'reason': 'Part of this instruction cannot be translated to an exact UI outcome.'})
                for target in targets:
                    # A literal identifier in the source request can bind the
                    # meaning of "result" without trusting generated selectors.
                    if target['kind'] == 'result' and not target.get('name'):
                        ids = re.findall(r'\b(?:output|result)\s+(?:with\s+)?id\s+([\w-]+)', source.get('user_task', ''), re.I)
                        if len(set(ids)) == 1:
                            target['source_selector'] = '#' + ids[0]
                    parsed.append({'action': {'action': 'click', 'name': action}, 'target': target,
                                   'property': 'value' if target['kind'] == 'input' else 'text', 'expected': expected})
        if not parsed:
            parsed = [{'supported': False, 'reason': 'This instruction is outside the explicit clear/exact-value UI grammar or is ambiguous.'}]
        for item in parsed:
            item.setdefault('supported', True)
            item.update(request_excerpt=excerpt, provenance={'kind': 'user_request', 'source_job_id': source.get('id', ''),
                        'source_revision_id': source_revision}, version=VERSION)
            item['id'] = _id(item)
            # Only the same explicit activation + target can replace an outcome.
            retained = []
            for old in criteria:
                logical = lambda target: {k: v for k, v in target.items() if k != 'source_selector'}
                if item['supported'] and old.get('supported') and old.get('action') == item['action'] and logical(old.get('target', {})) == logical(item['target']):
                    if old['target'].get('source_selector') and not item['target'].get('source_selector'):
                        item['target']['source_selector'] = old['target']['source_selector']
                    history.append({**old, 'superseded_by': item['id'], 'replacement_excerpt': excerpt})
                else:
                    retained.append(old)
            criteria = retained + [item]
    return {'version': VERSION, 'criteria': criteria, 'history': history, 'entrypoint_task': entrypoint_task}


def recipes(checks):
    """Only observed, revision-bound successful flows can establish setup."""
    result = []
    for row in checks:
        if (row.get('passed') and row.get('revision_id') and row.get('execution_succeeded')
                and row.get('origin') in {'confirmed_interaction', 'project_interaction'} and row.get('flow')):
            result.append({'revision_id': row['revision_id'], 'flow': deepcopy(row['flow'])})
    return result


def bindings(contract, checks):
    """Freeze resolvable source targets before coding; leave new controls open."""
    bound = {}
    for row in checks:
        if not row.get('passed') or not row.get('flow'):
            continue
        observations = [o for b in row.get('service_bindings', []) for o in b.get('observations', [])]
        nodes = next((o['observed'].get('nodes', []) for o in observations if o['action']['action'] == 'launch'), [])
        for criterion in contract.get('criteria', []):
            if not criterion.get('supported') or criterion['id'] in bound:
                continue
            try:
                targets = _matches(nodes, criterion['target'])
                # Stable web IDs and named editable desktop controls only.
                # A dynamic label Name or a DOM ordinal is not a stable identity.
                stable = []
                for node in targets:
                    if node.get('selector', '').startswith('#'):
                        stable.append({'selector': node['selector']})
                    elif node.get('editable') and node.get('name'):
                        stable.append({'name': node['name'], 'role': node['role']})
                if len(stable) == len(targets):
                    bound[criterion['id']] = {'revision_id': row['revision_id'], 'targets': stable}
            except Unverified:
                pass
    return bound


class Unverified(ValueError):
    pass


def _matches(nodes, target, *, activation=False):
    candidates = [n for n in nodes if n.get('showing', True)]
    if activation:
        candidates = [n for n in candidates if n.get('activatable') and n.get('name') == target['name']]
    elif target.get('selector') or target.get('source_selector'):
        candidates = [n for n in candidates if n.get('selector') == (target.get('selector') or target['source_selector'])]
    else:
        if target.get('name'):
            candidates = [n for n in candidates if n.get('name') == target['name']]
        if target['kind'] == 'input':
            candidates = [n for n in candidates if n.get('editable')]
        elif target['kind'] == 'result':
            candidates = [n for n in candidates if not n.get('interactive_ancestor')]
            outputs = [n for n in candidates if n.get('tag') == 'output' or n.get('role') in {'status', 'output'}]
            if outputs:
                candidates = outputs
            else:
                candidates = [n for n in candidates if n.get('role') == 'label' or
                              n.get('name', '').lower() in {'result', 'output'} or n.get('selector') in {'#result', '#output'}]
    if not candidates or not target.get('all') and len(candidates) != 1:
        raise Unverified(f'Expected {"one or more" if target.get("all") else "one"} suitable target for {target}; found {len(candidates)}.')
    return candidates


def _request(node):
    # These locators are resolved anew in each private session. Desktop IDs may
    # be pinned for a single flow when a label's Name changes with its text.
    return {'selector': node.get('selector') or node['id'],
            **({'session_id': node['session_id']} if node.get('session_id') else {})}


def _read(action, node, prop):
    request = {'action': prop, **_request(node)}
    observed = action(request)
    if prop not in observed:
        raise Unverified(f'The target does not expose {prop}.')
    return observed[prop]


def run(store, operation_id, repository, contract, context, bound=None):
    from coder_ui_checks import session, bind_project_entrypoint
    payload = store.get(operation_id)['payload']
    revision = payload['revision_id']
    rows = []
    for criterion in contract.get('criteria', []):
        row = {**deepcopy(criterion), 'revision_id': revision, 'status': 'unverified', 'evidence': []}
        if not criterion.get('supported'):
            rows.append(row)
            continue
        attempts = []
        for recipe in context:
            flow = recipe['flow']
            try:
                bind_project_entrypoint(repository, flow, contract.get('entrypoint_task', '') + '\n' + payload.get('task', ''))
                with session(store, operation_id, repository) as action:
                    action({'action': 'launch', **{k: v for k, v in flow.items() if k != 'steps'}})
                    # Only successful observed flows supply setup. Stop before
                    # the action being verified, and never execute assertions.
                    setup = []
                    for step in flow['steps']:
                        if step['action'] in {'click', 'press'} and step.get('name') == criterion['action']['name']:
                            break
                        if step['action'] in {'fill', 'click', 'press'}:
                            request = {k: v for k, v in step.items() if k not in {'equals', 'contains'}}
                            # AT-SPI paths from a previous session are not stable.
                            if flow['kind'] == 'desktop' and request.get('selector'):
                                raise Unverified('Setup uses a desktop node ID from a different session.')
                            action(request)
                            setup.append(request)
                    nodes = action({'action': 'observe'}).get('nodes', [])
                    button = _matches(nodes, criterion['action'], activation=True)[0]
                    targets = _matches(nodes, criterion['target'])
                    frozen = (bound or {}).get(criterion['id'])
                    if frozen:
                        actual = []
                        for identity in frozen['targets']:
                            matches = [n for n in targets if all(n.get(k) == v for k, v in identity.items())]
                            if len(matches) != 1:
                                raise Unverified('A source control is missing or ambiguous at this revision: ' + str(identity))
                            actual.extend(matches)
                        if len(actual) != len(targets):
                            raise Unverified('The target group changed since the source revision; review its bindings.')
                        targets = actual
                    prop = criterion['property']
                    if criterion['target']['kind'] == 'element' and all(n.get('editable') for n in targets):
                        prop = 'value'
                    before = [_read(action, n, prop) for n in targets]
                    if not setup or any(value == criterion['expected'] for value in before):
                        raise Unverified('No observed setup established a different starting value for every target; a no-op cannot prove this transition.')
                    action({'action': 'click', **_request(button)})
                    deadline = time.monotonic() + 2
                    while True:
                        after = [_read(action, n, prop) for n in targets]
                        if all(value == criterion['expected'] for value in after) or time.monotonic() >= deadline:
                            break
                        time.sleep(.1)  # observations only; the activation is never replayed
                    row['status'] = 'passed' if all(value == criterion['expected'] for value in after) else 'failed'
                    row['reason'] = f"Expected {prop} {criterion['expected']!r} after {criterion['action']['name']}; observed {after!r}."
                    row['evidence'] = [{'revision_id': revision, 'setup_revision_id': recipe['revision_id'],
                        'launch': {k: v for k, v in flow.items() if k != 'steps'}, 'setup': setup,
                        'targets': targets, 'action': button, 'property': prop, 'before': before, 'after': after}]
                    break
            except (InterruptedError, TimeoutError):
                raise
            except Exception as error:
                attempts.append(str(error))
        if row['status'] == 'unverified':
            row['reason'] = '; '.join(dict.fromkeys(attempts))[:4000] or 'No controller-observed interaction setup is available.'
        rows.append(row)
        store.event(operation_id, 'ui_contract_verification', criterion=row)
    return rows


def apply_summary(summary, criteria):
    summary.update(ui_contract_version=VERSION, criteria=criteria)
    failed = [c for c in criteria if c['status'] == 'failed']
    summary['failures'].extend({'id': c['id'], 'classification': 'ui_contract_failed',
        'reason': c['request_excerpt'] + '\n' + c['reason']} for c in failed)
    if any(c['status'] != 'passed' for c in criteria):
        summary['checks_passed'] = False
    summary['unverified_ui_criteria'] = [c['id'] for c in criteria if c['status'] == 'unverified']
