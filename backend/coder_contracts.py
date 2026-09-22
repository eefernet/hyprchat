"""Policy 3: controller-owned provenance, check identities, and partial drafts.

Models author semantics and executable assertions. The controller owns identity,
coverage, immutable request text, and targeted replacements.
"""
import copy
import ast
import json
from pathlib import Path

from coder_verification import (KINDS, digest, normalized, validate_requirements,
    validate_probes, PLAN_INSTRUCTION, PROBE_INSTRUCTION)


def request_sources(task):
    return {f"request-{i+1}": line for i, line in enumerate(task.splitlines()) if line.strip()}


def object_schema(properties, required):
    return {"type":"object", "properties":properties, "required":required, "additionalProperties":False}


STRING = {"type":"string"}
STRINGS = {"type":"array", "items":STRING, "minItems":1}
REQUIREMENT_SCHEMA = object_schema({"slot":{"type":"integer","minimum":1}, "text":STRING,
    "kind":{"enum":sorted(KINDS)}, "source_refs":STRINGS}, ["slot","text","kind","source_refs"])
REQUIREMENTS_SCHEMA = object_schema({"requirements":{"type":"array", "items":REQUIREMENT_SCHEMA, "minItems":1}}, ["requirements"])
PROBE_SCHEMA = object_schema({"runner":{"enum":["python","node","shell","browser"]}, "program":STRING,
    "cwd":STRING, "expected_behavior":STRING, "path":STRING, "steps":{"type":"array","items":{"type":"object"}}},
    ["runner","cwd","expected_behavior"])


def inspection_schema(final):
    final = copy.deepcopy(final)
    definitions = final.pop('$defs', None)
    result = {"anyOf":[final, object_schema({"inspect":{"type":"object"}},["inspect"])]}
    if definitions: result['$defs'] = definitions
    return result


def normalize_requirement(item, sources):
    slot = item.get("slot")
    if isinstance(slot,bool) or not isinstance(slot,int) or slot < 1:
        raise ValueError("Each requirement needs a positive integer slot")
    refs = item.get("source_refs")
    if not isinstance(refs,list) or not refs or any(ref not in sources for ref in refs):
        raise ValueError(f"Slot {slot}: source_refs must select supplied request IDs")
    if item.get("kind") not in KINDS or not str(item.get("text","")).strip():
        raise ValueError(f"Slot {slot}: supply text and a supported requirement kind")
    return {"id":f"r{slot}", "text":item["text"], "kind":item["kind"], "source_refs":list(dict.fromkeys(refs)),
        "source_quote":sources[refs[0]], "source_quotes":[sources[ref] for ref in refs]}


def merge_requirements(answer, sources, draft, pending=None):
    rows = answer.get("requirements")
    if not isinstance(rows,list) or not rows:
        raise ValueError("Return requirements with slot, text, kind, and source_refs")
    errors, seen = [], set()
    pending = pending if pending is not None else []
    for item in rows:
        try:
            if not isinstance(item,dict): raise ValueError("Requirement must be an object")
            row = normalize_requirement(item,sources)
            if row['id'] in seen: raise ValueError("Duplicate requirement slot")
            seen.add(row['id'])
            # Valid slots survive a model's unrelated rewrites during correction.
            draft.setdefault(row['id'],row)
            if row['id'] in pending: pending.remove(row['id'])
        except (ValueError,TypeError) as error:
            errors.append(str(error))
            if isinstance(item,dict) and isinstance(item.get('slot'),int):
                identity=f"r{item['slot']}"
                if identity not in draft and identity not in pending: pending.append(identity)
    if pending: errors.append('Uncorrected slots: '+', '.join(pending))
    if errors: raise ValueError("; ".join(errors) + "; return only corrected slots. Valid slots are already saved.")
    return sorted(draft.values(),key=lambda row:int(row['id'][1:]))


def normalize_probe(answer, requirement, identity, policy_version=3):
    allowed={"runner","program","cwd","expected_behavior","path","steps"}
    if policy_version >= 5:
        allowed.add('assertions')
    if policy_version >= 4:
        allowed.update({"target","cases","bindings"})
        if not isinstance(answer,dict) or set(answer)-allowed:
            raise ValueError('Probe contains unsupported fields; use the supplied schema')
        runner=answer.get('runner')
        specific = {'api': {'target','cases'}, 'browser': {'path','steps'}, 'file': {'path','assertions'}}
        permitted = {'runner','cwd','expected_behavior'} | specific.get(runner, {'program','bindings'})
        if set(answer)-permitted: raise ValueError(f'Unsupported fields for runner {runner}: {sorted(set(answer)-permitted)}')
        if policy_version >= 5:
            if runner not in {'api','file','browser','python','node','shell'}:
                raise ValueError('Select a supported runner')
            if permitted-set(answer): raise ValueError(f'Missing fields for runner {runner}: {sorted(permitted-set(answer))}')
            if not isinstance(answer['expected_behavior'],str) or not answer['expected_behavior'].strip():
                raise ValueError('Describe the expected behavior')
            if requirement['kind']=='documentation' and runner!='file':
                raise ValueError('Documentation must use a controller-owned file check bound to the documentation itself')
    check = {key:value for key,value in answer.items() if key in
        allowed}
    check.update(id=identity,requirement_ids=[requirement['id']])
    check.setdefault('cwd','.')
    validate_probes({'checks':[check]},[requirement],policy_version=policy_version)
    if check.get('runner')=='python':
        def active_nodes(node):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)):return
            yield node
            for child in ast.iter_child_nodes(node):yield from active_nodes(child)
        if not any(isinstance(node,ast.Assert) for node in active_nodes(ast.parse(check['program']))):
            raise ValueError('Run Python assertions at top level. Defining a test function without executing it does not verify behavior.')
    return check


def targeted_correction(answer, check, requirement, sources, policy_version=3):
    disposition = answer.get('disposition')
    if policy_version >= 4:
        if disposition not in {'code_defect','probe_defect','ambiguous'}:
            raise ValueError('Choose code_defect, probe_defect, or ambiguous')
        for field in ('reason','request_basis','source_basis','failure_basis'):
            if not isinstance(answer.get(field),str) or not answer[field].strip():
                raise ValueError(f'Diagnosis needs {field}, grounded in the original request, current source and failed assertion')
        if disposition != 'probe_defect': return copy.deepcopy(check), None
    if disposition == 'code_defect': return copy.deepcopy(check), None
    if disposition != 'probe_defect': raise ValueError('Choose code_defect or probe_defect')
    if not str(answer.get('reason','')).strip(): raise ValueError('Explain the demonstrated probe defect')
    refs=requirement.get('source_refs',[])
    if not refs or any(ref not in sources for ref in refs): raise ValueError('The saved requirement has no valid request provenance')
    revised=normalize_probe(answer.get('replacement',{}),requirement,check['id'],policy_version=policy_version)
    if policy_version >= 5:
        from coder_check_schema import executable_identity
        if executable_identity(revised)==executable_identity(check):
            raise ValueError('Correction must change executable assertions, not explanatory text')
    if policy_version >= 4 and digest(revised)==digest(check): raise ValueError('probe_defect requires a corrected replacement')
    return revised, {'check_id':check['id'],'reason':answer['reason'],'source_refs':refs,'source_quote':sources[refs[0]]}


def attach_acceptance_evidence(answer,checks,revision):
    """Reference actual matching evidence without changing the model verdict."""
    for row in answer.get('coverage',[]):
        if not isinstance(row,dict):continue
        row['check_ids']=[check['id'] for check in checks if check.get('revision_id')==revision
            and row.get('requirement_id') in check.get('requirement_ids',[])
            and (row.get('status')!='passed' or check.get('passed'))]
    return answer


def review_raw_probe(store, operation_id, repository, read_only, check, requirement, sources, *, return_negative=False, previous=None):
    """Review this check's coverage, without requiring every check to test the app."""
    instruction = (
        'Independently audit this raw probe for ONLY the supplied requirement.id, requirement.text and requirement.kind. '
        'Return {valid:boolean,reason:string}. The whole request provides context; other requirements have their own independent checks. '
        'Assess whether this check will detect violations of its assigned requirement. Do not demand coverage assigned to other checks. '
        'Python/Node API probes must import and call the real subject, never copy or shadow it. '
        'Shell CLI behavior probes must execute the real entrypoint. File-content or file-preservation probes may read the bound file and assert its content or hash. '
        'For a new project this is a prospective check: absence of files before Builder runs is not a defect. '
        'Respect explicitly requested paths; other layout suggestions remain advisory and can be resolved after building. '
        'Judge test validity, not whether the unfinished application already passes it. A negative verdict is a valid response; never change it merely to satisfy response validation. No edits. '
    )
    if requirement['kind']=='documentation':
        instruction += ('This requirement is DOCUMENTATION ONLY. Check the requested documentation exists and contains any explicitly requested information. '
            'File existence/content assertions are valid evidence for documentation. This probe does NOT need to import the API, '
            'execute behavioral cases, test input preservation or run the project test suite. Those belong to other checks. '
            'Do not invent mandatory documentation wording or runtime behavior beyond this requirement. ')
    if previous:
        instruction += ('Compare previous_probe and candidate_probe. A correction must change the disputed executable '
            'assertion or its actual fixture/target, while retaining requirement coverage. Reject changes confined to '
            'comments, diagnostic messages, logging, formatting or unrelated assertions. Explain the concrete '
            'assertion change in reason; do not infer it from the author\'s explanation. ')
    def validate(answer):
        if type(answer.get('valid')) is not bool or not isinstance(answer.get('reason'),str) or not answer['reason'].strip():
            raise ValueError('Raw probe review needs a boolean valid verdict and a nonempty reason')
    verdict=read_only(store,operation_id,repository,instruction,
        {'requirement':requirement,'candidate_probe':check,
         **({'_response_step':'raw-review:'+check['id']+':'+digest(check)} if return_negative else {}),
         **({'previous_probe':previous} if previous else {}),
         'request_sources':{ref:sources[ref] for ref in requirement.get('source_refs',[]) if ref in sources},
         'coverage_scope':{'requirement_ids':check['requirement_ids'],'other_requirements':'Verified by separate checks; do not add their obligations here'}},
        validate=validate,schema=inspection_schema(object_schema({'valid':{'type':'boolean'},'reason':STRING},['valid','reason'])))
    store.event(operation_id,'raw_probe_review',check_id=check['id'],requirement_id=requirement['id'],
        valid=verdict['valid'],reason=verdict['reason'])
    if not verdict['valid'] and not return_negative:
        raise ValueError('Invalid raw probe: '+verdict['reason'])
    return verdict


def run_contract(store, operation_id, repository, read_only):
    operation=store.get(operation_id);payload=operation['payload'];task=payload['original_task']
    version=payload.get('policy_version',3)
    browser_instruction=''
    schema=PROBE_SCHEMA
    if version >= 4:
        from coder_browser_schema import STEPS_SCHEMA, INSTRUCTION as browser_instruction
        from coder_api_probe import TARGET_SCHEMA, CASES_SCHEMA, BINDINGS_SCHEMA
        schema=object_schema({**PROBE_SCHEMA['properties'],'runner':{'enum':['api','python','node','shell','browser']},
            'steps':STEPS_SCHEMA,'target':TARGET_SCHEMA,'cases':CASES_SCHEMA,'bindings':BINDINGS_SCHEMA},PROBE_SCHEMA['required'])
    if version >= 5:
        from coder_check_schema import probe_schema
        schema = probe_schema()
    sources=request_sources(task)
    directory=store.root/'jobs'/operation['job_id'];directory.mkdir(parents=True,exist_ok=True)
    # Same request key can resume a completed subset after process interruption.
    identity={'kind':operation['kind'],'task':task,'revision':payload.get('revision_id'),
        'requirements':payload.get('requirements'),'previous':payload.get('diagnose_probes'),'evidence':payload.get('evidence')}
    if version >= 5:
        from coder_response_recovery import contract_identity
        identity=contract_identity(operation)
    path=directory/('contract-'+digest(identity)+'.json')
    draft=json.loads(path.read_text()) if path.exists() else {}
    def persist():
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(draft));temporary.replace(path)
    extra={**payload.get('evidence',{}),'request_sources':sources,'baseline_revision':payload.get('baseline_revision'),
        'scope_note':'Only request_sources define the new change. Source and previous tests establish existing behavior; do not quote them as new instructions.'}
    if operation['kind']=='plan':
        if not draft.get('requirements_complete'):
            valid=draft.setdefault('valid_requirements',{})
            pending=draft.setdefault('pending',[])
            def validate(answer):
                try: merge_requirements(answer,sources,valid,pending)
                finally: persist()
            read_only(store,operation_id,repository,
                'Extract ALL explicit requirements from the current request, including tests, docs, interfaces, errors and preservation. '
                'Return JSON, for example {"requirements":[{"slot":1,"text":"Preserve the API","kind":"preservation","source_refs":["request-1"]}]}. Use sequential integer slots. '
                'Kinds: behavior, interface, preservation, tests, documentation, visual. Visual is only for explicitly requested image review. '
                'Select existing source IDs; never type source quotations. Saved valid slots are fixed; return only corrected/missing slots after validation feedback.',
                {**extra,'saved_valid_slots':valid, **({'_response_step':'requirements'} if version>=5 else {})},
                validate=validate,schema=inspection_schema(REQUIREMENTS_SCHEMA))
            draft['requirements_complete']=True;persist()
        requirements=sorted(draft['valid_requirements'].values(),key=lambda r:int(r['id'][1:]))
        def validate_plan(answer):
            answer['requirements']=requirements
            validate_requirements(answer,task,policy_version=version)
        answer=read_only(store,operation_id,repository,
            PLAN_INSTRUCTION+'\nRequirements below are FIXED; omit requirements from your response. Use their IDs in milestones. '
            'For a small request use ONE milestone covering all requirements.\n'+browser_instruction,
            {**extra,'requirements':requirements, **({'_response_step':'plan'} if version>=5 else {})},validate=validate_plan)
        return answer
    requirements=payload['requirements'];previous=payload.get('diagnose_probes')
    checks=draft.setdefault('checks',{});reasons=draft.setdefault('corrections',[]);audits=draft.setdefault('audits',[])
    failed={c['id'].removeprefix('requirement-') for c in extra.get('failed_checks',[])}
    originals={c['requirement_ids'][0]:c for c in (previous or {}).get('checks',[])}
    for requirement in requirements:
        if requirement['kind']=='visual': continue
        identity='check-'+requirement['id']
        if identity in checks: continue
        old=originals.get(requirement['id'])
        if old and old['id'] not in failed:
            checks[identity]=old;persist();continue
        if requirement['kind']=='tests':
            checks[identity]={'id':identity,'requirement_ids':[requirement['id']],'runner':'project_tests','cwd':'.'}
            persist();continue
        instruction=PROBE_INSTRUCTION.replace('Return {checks:[{id,requirement_ids,cwd,runner,program,expected_behavior}]} covering every nonvisual requirement.',
            'Return ONE check object (runner,cwd,program,expected_behavior; or browser path/steps).')+'\n'
        instruction+='The controller supplies its ID and coverage. Focus ONLY on the supplied requirement. Do not add cases for other requirements. '
        instruction+='Python example: from module import function\nassert function(input) == expected\n'
        instruction+="Node example: import assert from 'node:assert/strict'; import {fn} from './module.mjs'; assert.deepEqual(fn(input), expected);\n"
        instruction+='Execute assertions at top level, not inside an uncalled test function. Do not run project test files to establish independent behavior.'
        instruction+=' For JavaScript special property names use computed data properties, for example {["__proto__"]:"value"}; Object.assign cannot repair a colon-form __proto__ literal. Group keys are the item property VALUES, not the property name.'
        if version >= 4:
            from coder_api_probe import INSTRUCTION
            instruction += '\n'+INSTRUCTION+'\n'+browser_instruction
        context={**extra,'requirement':requirement}
        if version >= 5:
            from coder_contracts_v5 import author_check
            check = author_check(store, operation_id, repository, read_only, requirement, identity,
                old, sources, schema, instruction, context, draft, persist)
            checks[identity]=check;persist();continue
        if old:
            instruction+='\nDiagnose this failed probe. Return {"disposition":"code_defect"} when the implementation is wrong; keep the check unchanged. '
            instruction+='Only for a demonstrated probe defect return JSON with "disposition":"probe_defect", "reason", and "replacement" containing the corrected check. The controller retains this requirement\'s request references. Never weaken the requested behavior to match code.'
            context['original_check']=old
            if version >= 4:
                instruction+='\nThis is a FRESH read-only audit. Also allow disposition:ambiguous if evidence cannot resolve the dispute. For EVERY disposition supply reason, request_basis, source_basis, failure_basis as specific nonempty explanations. Cite actual current paths and failed assertions. No code editing. A repair that leaves the same failure requires re-examining the probe, not assuming code_defect. Do not weaken original requirements.'
            def validate(answer): targeted_correction(answer,old,requirement,sources,policy_version=version)
            diagnosis_schema=inspection_schema(object_schema({'disposition':{'enum':['code_defect','probe_defect','ambiguous']},
                **{field:STRING for field in ('reason','request_basis','source_basis','failure_basis')},'replacement':schema},
                ['disposition','reason','request_basis','source_basis','failure_basis'])) if version>=4 else None
            answer=read_only(store,operation_id,repository,instruction,context,validate=validate,**({'schema':diagnosis_schema} if diagnosis_schema else {}))
            check,reason=targeted_correction(answer,old,requirement,sources,policy_version=version)
            if reason: reasons.append(reason)
            if version >= 4: audits.append({key:answer[key] for key in ('disposition','reason','request_basis','source_basis','failure_basis')} | {'check_id':old['id']})
        else:
            def validate(answer): normalize_probe(answer,requirement,identity,policy_version=version)
            answer=read_only(store,operation_id,repository,instruction,context,validate=validate,schema=inspection_schema(schema))
            check=normalize_probe(answer,requirement,identity,policy_version=version)
        if version >= 4 and check['runner'] in {'python','node','shell'}:
            review_raw_probe(store,operation_id,repository,read_only,check,requirement,sources)
        checks[identity]=check;persist()
    result={'checks':list(checks.values()),'corrections':reasons}
    if version >= 4: result['audits']=audits
    validate_probes(result,requirements,policy_version=version)
    return result
