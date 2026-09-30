"""Persist diagnosis before independently authoring a bounded probe correction."""
import copy


def author_check(store, operation_id, repository, read_only, requirement, identity,
                 old, sources, schema, instruction, context, draft, persist):
    from coder_contracts import (STRING, inspection_schema, object_schema,
        normalize_probe, targeted_correction, review_raw_probe)
    states = draft.setdefault('authoring', {})
    state = states.setdefault(identity, {'revision': 0, 'proposals': []})
    # A dispute needs only its requirement, its failure and relevant source.
    context = {k: v for k, v in context.items() if k not in {'requirements', 'previous_probes'}}
    context['original_check'] = old
    instruction = instruction.replace('runner is python, node, or shell.',
        'Choose runner api, file, browser, python, node, or shell using the runner-specific schema.')
    instruction = instruction.replace('complex APIs, CLI commands, documentation or other runtimes',
        'complex APIs, CLI commands or other runtimes')
    instruction = instruction.replace('Behavior, interface and preservation require independent executable assertions.',
        'Behavior, interface and API preservation require independent executable assertions. Literal file preservation uses file assertions.')
    instruction += (
        '\nUse runner file for documentation and literal file preservation. Its path is project-relative, cwd is ., '
        'and assertions is a list of {kind:exists}, {kind:nonempty}, {kind:contains,value:literal text}, '
        'or {kind:unchanged}. The controller supplies hashes from the baseline; never supply a hash. '
        'README requirements bind to the README itself. API/CLI/browser behavior requires actual execution. '
        'API args and equal expectations are recursive JSON values: preserve exact arrays, objects, numbers, '
        'booleans and null. Never wrap a list expectation in an extra object or array. Return only one check.'
    )
    diagnosis = state.get('diagnosis')
    if old and not diagnosis:
        fields = ('disposition', 'reason', 'request_basis', 'source_basis', 'failure_basis')
        def validate(answer):
            if set(answer) != set(fields): raise ValueError('Return only the diagnosis fields; no replacement check yet')
            if answer['disposition'] not in {'code_defect', 'probe_defect', 'ambiguous'}:
                raise ValueError('Choose code_defect, probe_defect, or ambiguous')
            if any(not isinstance(answer[k], str) or not answer[k].strip() for k in fields):
                raise ValueError('Ground the diagnosis in the request, current source and failed assertion')
        diagnosis = read_only(store, operation_id, repository,
            'Audit this disputed check without editing code or proposing a replacement. Return disposition '
            'code_defect, probe_defect, or ambiguous, and reason, request_basis, source_basis, failure_basis. '
            'Cite current source paths and the exact failed assertion. Do not weaken requirements to match code.',
            {**context, '_response_step':'diagnosis:'+identity}, validate=validate, schema=inspection_schema(object_schema({
                **{k: STRING for k in fields[1:]},
                'disposition': {'enum': ['code_defect', 'probe_defect', 'ambiguous']},
            }, list(fields))))
        state['diagnosis'] = diagnosis
        draft['audits'].append({**diagnosis, 'check_id': old['id']})
        persist()
        store.event(operation_id, 'probe_diagnosis', check_id=old['id'], **diagnosis)
    if diagnosis and diagnosis['disposition'] != 'probe_defect':
        return copy.deepcopy(old)
    if diagnosis:
        context['diagnosis'] = diagnosis
        instruction += '\nThe diagnosis is already saved. Return ONLY the replacement check. Executable assertions must change; preserve the assigned requirement.'
    while state['revision'] <= 2:
        attempt = state['revision']
        author_context = {**context, 'semantic_revision': attempt, 'rejected_proposals': state['proposals'],
            '_response_step':f'proposal:{identity}:{attempt}'}
        def validate(answer):
            if old:
                targeted_correction({**diagnosis, 'replacement': answer}, old, requirement, sources, policy_version=5)
            else:
                normalize_probe(answer, requirement, identity, policy_version=5)
        if 'proposal' not in state:
            answer = read_only(store, operation_id, repository, instruction, author_context,
                validate=validate, schema=inspection_schema(schema))
            state['proposal'] = normalize_probe(answer, requirement, identity, policy_version=5)
            persist()
            store.event(operation_id, 'probe_proposed', check_id=identity, proposal=state['proposal'],
                previous=old, semantic_revision=attempt)
        check = state['proposal']
        if check['runner'] in {'python', 'node', 'shell'}:
            verdict = review_raw_probe(store, operation_id, repository, read_only, check, requirement, sources,
                return_negative=True, previous=old)
            if not verdict['valid']:
                state['proposals'].append({'check': check, 'review': verdict})
                state.pop('proposal')
                state['revision'] += 1
                persist()
                store.event(operation_id, 'probe_rejected', check_id=identity, proposal=check,
                    reason=verdict['reason'], semantic_revision=attempt)
                continue
        if old:
            _, reason = targeted_correction({**diagnosis, 'replacement': {
                k: v for k, v in check.items() if k not in {'id', 'requirement_ids'}
            }}, old, requirement, sources, policy_version=5)
            draft['corrections'].append(reason)
        return check
    raise ValueError(f'Invalid raw probe {identity}: two semantic revisions exhausted; inspect rejected proposals')
