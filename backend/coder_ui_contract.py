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
    """Freeze literal requests once. Execution never reparses persisted contracts."""
    from coder_ui_parser import PARSER_VERSION, clauses, parse, action_name, tokenize
    source = source or {}
    previous = source.get('ui_contract') or {}
    criteria = deepcopy(previous.get('criteria', []))
    history = deepcopy(previous.get('history', []))
    entrypoint_task = previous.get('entrypoint_task') or source.get('original_user_request') or task
    result = {'version': VERSION, 'parser_version': PARSER_VERSION, 'criteria': criteria, 'history': history,
              'entrypoint_task': entrypoint_task}
    if previous.get('legacy_contracts'): result['legacy_contracts'] = deepcopy(previous['legacy_contracts'])
    ui_source = any(set(c.get('test_interfaces', [])) & {'web', 'desktop'} for c in source.get('checks', []))
    excerpts = list(clauses(task.strip()))
    if mode != 'edit_project' or not (ui_source or _UI.search(task) or any(action_name(*tokenize(c)) for c in excerpts)):
        return result
    parsed = [(excerpt, parse(excerpt)) for excerpt in excerpts]
    key = lambda item: json.dumps([item.get('action'), item.get('target'), item.get('property')], sort_keys=True)
    expectations = {}
    for _, items in parsed:
        for item in items:
            if item.get('supported'): expectations.setdefault(key(item), set()).add(item['expected'])
    for excerpt, items in parsed:
        for item in items:
            if item.get('supported') and len(expectations[key(item)]) > 1:
                item.update(supported=False, reason='This request gives conflicting expected values for the same action and target.')
            if item.get('supported') and item['target']['kind'] == 'result' and not item['target'].get('name'):
                ids = re.findall(r'\b(?:output|result)\s+(?:with\s+)?id\s+([\w-]+)', entrypoint_task, re.I)
                if len(set(ids)) == 1: item['target']['source_selector'] = '#' + ids[0]
            item.update(request_excerpt=excerpt, provenance={'kind': 'user_request', 'source_job_id': source.get('id', ''),
                        'source_revision_id': source_revision}, version=VERSION, parser_version=PARSER_VERSION)
            item['id'] = _id(item)
            retained = []
            reaffirmed = False
            for old in criteria:
                logical = lambda t: {k:v for k,v in t.items() if k != 'source_selector'}
                if old.get('id') == item['id']: continue
                if (item['supported'] and old.get('supported') and old.get('action') == item['action']
                        and logical(old.get('target', {})) == logical(item['target'])):
                    if old.get('expected') == item.get('expected') and old.get('property') == item.get('property'):
                        retained.append(old)
                        history.append({'kind': 'reaffirmed', 'criterion_id': old['id'], 'request_excerpt': excerpt})
                        reaffirmed = True
                        continue
                    if old['target'].get('source_selector') and not item['target'].get('source_selector'):
                        item['target']['source_selector'] = old['target']['source_selector']
                    history.append({**old, 'superseded_by': item['id'], 'replacement_excerpt': excerpt})
                else: retained.append(old)
            criteria = retained + ([] if reaffirmed else [item])
    result.update(criteria=criteria, history=history)
    return result


async def original_source(source, load):
    """Upgrade only the new job's inherited contract, never historical jobs/evidence."""
    if not source or (source.get('ui_contract') or {}).get('parser_version') == 2:
        return source
    lineage, seen = [], set()
    row = source
    while row and row.get('id') not in seen:
        seen.add(row.get('id')); lineage.append(row)
        if (row.get('ui_contract') or {}).get('parser_version') == 2: break
        row = await load(row['source_job_id']) if row.get('source_job_id') else None
    parent = {}
    legacy = []
    for row in reversed(lineage):
        old = row.get('ui_contract') or {}
        if old.get('parser_version') == 2:
            parent = deepcopy(row); continue
        if old: legacy.append({'source_job_id': row.get('id'), 'contract': deepcopy(old)})
        literal = row.get('original_user_request')
        if isinstance(literal, str) and literal.strip():
            compiled = freeze(literal, parent, mode=row.get('mode', 'edit_project'), source_revision=row.get('source_revision_id', ''))
        else:
            compiled = deepcopy(parent.get('ui_contract') or {'version': VERSION, 'parser_version': 2, 'criteria': [], 'history': []})
            for criterion in old.get('criteria', []):
                unresolved = {**criterion, 'supported': False,
                    'reason': 'The original user wording for this inherited requirement is unavailable.',
                    'provenance': {**criterion.get('provenance', {}), 'kind': 'legacy_unresolved'}}
                if unresolved['id'] not in {c['id'] for c in compiled['criteria']}: compiled['criteria'].append(unresolved)
        parent = {**row, 'ui_contract': compiled}
    parent.setdefault('ui_contract', {})['legacy_contracts'] = [*parent['ui_contract'].get('legacy_contracts', []), *legacy]
    return {**source, 'ui_contract': parent['ui_contract']}


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
            candidates = [n for n in candidates if not n.get('interactive_ancestor') and
                          not n.get('editable') and not n.get('activatable')]
            outputs = [n for n in candidates if n.get('tag') == 'output' or n.get('role') in {'status', 'output'}]
            if outputs:
                candidates = outputs
            else:
                # An explicitly identified result outranks unrelated captions
                # such as the label for a Task input. Never use its displayed
                # value to select it, and retain ambiguity among result controls.
                identified = [n for n in candidates if n.get('name', '').lower() in {'result', 'output'} or
                              n.get('selector') in {'#result', '#output'}]
                candidates = identified or [n for n in candidates if n.get('role') == 'label']
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


def seed_inputs(action, nodes, targets, before, expected, setup):
    """Reuse observed input data when an earlier action cleared the field.

    A completed Add often empties its input. The recorded fill can establish a
    different state again, without inventing data or choosing another control.
    """
    observed_setup = list(setup)
    for index, node in enumerate(targets):
        if before[index] != expected or not node.get('editable'):
            continue
        fills = []
        for step in observed_setup:
            if step.get('action') != 'fill' or str(step.get('value', '')) == expected:
                continue
            matches = [n for n in nodes if n.get('editable') and n.get('showing', True) and
                (n.get('selector') == step['selector'] if step.get('selector') else
                 n.get('name') == step.get('name') and (not step.get('role') or n.get('role') == step['role']))]
            if len(matches) == 1 and matches[0] == node:
                fills.append(step)
        if fills:
            request = {'action': 'fill', **_request(node), 'value': fills[-1]['value']}
            action(request)
            setup.append(request)
            before[index] = _read(action, node, 'value')
    return before


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
                        if step['action'] in {'click', 'press'}:
                            if step.get('name') == criterion['action']['name']:
                                break
                            if flow['kind'] == 'web' and step.get('selector'):
                                # Saved browser flows normally use CSS IDs.
                                # Resolve the named activation from this session's
                                # observations before replaying it as setup. Final
                                # resolution below still rejects duplicate names.
                                observed = action({'action': 'observe'}).get('nodes', [])
                                activations = [n for n in observed if n.get('showing', True) and n.get('activatable') and
                                               n.get('name') == criterion['action']['name']]
                                if any(n.get('selector') == step['selector'] for n in activations):
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
                    if prop == 'value':
                        before = seed_inputs(action, nodes, targets, before, criterion['expected'], setup)
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
                if getattr(error, 'details', {}).get('environment_fault'):
                    row.update(environment_fault=True, diagnostic=error.details)
                    attempts.append(str(error))
                    break  # Another recipe cannot repair the controller environment.
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
