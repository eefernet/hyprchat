"""Persisted, bounded policy-4 audits, separate from application repairs."""
import copy
import json
import re
from coder_verification import digest


def failure_fingerprint(check):
    # Ignore timing, log filenames, screenshot IDs and ephemeral ports.
    evidence={k:check[k] for k in ('id','contract_hash','exit_code','error','summary','log_tail','test_count','failure_kind') if k in check}
    if check.get('timeline'):
        evidence['assertions']=[{k:s[k] for k in ('action','selector','value','exact','error') if k in s}
            for s in check['timeline'] if s.get('status')=='failed']
    return digest(re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<port>',json.dumps(evidence,sort_keys=True)))


async def audit_failures(job, result, operate, save):
    """Return (updated job, recheck/block); completed audits survive resumption."""
    probes={p['id']:p for p in job['verification_plan']['checks']}
    failed=[c for c in result['checks'] if not c['passed'] and c.get('origin')=='independent'
        and c.get('id','').removeprefix('requirement-') in probes
        and probes[c['id'].removeprefix('requirement-')]['runner']!='project_tests']
    if not failed: return job,False
    job=await save(job['id'],checks=result['checks'])
    for check in failed:
        identity=check['id'].removeprefix('requirement-')
        history=copy.deepcopy(job.get('probe_audits',[]))
        probe=next(p for p in job['verification_plan']['checks'] if p['id']==identity)
        key={'check_id':identity,'revision_id':job['revision_id'],'probe_hash':digest(probe),'failure_hash':failure_fingerprint(check)}
        matching=[row for row in history if all(row.get(k)==v for k,v in key.items())]
        last=matching[-1] if matching else None
        if last and last['disposition']=='code_defect' and last.get('repair_attempt')==job.get('attempt',0): continue
        while True:
            round_number=len(matching)+1
            if round_number>2:
                return await _block(job,save,'disputed_probe','Independent verification remains disputed after two audits of the same revision, probe and failure.'),True
            prior=job['verification_plan']
            diagnosis,job=await operate(job,'verify',diagnose_probes=prior,
                evidence={'requirements':job['requirements'],'previous_probes':prior,'failed_checks':[check],
                    'audit_key':{**key,'round':round_number},'previous_audits':[a for a in history if a['check_id']==identity][-2:]})
            audit=next((a for a in diagnosis.get('audits',[]) if a['check_id']==identity),None)
            if not audit or audit.get('disposition') not in {'code_defect','probe_defect','ambiguous'}:
                return await _block(job,save,'invalid_probe','The verifier did not provide a valid evidence-based diagnosis.'),True
            row={**key,**audit,'round':round_number,'repair_attempt':job.get('attempt',0),
                'operation_id':job.get('worker_operation'),'cache_key':digest({**key,'round':round_number})}
            row['check_evidence']={k:check[k] for k in ('id','revision_id','log','server_log','screenshots','traces','error','summary') if k in check}
            history.append(row);matching.append(row)
            counts=dict(job.get('probe_replacements',{}))
            if audit['disposition']=='probe_defect':
                replacements={p['id']:p for p in diagnosis['checks']}
                replacement=replacements.get(identity)
                executable_changed = True
                if replacement and job.get('policy_version',1)>=5:
                    from coder_check_schema import executable_identity
                    executable_changed = executable_identity(replacement) != executable_identity(probe)
                unchanged=[p for p in prior['checks'] if p['id']!=identity]
                if (not replacement or set(replacements)!=set(probes) or any(replacements[p['id']]!=p for p in unchanged)
                        or replacement.get('requirement_ids')!=probe['requirement_ids'] or digest(replacement)==digest(probe) or not executable_changed):
                    job=await save(job['id'],probe_audits=history)
                    return await _block(job,save,'invalid_probe','Probe correction changed coverage or unrelated checks.'),True
                if counts.get(identity,0)>=2:
                    job=await save(job['id'],probe_audits=history)
                    return await _block(job,save,'disputed_probe',f'Independent check {identity} exhausted its two probe replacements.'),True
                counts[identity]=counts.get(identity,0)+1
                row['replacement_hash']=digest(replacement);row['previous_probe']=probe;row['replacement']=replacement
                job=await save(job['id'],state='checking',resume_state='checking',probe_audits=history,probe_replacements=counts,
                    verification_plan=diagnosis,verification_hash=digest(diagnosis),event='verification_corrected',failure=None)
                # Recheck the unchanged revision before spending any repair attempt.
                return job,True
            job=await save(job['id'],probe_audits=history,event='verification_audited')
            if audit['disposition']=='code_defect': break
    return job,False


async def _block(job,save,category,message):
    return await save(job['id'],state='blocked',resume_state='checking',blocker=message,event='verification_blocked',
        failure={'category':category,'stage':'checking','recovery_attempted':True,
            'next_action':'Inspect the verification audit and linked check evidence. Resolve the disputed test before further application repairs.'})
