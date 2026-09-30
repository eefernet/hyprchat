"""Final evidence gate. This checker never changes production Settings."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.run_coder_benchmark import matching_baseline,promotion_result


FOCUSED_FIXTURES = {'grouping','tags','pagination','greeting','stock','lookup','todos'}


def focused_verdict(report):
    rows=report.get('results',[])
    reasons=[]
    if len(rows)!=len(FOCUSED_FIXTURES) or {r.get('fixture') for r in rows}!=FOCUSED_FIXTURES:
        reasons.append('Focused pilot must contain all seven fixtures exactly once')
    if any(not row.get('passed') or row.get('false_acceptance') for row in rows):
        reasons.append('Focused pilot has a failed fixture or false acceptance')
    edits=next((r.get('followups',{}) for r in rows if r.get('fixture')=='grouping'),{})
    if (not edits.get('passed') or not edits.get('original_artifact_preserved') or len(edits.get('requests',[]))!=2
            or any(not e.get('evidence',{}).get('passed') or e.get('false_acceptance') for e in edits.get('requests',[]))):
        reasons.append('Both grouping edits and original artifact preservation must pass')
    return {'eligible':not reasons,'reasons':reasons}


def rollout_verdict(first,second,baseline,journeys):
    reasons=[]
    if first.get('workflow_version')!=3 or second.get('workflow_version')!=3:
        reasons.append('Both matrix reports must be persistent workflow evaluations')
    for key in ('source_hashes','models','settings','policy_version'):
        if not first.get(key) or first.get(key)!=second.get(key):reasons.append(f'Matrix {key} differ or are missing')
    if {r.get('job_id') for r in first.get('results',[])} & {r.get('job_id') for r in second.get('results',[])}:
        reasons.append('Matrix runs must use distinct jobs')
    if not matching_baseline(baseline,first):reasons.append('Legacy baseline does not match the evaluated model and allowances')
    if baseline.get('source_hashes')!=first.get('source_hashes'):
        reasons.append('Legacy baseline source does not match the candidate')
    for report in (first,second):
        jobs=[r.get('job_id') for r in report.get('results',[])]
        if any(not job for job in jobs) or len(set(jobs))!=len(jobs):
            reasons.append('Each matrix result needs a distinct nonempty job ID')
    for model in first.get('models',{}):
        old=[r for r in baseline.get('results',[]) if r.get('model')==model]
        for label,report in [('first',first),('second',second)]:
            rows=[r for r in report.get('results',[]) if r.get('model')==model]
            if not promotion_result(rows,old)['promotable']:reasons.append(f'{label} matrix failed quality gates for {model}')
    for key in ('source_hashes','models','settings'):
        if journeys.get(key)!=first.get(key):reasons.append(f'Journey {key} do not match the candidate')
    required={('web',0),('web',1),('web',2),('upload',0),('upload',1)}
    rows=journeys.get('results',[])
    if len(rows)!=len(required) or {(r.get('scenario'),r.get('step')) for r in rows}!=required:
        reasons.append('Journey coverage incomplete')
    if not rows or any(not r.get('evidence',{}).get('passed') or r.get('false_acceptance') for r in rows):
        reasons.append('A delivered-artifact journey check failed')
    if not journeys.get('original_artifacts_preserved'):reasons.append('Original artifact preservation is unverified')
    return {'eligible':not reasons,'reasons':reasons,'deployment_health_required':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('first','second','baseline','journeys'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    result=rollout_verdict(*(json.loads(Path(getattr(args,key)).read_text()) for key in ('first','second','baseline','journeys')))
    print(json.dumps(result,indent=2));raise SystemExit(0 if result['eligible'] else 1)
