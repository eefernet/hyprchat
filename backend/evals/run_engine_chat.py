"""Submit qualification prompts through a real, isolated HyprChat browser session.

The judge command receives a JSON request on stdin and returns the independent
verdict for the downloaded archive on stdout. It runs outside both model roles.
"""
import argparse
import fcntl
import hashlib
import io
import json
import re
import shutil
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
import zipfile

from playwright.sync_api import sync_playwright, Error as PlaywrightError
from engine_cases import PROMPT_BANK_VERSION
import engine_cohorts

TERMINAL = {'ready_for_review','completed','failed','blocked','cancelled','waiting_for_input'}
BANKS = engine_cohorts.COMPOSER


class CampaignInterrupted(RuntimeError):
    """Preserve the turn journal. A submitted message must never be sent twice."""


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n')
    temp.replace(path)


def _get(page, path):
    return page.evaluate("""async path => {
      const response = await fetch(path, {signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw Error(path+' HTTP '+response.status);
      return response.json();
    }""", path)


def api(page, path):
    """Only read-only polling may retry; prompt POSTs keep their intent journal."""
    origin = urlsplit(page.url)[:2]
    for attempt in range(3):
        try:
            if urlsplit(page.url)[:2] != origin:
                raise CampaignInterrupted('Qualification origin changed during polling')
            result = _get(page, path)
            if attempt:
                expected = getattr(page, '_qualification_user', None)
                current = _get(page, '/api/users/current')['user']['id']
                if expected is None or current != expected:
                    raise CampaignInterrupted('Profile identity changed or is unavailable after polling recovery')
                record = getattr(page, '_record_recovery', None)
                if record: record({'path': path, 'attempts': attempt + 1, 'at': time.time()})
            return result
        except PlaywrightError as error:
            transient = re.search(r'Failed to fetch|fetch failed|NetworkError|Load failed|ERR_CONNECTION|TimeoutError|signal.*timed out', str(error), re.I)
            if not transient or attempt == 2:
                raise CampaignInterrupted('Read-only polling interrupted: ' + str(error)[:1000]) from error
            page.wait_for_timeout(500 * (attempt + 1))


def completed_reply(conv, message):
    """Server delivery completion follows awaited tools; never infer it from prose."""
    messages = conv.get('messages', [])
    index = next((i for i, m in enumerate(messages) if str(m['id']) == str(message['id'])), None)
    if index is None: return None
    following = messages[index + 1:]
    if any(m['role'] == 'user' for m in following): return None
    replies = [m for m in following if m['role'] == 'assistant']
    if len(replies) != 1: return None
    metadata = replies[0].get('metadata') or {}
    return replies[0] if metadata.get('delivery_status') == 'completed' and metadata.get('in_progress') is False else None


def verify_judge(environment, cohort, bank_sha256):
    """Fail before opening the browser, even on a resumed campaign."""
    from engine_identity import validate
    command = environment['judge_command']
    argv = command['argv'] if isinstance(command, dict) else command
    adapter = Path(environment['judge_adapter_path']).resolve()
    if (str(adapter) not in argv or hashlib.sha256(adapter.read_bytes()).hexdigest()
            != environment.get('judge_adapter_sha256')):
        raise ValueError('Judge transport adapter differs from the frozen environment')
    result = subprocess.run(argv, input=json.dumps({'kind': 'identity'}) + '\n',
                            text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise CampaignInterrupted('Cannot verify private judge identity: ' + result.stderr[-1500:])
    identity = json.loads(result.stdout)
    validate(identity, environment, cohort, bank_sha256)
    return identity


def verify_coding_model(job, environment):
    if job.get('engine') != 'openhands159' or job.get('model') != environment['model']:
        raise CampaignInterrupted('Coding model identity diverged from frozen deployment')
    if job.get('model_digest') == environment['model_digest']:
        return
    # Source admission can fail before model resolution. Retain that first user
    # outcome as a workflow failure, without retrying its prompt or inventing a
    # model identity. Missing identity after any execution remains an interruption.
    if (not job.get('model_digest') and job.get('state') == 'blocked'
            and (job.get('failure') or {}).get('stage') in {'queued', 'source_preparation'}
            and job.get('calls_used') == 0 and not job.get('operation_calls')
            and not job.get('last_patch') and not job.get('candidate_artifact')):
        raise RuntimeError('Workflow stopped before coding: ' + (job.get('blocker') or 'Source admission failed'))
    raise CampaignInterrupted('Coding model identity diverged from frozen deployment')


def select_agent(page):
    page.get_by_role('button',name='New Chat',exact=True).click()
    active = page.locator('.hc-app-header .hc-chip').filter(has_text=re.compile(r'Agent\s*(?:🏛️?\s*)?Daedalus'))
    for attempt in range(2):
        page.get_by_role('button',name='Agents',exact=True).first.click()
        name = page.get_by_text(re.compile(r'^(?:🏛️?\s*)?Daedalus$')).first
        card = name.locator("xpath=ancestor::div[.//button[normalize-space()='Use Agent']][1]")
        try:
            card.get_by_role('button',name='Use Agent').click()
            break
        except PlaywrightError as error:
            if active.is_visible():
                break
            # Late account hydration can return to Chat after opening Agents.
            # Recover only this observable pre-submit navigation, once. A
            # missing profile in an otherwise visible agent list still fails.
            returned_to_chat = (page.locator('textarea:visible').is_visible()
                                and page.get_by_role('button',name='Use Agent',exact=True).count() == 0)
            if attempt or ('detached' not in str(error).lower() and not returned_to_chat):
                raise
    page.locator('textarea:visible').wait_for()
    active.wait_for()


def attach(page, files):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle,'w') as archive:
        for name, content in files.items():
            archive.writestr(name,content)
    with page.expect_response(lambda r:'/api/coder/upload-project' in r.url and r.request.method=='POST') as uploaded:
        page.locator('input[type=file][multiple]').first.set_input_files(
            {'name':'project.zip','mimeType':'application/zip','buffer':bundle.getvalue()})
    if not uploaded.value.ok:
        raise RuntimeError(f'Project upload returned HTTP {uploaded.value.status}')
    return uploaded.value.json()


def submit(page, prompt, turn, persist, conversation=None):
    if turn.get('intent'):
        # A crash may occur between the browser sending the message and the
        # network observer saving its ID. Reconnect using persisted messages.
        prior = set(turn.get('existing_conversations', []))
        candidates = [conversation] if conversation else [
            c['id'] for c in api(page,'/api/conversations?limit=500') if c['id'] not in prior]
        matches = []
        for cid in candidates:
            conv = api(page,f'/api/conversations/{cid}')
            matches.extend(cid for m in conv['messages'] if m['role']=='user' and m['content']==prompt
                           and str(m['id']) not in turn.get('prior_message_ids', []))
        if len(matches)!=1:
            raise CampaignInterrupted('Submission outcome uncertain. No duplicate message was sent')
        turn.update(submitted=True,conversation_id=matches[0])
        persist()
        reopen(page,matches[0])
        return
    turn['existing_conversations'] = [c['id'] for c in api(page,'/api/conversations?limit=500')]
    if conversation:
        turn['prior_message_ids'] = [str(m['id']) for m in api(page,f'/api/conversations/{conversation}')['messages']]
    text = page.locator('textarea:visible')
    text.fill(prompt)
    turn.update(intent=True, prompt=prompt, started=time.time())
    persist()
    with page.expect_request(lambda r:urlsplit(r.url).path=='/api/chat/stream' and r.method=='POST',timeout=45000) as sent:
        page.get_by_role('button',name='Send message',exact=True).click()
    body = sent.value.post_data_json
    turn.update(submitted=True, conversation_id=body['conversation_id'])
    persist()


def prepare_upload(page, case, trial, persist, allowance):
    """The upload control belongs to an existing conversation. Establish one
    through chat and verify that merely attaching source triggers no coding."""
    setup=trial.setdefault('upload_setup',{})
    if not setup.get('submitted'):
        submit(page,"I’m going to attach an existing project. Please wait for the file before doing any work.",setup,persist)
    cid=setup['conversation_id']
    deadline = time.monotonic()+300
    while time.monotonic()<deadline:
        messages=api(page,f'/api/conversations/{cid}')['messages']
        if any(m['role']=='assistant' and m.get('content') for m in messages):
            break
        page.wait_for_timeout(500)
    else:
        raise CampaignInterrupted('Upload preparation message did not finish')
    page.locator('button.hc-send-btn[data-state=send]').wait_for(timeout=300000)
    if not trial.get('question') and api(page,f'/api/coder/workflows?conversation_id={cid}'):
        raise RuntimeError('Setup message improperly dispatched project work')
    if not setup.get('attachment'):
        setup['attachment']=attach(page,case['upload'])
        persist()
    qa=trial.setdefault('question',{})
    if not qa.get('submitted'):
        submit(page,'Which file reads standard input in the uploaded project, and what does it do with the text? Cite the file and line. Just explain it.',qa,persist,cid)
    job=await_job(page,qa,set(),persist,allowance)
    if job['mode']!='ask_uploaded_project' or job['state']!='completed' or not job.get('qa_revision_id'):
        raise RuntimeError('The project question did not complete through the read-only worker')
    source=case['upload']['lines.mjs'].splitlines()
    matches=re.findall(r'lines\.mjs:(\d+)(?:[-–](\d+))?',job.get('answer',''))
    if not any(1<=int(start)<=int(end or start)<=len(source) and
               'readFileSync' in ' '.join(source[int(start)-1:int(end or start)]) for start,end in matches):
        raise RuntimeError('Project answer lacks a valid citation to the input-reading line')
    hashes = {}
    for name, content in case['upload'].items():
        observed=page.evaluate("""async ({url,name})=>{
          const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({operation:'bytes',path:name})});
          if(!r.ok)throw Error('Cannot inspect Q&A source: '+r.status);
          return r.json();
        }""", {'url':f"/api/coder/workflows/{job['id']}/inspect",'name':name})
        digest=hashlib.sha256(content.encode()).hexdigest()
        if observed.get('sha256')!=digest or observed.get('content')!=content:
            raise RuntimeError('Read-only question changed uploaded source: '+name)
        hashes[name]=digest
    qa.update(job_id=job['id'],revision=job['qa_revision_id'],answer=job['answer'],
              source_hashes=hashes)
    persist()
    return cid


def reopen(page, conversation):
    """Open the saved conversation by its id. Sidebar titles repeat when prompts repeat, so a title cannot identify it."""
    data = api(page, f'/api/conversations/{conversation}')
    if data.get('id') != conversation:
        raise CampaignInterrupted('Saved conversation is not available to this profile')
    saved = [m['id'] for m in data.get('messages', []) if m.get('id')]
    try:
        # The application's own source-link route: it loads the conversation and then clears the hash.
        page.evaluate("id => { window.location.hash = '#source?kind=chat&id=' + encodeURIComponent(id); }", conversation)
        if saved:
            page.locator(f'#saved-message-{saved[-1]}').wait_for(state='attached', timeout=20000)
        else:
            page.wait_for_function("() => !window.location.hash", timeout=20000)
    except PlaywrightError as error:
        raise CampaignInterrupted('Cannot reopen the saved conversation: ' + str(error)[:500]) from error


def await_job(page, turn, earlier, persist, allowance):
    cid = turn['conversation_id']
    deadline = time.monotonic() + allowance + 600
    route_deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        conv = api(page,f'/api/conversations/{cid}')
        identity = {key: conv.get(key) for key in ('id', 'user_id', 'model_config_id')}
        if identity['id'] != cid or turn.get('conversation_identity', identity) != identity:
            raise CampaignInterrupted('Saved conversation/profile identity changed during polling')
        turn.setdefault('conversation_identity', identity)
        messages = [m for m in conv['messages'] if m['role']=='user' and m['content']==turn['prompt']]
        if len(messages)>1:
            raise RuntimeError('Duplicate saved user messages')
        history = api(page,f'/api/coder/workflows?conversation_id={cid}')
        new = [j for j in history if j['id'] not in earlier and j.get('workflow_version')==3]
        if len(new)>1:
            raise RuntimeError('One user turn created multiple workflows')
        if new and messages:
            j = new[0]
            if j.get('conversation_id') != cid or turn.get('job_id', j['id']) != j['id']:
                raise CampaignInterrupted('Saved workflow identity changed during polling')
            if str(j.get('chat_request_key')) != str(messages[0]['id']) or j.get('original_user_request')!=turn['prompt']:
                raise RuntimeError('Workflow is not bound to the exact user message')
            if turn.get('job_id') != j['id']:
                turn.update(job_id=j['id'], message_id=messages[0]['id'])
                persist()
            if j['state'] in TERMINAL:
                turn['workflow_ids'] = [j['id']]
                return j
        if not new and messages:
            reply = completed_reply(conv, messages[0])
            if reply:
                # Re-read after the server's terminal delivery marker so a
                # workflow committed between our two GETs cannot be missed.
                current = api(page, f'/api/coder/workflows?conversation_id={cid}')
                if any(j['id'] not in earlier and j.get('workflow_version') == 3 for j in current):
                    continue
                turn.update(message_id=messages[0]['id'], response_id=reply['id'],
                            dispatch_outcome='completed_without_workflow')
                persist()
                raise RuntimeError('Daedalus chat completed without starting a workflow for this message')
        if not new and time.monotonic()>=route_deadline:
            raise RuntimeError('Daedalus chat did not start a workflow within ten minutes')
        page.wait_for_timeout(1500)
    raise CampaignInterrupted('Workflow deadline reached. Inspect the existing job before resuming')


def require_verified_controller(job):
    """Private behavior cannot authorize acceptance of a failed controller result."""
    summary = job.get('verification_summary') or {}
    patch = job.get('last_patch') or {}
    status = str(patch.get('execution_status', '')).rsplit('.', 1)[-1].lower()
    if (summary.get('checks_ok') is not True or not job.get('revision_id')
            or summary.get('revision_id') != job['revision_id'] or summary.get('incomplete_reason')
            or patch.get('agent_finished') is False and status in {'stuck', 'error'}
            or any(c.get('status') == 'failed' or c.get('supported') and c.get('status') != 'passed'
                   for c in summary.get('criteria', []))):
        raise RuntimeError('Controller verification failed, stale or incomplete; revision cannot qualify for acceptance or follow-up')


def archive_and_accept(page, job, turn, case, index, path, judge, persist, *, promote=True, judge_identity=None):
    if not job.get('candidate_artifact'):
        details = job.get('failure') or {}
        failure = details.get('category')
        blocker = job.get('blocker') or ''
        preparation = details.get('preparation') or []
        if isinstance(preparation, dict): preparation = [preparation]
        if not isinstance(preparation, list): preparation = []
        if job.get('state') == 'blocked' and failure == 'environment' and (
                details.get('stage') == 'source_preparation' or any(
                    isinstance(d, dict) and d.get('category') == 'preparation_limit' and d.get('retryable') is False
                    for d in preparation)):
            raise RuntimeError('Workflow exhausted bounded preparation: ' + blocker)
        if failure in {'environment','network','worker_interrupted','upstream_service'} or any(
                token in blocker for token in ('IsolationUnavailable','Mount is outside','No space left on device')):
            raise CampaignInterrupted('Candidate infrastructure: ' + blocker)
        raise RuntimeError(f"No candidate in {job['state']}: {job.get('blocker','')}")
    artifact = job['candidate_artifact']
    selector = f'a[download][href*="/api/artifacts/{artifact["id"]}/download"]'
    locator = page.locator(selector).first
    # Wait for the streamed card to refresh after the backend finishes. A
    # background strip can open a modal that obscures the actual delivery.
    dialog = page.locator('dialog.dj-dialog[open]')
    if dialog.count():
        dialog.get_by_role('button',name='Close job details').click()
    archive = path.parent / (job['id']+'.tar.gz')
    if not archive.exists():
        locator.wait_for(state='visible',timeout=45000)
        with page.expect_download(timeout=45000) as dl:
            locator.click(timeout=45000)
        temporary = archive.with_suffix('.part')
        dl.value.save_as(temporary)
        temporary.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != artifact['sha256']:
        raise RuntimeError('Downloaded bytes differ from delivered archive')
    turn.update(download_sha256=digest)
    job['composer'] = {k:turn[k] for k in ('prompt','message_id','workflow_ids','download_sha256')}
    if 'held_out' not in turn:
        request = dict(job_id=job['id'],project_id=job['project_id'],archive=str(archive.resolve()),
                       sha256=digest, case=case['id'],followup=bool(index),
                       judge_identity_sha256=(judge_identity or {}).get('sha256'))
        if isinstance(judge, dict):
            # Stream the archive to an independent Linux judge (e.g. via SSH).
            # It receives no path into the browser machine or the coding workspace.
            with subprocess.Popen(judge['argv'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as process:
                try:
                    process.stdin.write((json.dumps(request)+'\n').encode())
                    with archive.open('rb') as source:
                        shutil.copyfileobj(source,process.stdin)
                    process.stdin.close()
                    process.stdin=None
                    stdout,stderr=process.communicate(timeout=700)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                checked=subprocess.CompletedProcess(judge['argv'],process.returncode,stdout.decode(),stderr.decode())
        else:
            checked=subprocess.run(judge,input=json.dumps(request),text=True,capture_output=True,timeout=700)
        if checked.returncode:
            raise CampaignInterrupted('Private judge unavailable: '+checked.stderr[-1500:])
        turn['held_out'] = json.loads(checked.stdout)
        persist()
    job['held_out'] = turn['held_out']
    if not judge_identity or job['held_out'].get('judge_identity_sha256') != judge_identity['sha256']:
        raise CampaignInterrupted('Private verdict belongs to a different judge identity')
    if job['held_out'].get('archive_sha256') != digest:
        raise CampaignInterrupted('Private verdict belongs to a different downloaded archive')
    if job['held_out'].get('passed') is not True or not job['held_out'].get('checks') or any(
            c.get('passed') is not True for c in job['held_out']['checks']):
        raise RuntimeError('Private downloaded application check failed')
    if not promote:
        return
    require_verified_controller(job)
    versions = f"/api/coder/projects/{job['project_id']}/versions"
    if api(page,versions)['accepted']['revision_id'] != job['revision_id']:
        region = locator.locator("xpath=ancestor::*[.//button[normalize-space()='Accept this build']][1]")
        with page.expect_response(lambda r:urlsplit(r.url).path==versions.replace('/versions','/accept') and r.request.method=='POST') as accepted:
            region.get_by_role('button',name='Accept this build',exact=True).click()
        if not accepted.value.ok or accepted.value.request.post_data_json.get('revision_id')!=job['revision_id']:
            raise RuntimeError('Exact revision acceptance failed')
    job['exact_acceptance_verified'] = api(page,versions)['accepted']['revision_id']==job['revision_id']
    if dialog.count():
        dialog.get_by_role('button',name='Close job details').click()
    if not job['exact_acceptance_verified']:
        raise RuntimeError('Accepted revision differs from user-reviewed revision')


def run(args):
    env = json.loads(args.environment.read_text())
    base = env['url']
    if not env.get('isolated_candidate') or urlsplit(base).port in (None,80,443,8000,8587):
        raise ValueError('A separate verified candidate origin is required')
    root = args.output.resolve()
    root.mkdir(parents=True,exist_ok=True)
    lock = (root.parent/'chat-qualification.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    path = root/'report.json'
    cases = BANKS[args.cohort]
    reps = engine_cohorts.repeats(args.cohort)
    schedule = [(case,i) for i in range(1,reps+1) for case in cases]
    freeze = {'cohort':args.cohort,'prompt_bank_version':PROMPT_BANK_VERSION,'environment':env,'bank_sha256':hashlib.sha256(
        json.dumps(cases,sort_keys=True).encode()).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    identity = verify_judge(env, args.cohort, freeze['bank_sha256'])
    report = json.loads(path.read_text()) if path.exists() else dict(freeze=freeze,trials=[],
              entrypoint='daedalus-composer',model=env['model'],model_digest=env['model_digest'],complete=False)
    if report['freeze']!=freeze:
        old = dict(report['freeze'])
        prior_driver = old.pop('runner_sha256',None)
        same_inputs = old == {k:v for k,v in freeze.items() if k!='runner_sha256'}
        if not (args.resume_driver and args.cohort=='development' and same_inputs):
            raise ValueError('Frozen inputs changed. Start a new cohort')
        report.setdefault('driver_recoveries',[]).append({'at':time.time(),'previous':prior_driver,
                                                           'current':freeze['runner_sha256'],'reason':args.resume_driver})
        report['freeze']=freeze
    persist = lambda: save(path,report)
    persist()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width':1440,'height':1024},accept_downloads=True)
        page.set_default_timeout(30000)
        page.goto(base,wait_until='domcontentloaded')
        page.get_by_role('button',name='Sign In',exact=True).click()
        page.get_by_role('button',name='Sign In',exact=True).wait_for(state='hidden')
        page._qualification_user = api(page, '/api/users/current')['user']['id']
        def record_recovery(event):
            report.setdefault('read_only_recoveries', []).append(event)
            persist()
        page._record_recovery = record_recovery
        if args.slot and args.slot not in {f"{c['id']}-{i}" for c,i in schedule}:
            raise ValueError('Unknown frozen trial slot')
        for case,rep in schedule:
            key=f"{case['id']}-{rep}"
            if args.slot and key!=args.slot:
                continue
            trial=next((r for r in report['trials'] if r['key']==key),None)
            if trial and trial.get('finished'):
                continue
            if trial is None:
                trial=dict(key=key,case=case['id'],family=case['family'],repeat=rep,started=time.time(),turns=[],jobs=[],passed=False)
                report['trials'].append(trial)
                persist()
            try:
                cid=next((t.get('conversation_id') for t in trial['turns'] if t.get('conversation_id')),None) or trial.get('upload_setup',{}).get('conversation_id')
                if cid:
                    reopen(page,cid)
                else:
                    select_agent(page)
                if case.get('upload') and not trial.get('question',{}).get('revision'):
                    cid=prepare_upload(page,case,trial,persist,env['job_seconds'])
                for index,prompt in enumerate([case['task'],case['followup']]):
                    if index < len(trial['jobs']) and trial['jobs'][index].get('exact_acceptance_verified'):
                        require_verified_controller(trial['jobs'][index])
                        continue
                    if index==len(trial['turns']):
                        trial['turns'].append({})
                    turn=trial['turns'][index]
                    if not turn.get('submitted'):
                        submit(page,prompt,turn,persist,cid)
                    if cid and cid != turn['conversation_id']:
                        raise RuntimeError('Follow-up opened a different conversation')
                    cid=turn['conversation_id']
                    earlier={j['id'] for j in trial['jobs'][:index]}
                    if trial.get('question',{}).get('job_id'):
                        earlier.add(trial['question']['job_id'])
                    job=await_job(page,turn,earlier,persist,env['job_seconds'])
                    if index==len(trial['jobs']):
                        trial['jobs'].append(job)
                    else:
                        trial['jobs'][index]=job
                    persist()
                    verify_coding_model(job, env)
                    expected_mode='edit_project' if index or case.get('upload') else 'build_from_prompt'
                    if job['mode']!=expected_mode or index and job.get('source_revision_id')!=trial['jobs'][0].get('revision_id'):
                        raise RuntimeError('Chat selected the wrong workflow or parent revision')
                    archive_and_accept(page,job,turn,case,index,path,env['judge_command'],persist,judge_identity=identity)
                    persist()
                trial['passed']=True
            except CampaignInterrupted:
                persist()
                raise
            except RuntimeError as error:
                trial['error']=str(error)
            trial['finished']=time.time()
            persist()
            print(key,'PASS' if trial['passed'] else 'FAIL',trial.get('error',''),flush=True)
        browser.close()
    report['complete']=len(report['trials'])==len(schedule) and all(t.get('finished') for t in report['trials'])
    floor=engine_cohorts.floor(args.cohort)
    report['behavioral_gate_passed']=all(sum(t['passed'] for t in report['trials'] if t['family']==f)>=
        floor*sum(c['family']==f for c,_ in schedule) for f in {c['family'] for c in cases})
    persist()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-driver', help='Development only: record a driver correction while preserving submitted turns')
    parser.add_argument('--slot', help='Run one frozen slot, for interleaved paired comparisons')
    parser.add_argument('--environment',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cohort',choices=BANKS,required=True)
    run(parser.parse_args())
