#!/usr/bin/env python3
"""Apply rollout gates without changing a frozen evaluator or retrying failures."""
import argparse
import json
from pathlib import Path


def assess(report):
    expected = {'core':24, 'web':12, 'desktop':12}
    reliability = (report.get('freeze') or {}).get('cohort') == 'reliability'
    families = {}
    failures = []
    for family, count in expected.items():
        trials = [t for t in report['trials'] if t['family']==family]
        successes = 0
        for trial in trials:
            reasons = []
            if not trial.get('finished'): reasons.append('unfinished')
            if not trial.get('passed'): reasons.append(trial.get('error','behavior or lifecycle failed'))
            if len(trial.get('jobs',[]))!=2: reasons.append('initial or follow-up delivery missing')
            for job in trial.get('jobs',[]):
                summary = job.get('verification_summary') or {}
                if not summary.get('checks_ok') or summary.get('revision_id')!=job.get('revision_id'):
                    reasons.append(job['id']+': project checks did not pass on the delivered revision')
                if reliability and (not summary.get('exercised') or summary.get('unverified_interfaces')):
                    reasons.append(job['id']+': controller did not exercise the requested application interfaces')
                if any(c.get('status') == 'failed' for c in summary.get('criteria', [])):
                    reasons.append(job['id']+': controller-owned UI outcome failed')
                if job.get('engine_behavior_version',0)>=1 and any(c.get('supported') and c.get('status')!='passed' for c in summary.get('criteria', [])):
                    reasons.append(job['id']+': supported UI outcome not verified')
                if summary.get('incomplete_reason'):
                    reasons.append(job['id']+': incomplete candidate: '+summary['incomplete_reason'])
                if job.get('stop_limit') in {'model_calls','job_seconds'}:
                    reasons.append(job['id']+': allowance exhausted')
                if not job.get('exact_acceptance_verified'): reasons.append(job['id']+': exact acceptance unverified')
            if reasons: failures.append({'trial':trial['key'],'reasons':reasons})
            else: successes += 1
        families[family]={'successes':successes,'runs':len(trials),'expected':count,
                          'rate':successes/count,'passed':len(trials)==count and successes/count>=.8}
    return {'complete':bool(report.get('complete')),'families':families,'failures':failures,
            'model':report.get('model'),'model_digest':report.get('model_digest'),
            'behavioral_gate_passed':bool(report.get('behavioral_gate_passed')),
            'rollout_behavior_gate_passed':bool(report.get('complete')) and all(f['passed'] for f in families.values()),
            'additional_required_gates':['runtime fault tests','compatibility checks','production opt-in smoke test']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(); value=assess(json.loads(args.report.read_text()))
    text=json.dumps(value,indent=2)+'\n'
    if args.output: args.output.write_text(text)
    print(text,end='')
