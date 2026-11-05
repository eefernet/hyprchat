#!/usr/bin/env python3
"""Grade a frozen roster against its private results and delivered revisions."""
import argparse
from collections import Counter
import json
import math
import statistics
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/evals'))
from engine_cases import CASES, DEVELOPMENT, private_probes
from engine_cases_reliability import CASES as RELIABILITY


def roster(cohort):
    if cohort in {'everyday','breadth','pocket'}:
        from engine_cases_everyday import CASES as everyday, POCKET, breadth_cases
        if cohort == 'breadth':
            return breadth_cases(), 1
        return (everyday, 3) if cohort == 'everyday' else (POCKET, 1)
    return {'classic': (CASES, 3), 'reliability': (RELIABILITY, 3),
            'development': (DEVELOPMENT, 2)}[cohort]


def check_job(job, reliability, errors):
    reasons = []
    summary = job.get('verification_summary') or {}
    revision = job.get('revision_id')
    archive = job.get('candidate_artifact') or {}
    private = job.get('held_out') or {}
    checks = job.get('checks', summary.get('checks', []))
    private_rows = private.get('checks', [])
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40,64}', revision):
        errors.append('missing or malformed delivered revision')
    if not summary.get('checks_ok') or summary.get('revision_id') != revision:
        reasons.append('project checks did not pass on the delivered revision')
    if not checks or any(c.get('revision_id') != revision for c in checks):
        errors.append('missing or stale controller check evidence')
    if not any(c.get('passed') is True and c.get('outcome', 'passed') == 'passed' for c in checks):
        reasons.append('no passing controller check')
    if any(c.get('outcome') == 'failed' for c in checks) or summary.get('failures'):
        reasons.append('controller reported a failed requirement or check')
    if reliability and (summary.get('exercised') is not True or summary.get('unverified_interfaces')):
        reasons.append('controller did not exercise the requested application interfaces')
    criteria = summary.get('criteria') or []
    if any(c.get('status') == 'failed' for c in criteria):
        reasons.append('controller-owned UI outcome failed')
    if any(c.get('supported') and c.get('status') != 'passed' for c in criteria):
        reasons.append('supported UI outcome not verified')
    if summary.get('incomplete_reason'):
        reasons.append('incomplete candidate: ' + summary['incomplete_reason'])
    patch = job.get('last_patch') or {}
    execution_status = str(patch.get('execution_status','')).rsplit('.',1)[-1].lower()
    if patch.get('agent_finished') is False and execution_status in {'stuck','error'} and not summary.get('incomplete_reason'):
        reasons.append('coding worker stopped before completion: ' + execution_status)
    if job.get('stop_limit') in {'model_calls','job_seconds'}:
        reasons.append('allowance exhausted')
    if job.get('exact_acceptance_verified') is not True:
        reasons.append('exact acceptance unverified')
    digest = archive.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or private.get('archive_sha256') != digest:
        errors.append('private observations are not bound to the delivered archive')
    if (archive.get('metadata') or {}).get('revision_id', revision) != revision:
        errors.append('artifact metadata references a different revision')
    if private.get('passed') is not True or not private_rows or any(c.get('passed') is not True for c in private_rows):
        errors.append('successful trial lacks passing private checks')
    return reasons


def verify_private_records(case, index, rows):
    """Re-read measured outcomes instead of trusting per-row pass flags."""
    if case.get('kind') == 'pocket':
        ids = ['mobile viewport','add feedback','saved task','completed','completion persisted',
               'cleared field','clear feedback','clearing kept completed task','visible blank error',
               'validation clears','distinct start','exact result transition','two delete controls','deletion persisted']
        expected = [True,'Task added',1,True,True,'','Ready' if index else '',True,True,False,
                    'Task added','Ready' if index else '',2,0]
        return [r.get('id') for r in rows] == ids and [r.get('actual') for r in rows] == expected
    if case['family']=='core' or case.get('kind')=='http':
        expected = private_probes(case, bool(index))
        if len(expected)!=len(rows):
            return False
        http = case.get('kind')=='http'
        for number, (probe, row) in enumerate(zip(expected,rows)):
            if row.get('id') != f"{'http' if http else 'probe'}-{number}" or row.get('expected')!=probe:
                return False
            code = row.get('exit')
            if type(code) is not int or (code==0 if probe.get('nonzero') else code!=probe.get('code',200 if http else 0)):
                return False
            stdout = row.get('actual' if http else 'stdout')
            if not isinstance(stdout,str) or 'stdout' in probe and stdout!=probe['stdout']:
                return False
            if 'json' in probe:
                try:
                    if json.loads(stdout)!=probe['json']:
                        return False
                except ValueError:
                    return False
            stderr = row.get('stderr','')
            if probe.get('stderr_nonempty') and not stderr.strip():
                return False
            if 'stderr_contains' in probe and probe['stderr_contains'].lower() not in stderr.lower():
                return False
        return True
    steps = case['steps'] + (case['followup_steps'] if index else [])
    expected = [(f'step-{i}',k,step[k]) for i,step in enumerate(steps)
                for k in ('equals','contains','excludes','json','absent_name','feedback_pattern') if k in step]
    if len(expected)!=len(rows):
        return False
    for (identity, comparison, value), row in zip(expected,rows):
        if (row.get('id'),row.get('comparison'),row.get('expected'))!=(identity,comparison,value):
            return False
        actual = row.get('actual')
        if comparison=='equals' and actual!=value:
            return False
        if comparison in ('excludes','absent_name') and (not isinstance(actual,(list,str)) or value in actual):
            return False
        if comparison=='contains' and (not isinstance(actual,str) or value not in actual):
            return False
        if comparison=='feedback_pattern' and (not isinstance(actual,str) or not re.search(value,actual)):
            return False
        if comparison=='json':
            try:
                if json.loads(actual)!=value:
                    return False
            except (TypeError,ValueError):
                return False
    return True


def conflicting_ui_evidence(job, case, index):
    """Identify an exact action/property/target the controller verified incorrectly."""
    private = job.get('held_out') or {}
    artifact = job.get('candidate_artifact') or {}
    revision = job.get('revision_id')
    if (not revision or not artifact.get('sha256') or private.get('archive_sha256') != artifact['sha256']
            or not case.get('steps')):
        return []
    steps = case['steps'] + (case['followup_steps'] if index else [])
    rows = {row.get('id'):row for row in private.get('checks', [])}
    conflicts = []
    click = None
    for i, step in enumerate(steps):
        if step['action'] == 'click':
            click = step.get('name')
        elif step['action'] not in ('value','text','observe'):
            click = None
        row = rows.get(f'step-{i}', {})
        if not click or 'equals' not in step or row.get('comparison') != 'equals' or 'actual' not in row:
            continue
        if row.get('expected') != step['equals'] or row['actual'] == step['equals']:
            continue
        for criterion in (job.get('verification_summary') or {}).get('criteria', []):
            if (criterion.get('status') != 'passed' or criterion.get('revision_id') != revision
                    or criterion.get('expected') != step['equals'] or criterion.get('property') != step['action']
                    or (criterion.get('action') or {}).get('name') != click):
                continue
            targets = [target for evidence in criterion.get('evidence', []) if evidence.get('revision_id') == revision
                       for target in evidence.get('targets', [])]
            if any(step.get(k) and step[k] == target.get(k) for target in targets for k in ('selector','name')):
                conflicts.append({'job':job['id'],'criterion':criterion['id'],'revision':revision,
                                  'private_step':i,'expected':step['equals'],'actual':row['actual']})
    return conflicts


def assess(report, *, require_composer=False):
    errors, failures = [], []
    conflicts = []
    cohort = (report.get('freeze') or {}).get('cohort', 'classic')
    if report.get('development') is True:
        cohort = 'development'
    cases, repeats = roster(cohort)
    schedule = {(case['id'], rep):case for case in cases for rep in range(1, repeats+1)}
    totals = Counter(c['family'] for c in schedule.values())
    seen, jobs_seen, projects_seen, success_keys = set(), set(), set(), set()
    observed, successes, behavior = Counter(), Counter(), Counter()
    composer = report.get('entrypoint') == 'daedalus-composer'
    for trial in report['trials']:
        slot = (trial.get('case'), trial.get('repeat'))
        case = schedule.get(slot)
        key = trial.get('key', '')
        if (not case or type(slot[1]) is not int or key != f'{slot[0]}-{slot[1]}'
                or slot in seen or case['family'] != trial.get('family')):
            errors.append(f'{key}: duplicate, unexpected or misidentified trial')
            continue
        seen.add(slot)
        family = case['family']
        observed[family] += 1
        if not trial.get('finished'):
            errors.append(f'{key}: unfinished trial')
        jobs = trial.get('jobs', [])
        for index, job in enumerate(jobs):
            identity = job.get('id')
            if not identity or identity in jobs_seen:
                errors.append(f'{key}: missing or duplicate job ID')
            jobs_seen.add(identity)
            if composer:
                conflicts.extend(conflicting_ui_evidence(job, case, index))
        if trial.get('passed') is not True:
            failures.append({'trial':key,'reasons':[trial.get('error') or 'behavior or lifecycle failed']})
            continue
        behavior[family] += 1
        invalid, reasons = [], []
        if len(jobs) != 2:
            invalid.append('successful trial requires two jobs')
        for index, job in enumerate(jobs):
            reasons.extend(check_job(job, cohort != 'classic' or composer, invalid))
            if composer:
                if index==0 and job.get('project_id') in projects_seen:
                    invalid.append('fresh trial reused a previous project')
                if index==0:
                    projects_seen.add(job.get('project_id'))
                raw = job.get('checks') or []
                if not any(c.get('passed') and c.get('coverage_observed') and c.get('execution_succeeded')
                           and (c.get('source_bindings') or c.get('service_bindings')) for c in raw):
                    reasons.append('no controller-observed execution bound to the application')
                if not verify_private_records(case,index,(job.get('held_out') or {}).get('checks',[])):
                    invalid.append('private observations do not match the frozen assertions')
                receipt = job.get('composer') or {}
                prompt = case['task'] if index == 0 else case['followup']
                if (not receipt.get('message_id') or not job.get('conversation_id') or not job.get('project_id')
                        or receipt.get('prompt') != prompt or job.get('original_user_request') != prompt
                        or receipt.get('workflow_ids') != [job['id']]
                        or receipt.get('download_sha256') != (job.get('candidate_artifact') or {}).get('sha256')):
                    invalid.append('missing or conflicting composer, saved prompt, workflow or download evidence')
                if str(receipt.get('message_id')) != str(job.get('chat_request_key')):
                    invalid.append('workflow does not reference its saved user turn')
                if job.get('model') != report.get('model') or job.get('model_digest') != report.get('model_digest'):
                    invalid.append('coding model identity differs from the frozen run')
                if job.get('mode') != ('edit_project' if index or case.get('upload') else 'build_from_prompt'):
                    invalid.append('incorrect user workflow mode')
        if len(jobs) == 2 and (composer or 'source_revision_id' in jobs[1] or 'project_id' in jobs[0]):
            parent, edit = jobs
            if (not parent.get('project_id') or edit.get('project_id') != parent['project_id']
                    or edit.get('source_revision_id') != parent.get('revision_id')
                    or edit.get('conversation_id') != parent.get('conversation_id')):
                invalid.append('follow-up is not bound to the initial project and reviewed source revision')
        errors.extend(f'{key}: {reason}' for reason in invalid)
        if reasons or invalid:
            failures.append({'trial':key,'reasons':reasons+invalid})
        else:
            successes[family] += 1
            success_keys.add(key)
    if seen != schedule.keys():
        errors.append('incomplete case/repetition roster')
    floor = 1.0 if cohort in ('development','pocket','breadth') else .8
    families = {family:{'successes':successes[family], 'runs':observed[family], 'expected':count,
                        'rate':successes[family]/count, 'passed':successes[family]/count >= floor}
                for family, count in totals.items()}
    behavioral = seen == schedule.keys() and all(behavior[f]/count >= floor for f,count in totals.items())
    if report.get('behavioral_gate_passed') is not behavioral:
        errors.append('top-level behavior verdict disagrees with the trials')
    if (require_composer or cohort in ('everyday','pocket','breadth')) and not composer:
        errors.append('release qualification requires the actual Daedalus composer')
    complete = report.get('complete') is True and not errors
    passed = complete and all(f['passed'] for f in families.values()) and not conflicts
    for family, row in families.items():
        trials = [t for t in report['trials'] if t.get('family')==family]
        jobs = [j for t in trials for j in t.get('jobs',[])]
        seconds = sorted(t['finished']-t['started'] for t in trials
                         if isinstance(t.get('started'),(int,float)) and isinstance(t.get('finished'),(int,float)))
        row['initial_private_passes'] = sum(bool(t.get('jobs')) and t['jobs'][0].get('held_out',{}).get('passed') is True for t in trials)
        row['followup_private_passes'] = sum(len(t.get('jobs',[]))==2 and t['jobs'][1].get('held_out',{}).get('passed') is True for t in trials)
        row['calls_charged'] = sum(j.get('calls_used',0) or 0 for j in jobs)
        row['active_seconds'] = sum(j.get('seconds_used',0) or 0 for j in jobs)
        row['repairs'] = sum(j.get('repair_round',0) or 0 for j in jobs)
        row['median_wall_seconds'] = statistics.median(seconds) if seconds else None
        row['p90_wall_seconds'] = seconds[math.ceil(.9*len(seconds))-1] if seconds else None
        row['successes_without_app_repair'] = sum(t.get('key') in success_keys and not any(j.get('repair_round') for j in t.get('jobs',[])) for t in trials)
    return {'grader_version':2,'cohort':cohort,'complete':complete,'families':families,'failures':failures,
            'invalid_report':errors,'confirmed_false_verifications':conflicts,
            'behavioral_gate_passed':behavioral,'rollout_behavior_gate_passed':passed,
            'user_workflow_gate_passed':passed and composer,'model':report.get('model'),'model_digest':report.get('model_digest')}


def compare(previous, candidate):
    """Only compare identical prompt banks, model roles, resources and judges."""
    before = assess(previous, require_composer=True)
    after = assess(candidate, require_composer=True)
    constraints = ('model','model_digest','chat_model','chat_digest','profiles','settings','judge_sha256',
                   'judge_bank_sha256','sdk_version','limits','cache_policy')
    old = previous['freeze']['environment']
    new = candidate['freeze']['environment']
    mismatches = [k for k in constraints if not old.get(k) or old[k] != new.get(k)]
    if not previous['freeze'].get('bank_sha256') or previous['freeze']['bank_sha256'] != candidate['freeze'].get('bank_sha256'):
        mismatches.append('prompt bank')
    if not old.get('release_id') or not new.get('release_id') or old['release_id'] == new['release_id']:
        mismatches.append('two identified harness releases required')
    if any(report.get(k) != env.get(k) for report,env in ((previous,old),(candidate,new)) for k in ('model','model_digest')):
        mismatches.append('report and frozen model identity disagree')
    if before['cohort'] != 'everyday' or after['cohort'] != 'everyday':
        mismatches.append('expected everyday cohort')
    if not before['complete'] or not after['complete']:
        mismatches.append('incomplete or invalid comparison')
    if mismatches:
        return {'comparable':False,'reasons':mismatches,'improvement_demonstrated':False}
    old_failed = {f['trial'] for f in before['failures']}
    new_failed = {f['trial'] for f in after['failures']}
    slots = {t['key'] for t in candidate['trials']}
    wins, losses = sorted(old_failed-new_failed), sorted(new_failed-old_failed)
    net = len(wins)-len(losses)
    same_or_better = all(f['successes'] >= before['families'][name]['successes'] for name,f in after['families'].items())
    interval = paired_interval(candidate['trials'], set(wins), set(losses))
    return {'comparable':True,'wins':wins,'losses':losses,'ties':len(slots)-len(wins)-len(losses),
            'net_successes':net,'change_percentage_points':100*net/len(slots),
            'paired_case_bootstrap_95_percentage_points':interval,
            'improvement_demonstrated':bool(after['user_workflow_gate_passed'] and same_or_better and net>=5)}


def paired_interval(trials, wins, losses):
    """Resample cases within each family, retaining all repetitions and both arms."""
    groups = {}
    for trial in trials:
        family = groups.setdefault(trial['family'], Counter())
        family[trial['case']] += int(trial['key'] in wins) - int(trial['key'] in losses)
    clusters = [list(family.values()) for family in groups.values()]
    rng = random.Random(0)
    changes = sorted(100 * sum(sum(rng.choices(group, k=len(group))) for group in clusters) / len(trials)
                     for _ in range(10000))
    return [changes[249], changes[9749]]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--compare',type=Path,help='Frozen previous everyday composer report')
    parser.add_argument('--require-composer', action='store_true')
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text())
        value = assess(report, require_composer=args.require_composer)
        if args.compare:
            value['comparison'] = compare(json.loads(args.compare.read_text()), report)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        value = {'grader_version':2,'rollout_behavior_gate_passed':False,'invalid_report':[f'Malformed report: {error}']}
    rendered = json.dumps(value, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    print(rendered,end='')
    ok = value['rollout_behavior_gate_passed'] and value.get('comparison',{}).get('improvement_demonstrated', True)
    sys.exit(0 if ok else 1)
