"""Exercise user-visible Stop, Resume, working revisions, and web/desktop previews.

Requires a passing development cohort on the same isolated origin. Use a new
output file and retain failed runs; never send replacement prompts automatically.
"""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from engine_cases import DEVELOPMENT
from engine_cases_everyday import REHEARSAL
from run_engine_chat import api, reopen, submit, await_job, archive_and_accept, save


def preview(page, job, case, out):
    reopen(page,job['conversation_id'])
    current=api(page,"/api/coder/workflows/"+job['id'])
    artifact=current.get('artifact') or current['candidate_artifact']
    link=page.locator(f'a[download][href*="/api/artifacts/{artifact["id"]}/download"]').first
    card=link.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' dj-card ')][1]")
    card.get_by_role('button',name='View details').click()
    dialog=page.locator('dialog.dj-dialog[open]')
    dialog.get_by_role('button',name='Preview',exact=True).click()
    dialog.get_by_label('Application type').select_option(case['kind'])
    dialog.get_by_label('Launch command').fill(case['command'])
    endpoint=f"/api/coder/workflows/{job['id']}/previews"
    opened=None
    try:
        with page.expect_response(lambda r:urlsplit(r.url).path==endpoint and r.request.method=='POST') as response:
            dialog.get_by_role('button',name='Launch preview').click()
        opened=response.value.json()
        if not response.value.ok or opened['revision']!=job['revision_id']:
            raise RuntimeError('Preview selected a different revision or failed to launch')
        dialog.get_by_text('Preview running',exact=True).wait_for(timeout=240000)
        dialog.locator('canvas').first.wait_for(state='visible',timeout=45000)
        image=out.parent/(case['id']+'-preview.png')
        dialog.screenshot(path=str(image))
        receipt = {'id':opened['id'],'revision':opened['revision'],'screenshot':image.name}
        if case['kind']=='desktop':
            control_path=f"{endpoint}/{opened['id']}/control"
            history=[]
            for button,action in [('Keep running','pin'),('Stop','stop'),('Restart','restart'),('Stop','stop')]:
                with page.expect_response(lambda r:urlsplit(r.url).path==control_path and r.request.method=='POST') as controlled:
                    dialog.get_by_role('button',name=button,exact=True).click()
                response=controlled.value
                record=response.json()
                if (not response.ok or response.request.post_data_json.get('action')!=action
                        or record.get('id')!=opened['id'] or record.get('revision')!=job['revision_id']
                        or record.get('lifecycle')!='pinned'):
                    raise RuntimeError('Pinned preview action changed its identity, revision or lifecycle')
                if action=='restart':
                    dialog.get_by_text('Preview running',exact=True).wait_for(timeout=240000)
                    dialog.locator('canvas').first.wait_for(state='visible',timeout=45000)
                elif action=='stop' and record.get('status')!='closed':
                    raise RuntimeError('Pinned preview did not stop')
                history.append({'action':action,'status':record['status']})
            receipt['controls']=history
        else:
            dialog.get_by_role('button',name='Close preview').click()
            dialog.get_by_role('button',name='Close preview').wait_for(state='hidden')
        return receipt
    finally:
        if opened and opened.get('id') and opened.get('revision')==job['revision_id']:
            url=f"{endpoint}/{opened['id']}"
            if api(page,url).get('status') in ('starting','running'):
                closed=page.evaluate("async url => (await fetch(url,{method:'DELETE'})).ok",url)
                if not closed:
                    raise RuntimeError('Failed to stop this rehearsal preview: '+opened['id'])
        if dialog.is_visible():
            dialog.get_by_role('button',name='Close job details').click()


def wait_state(page, job_id, predicate, seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        job=api(page,f'/api/coder/workflows/{job_id}')
        if predicate(job):return job
        page.wait_for_timeout(600)
    raise RuntimeError('Timed out waiting for the recorded job state')


def working_edits(page, source, out, report, env):
    reopen(page,source['conversation_id'])
    case=REHEARSAL[0]
    history=f"/api/coder/workflows?conversation_id={source['conversation_id']}"
    previous={j['id'] for j in api(page,history)}
    versions=f"/api/coder/projects/{source['project_id']}/versions"
    accepted=api(page,versions)['accepted']['revision_id']
    if accepted!=source['revision_id']:
        raise RuntimeError('Expected the reviewed Python baseline')
    parent=accepted
    persist=lambda:save(out,report)
    for index,prompt in enumerate((case['task'],case['followup'])):
        turn={}
        report['turns'].append(turn)
        submit(page,prompt,turn,persist,source['conversation_id'])
        if index==0:
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                new=[j for j in api(page,history) if j['id'] not in previous]
                if len(new)==1:break
                page.wait_for_timeout(500)
            else:raise RuntimeError('No unique workflow for Stop rehearsal')
            job_id=new[0]['id']
            wait_state(page,job_id,lambda j:j['state']=='coding',300)
            region=page.get_by_role('region',name='Background coding job')
            region.get_by_role('button',name='Stop',exact=True).click()
            stopped=wait_state(page,job_id,lambda j:j['state']=='cancelled',120)
            counts=(stopped['calls_used'],stopped['seconds_used'],stopped['repair_round'])
            with page.expect_response(lambda r:urlsplit(r.url).path==f'/api/coder/workflows/{job_id}/resume') as resumed:
                page.get_by_role('button',name='Resume',exact=True).last.click()
            response=resumed.value.json()
            if not resumed.value.ok or response['id']!=job_id or any(a<b for a,b in zip(
                    (response['calls_used'],response['seconds_used'],response['repair_round']),counts)):
                raise RuntimeError('Resume lost the operation identity or consumed allowance')
            report['stop_resume']={'job':job_id,'consumed_at_stop':counts}
            persist()
        job=await_job(page,turn,previous,persist,env['job_seconds'])
        if job['source_revision_id']!=parent or api(page,versions)['accepted']['revision_id']!=accepted:
            raise RuntimeError('Follow-up inherited the wrong revision or changed the accepted pointer')
        archive_and_accept(page,job,turn,case,index,out,env['judge_command'],persist,promote=bool(index))
        turn.update(source_revision=parent,revision=job['revision_id'],accepted_before=accepted)
        parent=job['revision_id']
        previous.add(job['id'])
        persist()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('development',type=Path)
    parser.add_argument('--environment', type=Path, help='Use an updated private judge for the same tested candidate')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from sys import path
    path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    from qualify_engine_report import assess
    data=json.loads(args.development.read_text())
    if not assess(data,require_composer=True)['user_workflow_gate_passed']:
        raise ValueError('A passing development cohort is required')
    env=data['freeze']['environment']
    if args.environment:
        current=json.loads(args.environment.read_text())
        if any(current.get(k)!=env[k] for k in ('url','release_id','model_digest','profiles','settings')):
            raise ValueError('The rehearsal must use the same tested candidate')
        env=current
    if not env.get('isolated_candidate') or urlsplit(env['url']).port in (None,80,443,8000):
        raise ValueError('Use a disposable candidate origin')
    if args.output.exists():raise ValueError('Use a new rehearsal output')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'turns':[],'previews':{},'judge_sha256':env['judge_sha256']}
    save(args.output,report)
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1024})
        page.goto(env['url']);page.get_by_role('button',name='Sign In',exact=True).click()
        for name in ('dev-web','dev-qt','dev-python'):
            source=next(t['jobs'][-1] for t in data['trials'] if t['case']==name)
            if name=='dev-python':
                working_edits(page,source,args.output,report,env)
            else:
                case=next(c for c in DEVELOPMENT if c['id']==name)
                report['previews'][name]=preview(page,source,case,args.output)
                save(args.output,report)
        browser.close()
    report['passed']=True
    save(args.output,report)


if __name__=='__main__':
    main()
