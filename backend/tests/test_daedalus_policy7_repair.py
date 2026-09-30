"""Recorded false acceptance, mixed failures and medium-edit recovery."""
import json
from pathlib import Path
import shutil

import pytest

from coder_policy7 import make_brief, audit_checks
from coder_policy7_evidence import acceptance, decide, failure_signature
from coder_policy7_ops import edit_transition, implementation_task


def evidence(**changes):
    return {'id': 'audit-1', 'origin': 'independent', 'passed': True, 'revision_id': 'revision',
        'execution_id': 'op:1', 'outcomes': ['o1'], 'evidence_types': ['behavior'],
        'coverage_observed': True, 'execution_succeeded': True,
        'source_bindings': [{'path': 'app.py', 'sha256': 'a'*64}], **changes}


def test_recorded_false_acceptance_cannot_replace_delivered_tests_with_app_audit():
    outcomes = make_brief({'outcomes': ['README and executable tests'], 'batches': [{'task': 'Build'}]}, 'Build')['outcomes']
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    result = acceptance(outcomes, [evidence()], verdict, 'revision')
    assert not result['accepted']
    assert set(result['outcomes'][0]['missing_evidence']) == {'behavior', 'documentation', 'tests'}


def test_tests_must_be_delivered_executed_current_and_component_specific():
    outcomes = [{'id': 'o1', 'text': 'Backend tests', 'component': 'backend', 'evidence_types': ['tests']}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    check = evidence(origin='project', is_test=True, cwd='backend', test_files=[{'path': 'backend/test_app.py', 'sha256': 'b'*64}])
    assert acceptance(outcomes, [check], verdict, 'revision')['accepted']
    for delta in ({'revision_id': 'old'}, {'cwd': 'frontend'}, {'test_files': []},
                  {'coverage_observed': False}, {'origin': 'independent'}, {'execution_succeeded': False}):
        assert not acceptance(outcomes, [{**check, **delta}], verdict, 'revision')['accepted']


def test_empty_documentation_cannot_satisfy_a_documentation_check(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    repo,store=fixture(tmp_path,{'README.md':'  \n'})
    result=run_revision(store,'check',repo,{'profiles':[],'checks':[],'services':[]},project_id='p',checks=[
        {'id':'docs','kind':'file','origin':'independent','phase':'test','path':'README.md',
         'outcomes':['o1'],'evidence_types':['documentation'],'assertions':[{'kind':'exists'}]}])
    assert not result['checks'][0]['passed']


def test_mixed_faults_preserve_confirmed_application_repairs():
    job = {'checks': [evidence(passed=False, classification='audit_defect'),
        evidence(id='setup', origin='project', passed=False, classification='application_defect', log_tail='sqlite3 is not a pip dependency')],
        'repair_round': 0, 'audit_corrections': 2}
    result = decide(job, {'outcomes': []}, {})
    assert result['action'] == 'repair' and [c['id'] for c in result['feedback']] == ['setup']


def test_bad_audit_assertions_cannot_authorize_application_edits():
    job = {'checks': [evidence(passed=False, classification='unresolved')], 'repair_round': 0}
    verdict = {'diagnoses': [{'check_id': 'audit-1', 'kind': 'audit_defect'}]}
    assert decide(job, {'outcomes': []}, verdict)['action'] == 'audit'
    job['audit_corrections'] = 2
    result = decide(job, {'outcomes': []}, verdict)
    assert result['limit'] == 'audit_corrections' and 'application repair' not in result['reason']


def test_confirmed_application_exception_before_assertion_remains_repairable():
    check = evidence(passed=False, classification='missing_coverage', coverage_observed=False,
                     exit_code=1, log_tail='Application raised ValueError before returning its result')
    job = {'checks': [check], 'repair_round': 0}
    verdict = {'diagnoses': [{'check_id': check['id'], 'kind': 'application_defect'}]}
    assert decide(job, {'outcomes': []}, verdict)['action'] == 'repair'
    check.update(exit_code=0, log_tail='All tests passed!')
    assert decide(job, {'outcomes': []}, verdict)['action'] == 'audit'
    check.update(exit_code=1, source_bindings=[], service_bindings=[])
    assert decide(job, {'outcomes': []}, verdict)['action'] == 'audit'


def test_missing_tests_route_builder_while_missing_audit_routes_author():
    summary = {'outcomes': [{'id': 'o1', 'text': 'Tests', 'component': '.', 'missing_evidence': ['tests']}]}
    assert decide({'checks': []}, summary, {})['action'] == 'repair'
    summary['outcomes'][0]['missing_evidence'] = ['behavior']
    assert decide({'checks': []}, summary, {})['action'] == 'candidate'


def test_baseline_failure_is_retained_without_expanding_repair_scope():
    result = decide({'checks': [evidence(passed=False, classification='existing_failure')]}, {'outcomes': []}, {})
    assert result['action'] == 'candidate' and 'baseline' in result['reason']


def test_audit_rejects_wrong_runtime_missing_files_and_shadow_application(tmp_path):
    project = tmp_path / 'project'; project.mkdir(); (project / 'app.js').write_text('export const x=1;')
    audit = tmp_path / 'audit'; audit.mkdir()
    def metadata(command):
        (audit / 'audit.json').write_text(json.dumps({'checks': [{'command': command, 'outcomes': ['o1']}]}))
    metadata('python3 {audit}/test_app.js'); (audit / 'test_app.js').write_text('assert(true)')
    with pytest.raises(ValueError, match='language'):
        audit_checks(audit, [{'id': 'o1'}], project)
    metadata('python3 {audit}/missing.py')
    with pytest.raises(ValueError, match='missing'):
        audit_checks(audit, [{'id': 'o1'}], project)
    metadata('python3 {audit}/test_app.py'); (audit / 'test_app.py').write_text('assert True')
    (audit / 'app.js').write_text('export const x=2;')
    with pytest.raises(ValueError, match='shadow'):
        audit_checks(audit, [{'id': 'o1'}], project)
    (audit/'app.js').unlink()
    metadata('python3 -c {audit}/test_app.py')
    with pytest.raises(ValueError, match='registered test file'):
        audit_checks(audit, [{'id': 'o1'}], project)
    (audit/'audit.json').write_text(json.dumps({'checks':[{'command':'python3 {audit}/test_app.py','outcomes':['o1'],'cwd':'../outside'}]}))
    with pytest.raises(ValueError, match='working directory'):
        audit_checks(audit, [{'id': 'o1'}], project)


def test_truncated_medium_edit_splits_without_resetting_repair_round():
    job = {'task': 'Create a fullstack app', 'repair_round': 1, 'brief': {'batches': [
        {'task': 'Fix backend', 'files': ['server.js', 'routes.js']}]}}
    next_step = edit_transition(job, {'category': 'output_limit', 'changed': []})
    assert next_step['narrowed_targets'] == ['server.js', 'routes.js']
    assert 'repair_round' not in next_step
    narrowed = {**job, **next_step}
    task, targets = implementation_task(narrowed)
    assert targets == ['server.js'] and 'ONLY server.js' in task
    # A file that cannot be patched is skipped; the queue is always finished.
    skipped = edit_transition(narrowed, {'category': 'output_limit', 'changed': []})
    assert skipped['narrowed_targets'] == ['routes.js'] and 'stop_limit' not in skipped
    assert edit_transition({**narrowed, **skipped}, {'category': 'narration', 'changed': []})['stop_limit'] == 'no_progress'
    # Progress earlier in the pass means the checkpoint is checked rather than abandoned.
    assert edit_transition({**narrowed, **skipped, 'narrowed_changed': True}, {'category': 'narration', 'changed': []})['state'] == 'checking'
    partial = edit_transition(job, {'category': 'output_limit', 'changed': ['server.js']})
    assert partial['narrowed_targets'] == ['routes.js'] and partial['narrowed_changed']


def test_file_queue_skips_satisfied_file_and_retries_truncated_reply():
    """Frozen pass1-medium: an already-correct first file ended a ten-file repair queue."""
    job = {'task': 'Build app', 'repair_round': 1, 'narrowed_targets': ['api_endpoints.py', 'index.html', 'package.json'],
           'brief': {'batches': [{'task': 'Build', 'files': []}]}}
    step = edit_transition(job, {'category': 'no_op', 'changed': [], 'applied': ['api_endpoints.py']})
    assert step['narrowed_targets'] == ['index.html', 'package.json'] and 'stop_limit' not in step
    job = {**job, **step}
    # Frozen pass2-upload-medium: complete blocks of a truncated reply are progress.
    step = edit_transition(job, {'category': 'output_limit', 'changed': ['index.html']})
    assert step['narrowed_targets'] == ['index.html', 'package.json'] and step['limit_retries'] == 1
    _, targets = implementation_task({**job, **step})
    assert targets == ['index.html']
    job = {**job, **step}
    step = edit_transition(job, {'category': 'source_changed', 'changed': ['index.html']})
    assert step['narrowed_targets'] == ['package.json'] and step['limit_retries'] == 0
    final = edit_transition({**job, **step}, {'category': 'narration', 'changed': []})
    assert final['state'] == 'checking' and 'stop_limit' not in final


def test_failure_signatures_ignore_execution_paths_ports_times_and_addresses():
    def failure(path, port, seconds):
        return [{'id': 'x', 'classification': 'unresolved', 'log_tail': f'{path}/project/app.py HTTP 127.0.0.1:{port} in {seconds}s'}]
    assert failure_signature(failure('/worker/execution/op-1', 1111, 1.2)) == failure_signature(failure('/worker/execution/op-2', 2222, 9.8))


def test_operation_identity_survives_progress_accounting_and_restart():
    from coder_policy7_controller import operation_inputs
    job = {'brief': {'batches': []}, 'batch': 0, 'revision_id': 'fixed', 'repair_round': 1,
           'calls_used': 1, 'worker_operation': 'job-2', 'event_sequence': 3}
    before = operation_inputs(job)
    resumed = {**job, 'calls_used': 3, 'worker_operation': 'job-3', 'event_sequence': 9,
               'updated_at': 'later', 'operation_key': 'key', 'operation_accounted': True}
    assert operation_inputs(resumed) == before
    assert operation_inputs({**resumed, 'repair_round': 2}) != before


def test_edit_revision_and_next_stage_commit_atomically():
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    job = {'id': 'j', 'state': 'coding', 'project_id': 'p', 'revision_id': 'old',
           'stage_allocation': {'round_key': '1:0', 'verification_reserve': 30},
           'brief': {'outcomes': [], 'batches': [{'task': 'Build', 'files': ['app.py']}]}}
    writes = []
    async def save(identity, **changes):
        writes.append(changes); return {**job, **changes}
    async def operate(*args, **kwargs):
        return {'changed': ['app.py'], 'snapshot': {'revision': 'new'}, 'inventory': {}}, job
    asyncio.run(step(job, SimpleNamespace(store=SimpleNamespace(save=save), _operate=operate,
        context_policy=SimpleNamespace(runtime_settings=lambda: {'daedalus_model_calls': 120}))))
    assert len(writes) == 1
    assert writes[0]['revision_id'] == 'new' and writes[0]['state'] == 'checking'


def test_invalid_audit_does_not_gain_free_corrections_after_application_repair():
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step
    job = {'id': 'j', 'state': 'checking', 'project_id': 'p', 'revision_id': 'r', 'audit_attempts': 1,
           'audit_error': 'invalid metadata', 'audit_corrections': 2, 'repair_round': 1}
    async def save(identity, **changes):
        job.update(changes); return dict(job)
    async def record(*args): pass
    async def operate(*args, **kwargs):
        return {'checks': [], 'execution_profile': {}}, {'worker_operation': 'check'}
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate)
    asyncio.run(step(job, controller))
    assert job['state'] == 'accepting'
    assert job['audit_corrections'] == 2
    assert job['checks'][0]['classification'] == 'audit_defect'


def test_log_inspection_enforces_job_ownership_for_policy7(tmp_path):
    from coder_worker_runtime import WorkerStore, read_check_log
    store = WorkerStore(tmp_path)
    for job in ('mine', 'other'):
        store.create(job+'-1', job, 'check', {'policy_version': 7})
        folder = tmp_path/'execution'/(job+'-1'); folder.mkdir(parents=True)
        (folder/'command.log').write_text(job)
    op = store.get('mine-1')
    assert read_check_log(store, op, str(tmp_path/'execution/mine-1/command.log'))['content'] == 'mine'
    with pytest.raises(ValueError, match='belonging'):
        read_check_log(store, op, str(tmp_path/'execution/other-1/command.log'))


def test_brief_cannot_omit_requested_api_and_browser_test_interfaces():
    brief = make_brief({'outcomes': ['Tests'], 'batches': [{'task': 'Build'}]},
                       'Include executable API tests and browser regression tests')
    assert {'api', 'browser'} <= {name for row in brief['outcomes'] for name in row['test_interfaces']}


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox audit sandbox')
def test_declared_audit_targets_are_writable_but_shadow_apps_are_not(tmp_path):
    import subprocess,sys
    from coder_patch_runtime import readonly_command
    audit=tmp_path/'audit';audit.mkdir()
    target=audit/'test_behavior.py';target.touch()
    program='from pathlib import Path; Path('+repr(str(target))+').write_text("assert True\\n"); Path('+repr(str(audit/'ledger.py'))+').write_text("shadow")'
    result=subprocess.run(readonly_command([sys.executable,'-c',program],[target]),capture_output=True)
    assert result.returncode != 0 and target.read_text() == 'assert True\n'
    assert not (audit/'ledger.py').exists()


def test_invalid_audit_is_not_a_correction_starting_point(tmp_path):
    from types import SimpleNamespace
    from coder_policy7_ops import Operations
    invalid=tmp_path/'bad';invalid.mkdir();(invalid/'ledger.py').write_text('shadow')
    observed=[]
    def editor(store,op,root,task,**kwargs):
        observed.append((root/'ledger.py').exists())
        (root/'test_behavior.py').write_text('from ledger import total\nassert total([])==0\n')
        (root/'audit.json').write_text(json.dumps({'checks':[{'command':'python3 {audit}/test_behavior.py','outcomes':['o1']}]}))
        return {'changed':['test_behavior.py','audit.json']}
    project=tmp_path/'project';project.mkdir();(project/'ledger.py').write_text('def total(values): return sum(values)\n')
    job={'audit_dir':str(invalid),'audit_correction_pending':True,'task':'Fix ledger',
         'brief':{'outcomes':[{'id':'o1','text':'Sum values','evidence_types':['behavior']} ]}}
    result=Operations(None,'op',SimpleNamespace(root=project),tmp_path,job,editor=editor).author('op')
    assert observed == [False] and result['audit_checks']


@pytest.mark.skipif(not shutil.which('bwrap') or not Path('/opt/openhands-worker/aider-venv/bin/aider').exists(), reason='Codebox Aider integration')
def test_real_aider_can_edit_declared_audit_files_under_readonly_mounts(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_patch_runtime import edit
    repo,store=fixture(tmp_path,{'ledger.py':'VALUE=42\n'})
    audit=tmp_path/'audit';audit.mkdir()
    def response(*args,**kwargs):
        return ('test_behavior.py\n```python\n<<<<<<< SEARCH\n=======\n'
                'from ledger import VALUE\nassert VALUE == 42\n>>>>>>> REPLACE\n```\n'
                'audit.json\n```json\n<<<<<<< SEARCH\n=======\n'
                '{"checks":[{"command":"python3 {audit}/test_behavior.py","outcomes":["o1"]}]}\n>>>>>>> REPLACE\n```')
    result=edit(store,'check',audit,'Write independent tests.',read_root=repo.root,
                preferred=['audit.json','test_behavior.py'],chat=response)
    assert result['category']=='source_changed',result
    assert (audit/'test_behavior.py').read_text().endswith('assert VALUE == 42\n')
    assert (repo.root/'ledger.py').read_text()=='VALUE=42\n'


@pytest.mark.skipif(not shutil.which('bwrap') or not shutil.which('node'), reason='Codebox runtimes')
def test_python_http_audit_uses_managed_node_service_without_importing_javascript(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import run_revision
    repo, store = fixture(tmp_path, {'server.js': "const http=require('node:http');http.createServer((q,r)=>{r.end(JSON.stringify({value:42}))}).listen(Number(process.argv[2]),'127.0.0.1');"})
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'test_http.py').write_text('import os,json,urllib.request\nfrom playwright.sync_api import sync_playwright\nwith urllib.request.urlopen(os.environ["DAEDALUS_APP_URL"]) as r:\n assert json.load(r)["value"]==42\n')
    profile = {'profiles': [], 'checks': [], 'services': [{'id': 'app', 'cwd': '.', 'command': 'node server.js {port}'}]}
    result = run_revision(store, 'check', repo, profile, project_id='node', audit_source=audit,
        checks=[{'id': 'audit', 'origin': 'independent', 'command': '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_http.py', 'is_test': True, 'phase': 'test'}])
    check = next(c for c in result['checks'] if c['id'] == 'audit')
    assert check['passed'] and check['assertions_executed'] == 1
    assert check['service_bindings'][0]['requests'][0]['path'] == '/'
    assert check['service_bindings'][0]['revision_id'] == store.get('check')['payload']['revision_id']

@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox audit sandbox')
@pytest.mark.parametrize('partial_edit',[False,True])
def test_persistent_policy7_controller_runs_worker_operations_and_publishes_exact_revision(monkeypatch,tmp_path,partial_edit):
    import asyncio,hashlib,time
    import coder_inference,coder_jobs,config
    import coder_policy7_ops as ops
    import database as db
    from db import coder_jobs as jobs
    from coder_worker_runtime import WorkerStore,execute
    from context_policy import DEFAULTS
    from tests.test_daedalus_policy6 import setup
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(coder_jobs,'POLICY_VERSION',7)
    monkeypatch.setattr(coder_inference,'require_local_model',lambda *a,**k:{})
    worker=WorkerStore(tmp_path/'worker');order=[]
    def chat(store,op,role,messages,**kwargs):
        if role=='architect':
            return json.dumps({'project_name':'Values','outcomes':[{'text':'Return values','evidence_types':['behavior']},
                {'text':'README and executable tests','evidence_types':['documentation','tests']}],
                'batches':[{'task':'Implement values','files':['subject.py','test_subject.py','README.md']}]})
        return json.dumps({'scope_complete':True,'outcomes':[{'id':'o1','status':'passed'},{'id':'o2','status':'passed'}], 'diagnoses':[]})
    edits=[]
    def edit(store,op,root,task,**kwargs):
        if kwargs.get('read_root'):
            (root/'test_behavior.py').write_text('from subject import values\nassert values()==[1,2]\n')
            (root/'audit.json').write_text(json.dumps({'checks':[
                {'command':'python3 {audit}/test_behavior.py','outcomes':['o1'],'evidence_types':['behavior']},
                {'kind':'file','path':'README.md','outcomes':['o2'],'evidence_types':['documentation'],'assertions':[{'kind':'nonempty'}]}]}))
            return {'changed':['audit.json','test_behavior.py']}
        edits.append(op)
        if partial_edit and len(edits)>1:
            return {'changed':[], 'category':'narration'}
        (root/'subject.py').write_text('def values(): return [1,2]\n')
        (root/'test_subject.py').write_text('from subject import values\nassert values()==[1,2]\n')
        (root/'README.md').write_text('Import subject and call values. Run python3 test_subject.py.\n')
        return {'changed':['subject.py','test_subject.py','README.md'], 'category':'source_changed'}
    monkeypatch.setattr(ops,'edit',edit);monkeypatch.setattr(ops,'local_chat',chat)
    # A new project is built by the agent-loop builder behind the same gate; the fake agent run writes
    # the files and, in the partial variant, returns a checkpoint without calling finish.
    import coder_sdk_runtime
    def agent_run(store,op,repository):
        seen=store.get(op)['payload']
        assert seen['brief']['outcomes'] and 'PROJECT CONVENTIONS' in seen['builder_guidance'] and seen['milestone_id']=='build-r0'
        edit(store,op,repository.root,'agent build')
        return {'agent_finished':not partial_edit,'execution_status':'finished','session_id':'s','events':1}
    monkeypatch.setattr(coder_sdk_runtime,'run_coder',agent_run)
    async def operate(job,kind,**extra):
        identity=f"{job['id']}-{len(order)+1}";order.append(kind)
        payload={'policy_version':7,'kind':kind,'task':job['user_task'],'original_task':job['user_task'],
            'model':'saved:local','ollama_url':'http://unused','settings':{**DEFAULTS,'daedalus_min_free_mb':1},
            'seconds_remaining':60,'calls_remaining':20,'revision_id':job.get('revision_id',''),
            'baseline_revision':job.get('baseline_revision',''),'project_id':job['project_id'],**extra}
        worker.create(identity,job['id'],kind,payload);worker.update(identity,started=time.time());execute(worker,identity)
        result=worker.get(identity)
        assert result['status']=='succeeded',result['result']
        saved=await jobs.save(job['id'],worker_operation=identity)
        if result['result'].get('snapshot'):await jobs.record_revision(job['id'],result['result']['snapshot'])
        return result['result'],saved
    monkeypatch.setattr(coder_jobs,'_operate',operate)
    async def deliver(job,result,**kw):
        assert kw.get('candidate') and not job['verification_summary']['accepted']
        return await jobs.finish_candidate(job['id'],job['revision_id'],artifact_id='candidate',filename='values.tar.gz',
            url='/values.tar.gz',storage_path=result['path'],sha256=result['sha256'],kind='archive',status='candidate')
    monkeypatch.setattr(coder_jobs,'_deliver',deliver)
    async def scenario():
        explicit={'packages':{'.':{'setup':'true','build':'python3 -m py_compile subject.py','test':'python3 test_subject.py'}}}
        job=await coder_jobs.create('conversation','Return values with README and executable tests.',model='saved:local',execution_commands=explicit)
        await coder_jobs.run(job['id'])
        saved=await jobs.get(job['id'])
        assert saved['state']=='ready_for_review',saved.get('blocker')
        assert not saved['verification_summary']['accepted']
        assert saved['delivery_status']=='review_candidate'
        assert all(c['revision_id']==saved['revision_id'] for c in saved['checks'])
        assert hashlib.sha256(Path(saved['candidate_artifact']['storage_path']).read_bytes()).hexdigest()==saved['candidate_artifact']['sha256']
        # A partial checkpoint (agent not finished) earns a continuation before any check runs.
        assert order==['inspect','plan','code',*(['check','code'] if partial_edit else []),'check','verify','check','accept','package']
        assert saved['builder']=='sdk' and saved['last_patch']['builder']=='sdk' and len(edits)==(2 if partial_edit else 1)
        assert not saved.get('editor_stopped') and saved['repair_round']==0
    asyncio.run(scenario())


# --- Frozen proof 2026-09-17: six working repairs, zero internal acceptances ---

def _python_repair():
    outcomes = [{'id': 'o1', 'text': 'Total is correct', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o2', 'text': 'Delivered tests', 'evidence_types': ['tests'], 'component': '.'}]
    checks = [evidence(), evidence(id='.:test:0', origin='project', is_test=True, cwd='.', phase='test', outcomes=[],
                                   test_files=[{'path': 'tests/check_ledger.py', 'sha256': 'b'*64}])]
    return outcomes, checks


def test_reviewer_scope_notes_cannot_veto_executed_evidence():
    """All ten frozen reviews put every outcome ID in missing_outcomes, so nothing could be accepted."""
    from coder_policy7_evidence import normalize_verdict
    outcomes, checks = _python_repair()
    raw = {'scope_complete': 'true', 'missing_outcomes': ['o1', 'o2'], 'diagnoses': None,
           'outcomes': [{'id': 'o1', 'status': 'Passed', 'reason': 'ok'}, {'id': 'o2', 'status': 'passed'}, {'id': 'zz', 'status': 'passed'}]}
    verdict = normalize_verdict(raw, outcomes)
    assert 'missing_outcomes' not in verdict and verdict['scope_complete'] is True
    summary = acceptance(outcomes, checks, verdict, 'revision')
    assert not summary['accepted']
    # The safety properties are unchanged.
    assert not acceptance(outcomes, checks, {**verdict, 'scope_complete': False}, 'revision')['accepted']
    assert not acceptance(outcomes, [checks[0], {**checks[1], 'coverage_observed': False}], verdict, 'revision')['accepted']
    assert not acceptance(outcomes, [*checks, {'id': 'audit-metadata', 'passed': False, 'origin': 'independent'}], verdict, 'revision')['accepted']
    failed = {**verdict, 'outcomes': [{**verdict['outcomes'][0], 'status': 'failed'}, verdict['outcomes'][1]]}
    assert not acceptance(outcomes, checks, failed, 'revision')['accepted']


def test_review_format_errors_are_retryable_not_silent():
    from coder_policy7_evidence import normalize_verdict
    outcomes, _ = _python_repair()
    with pytest.raises(ValueError, match='every outcome ID: o2'):
        normalize_verdict({'outcomes': [{'id': 'o1', 'status': 'passed'}]}, outcomes)
    with pytest.raises(ValueError, match='status'):
        normalize_verdict({'outcomes': [{'id': 'o1', 'status': 'ok'}, {'id': 'o2', 'status': 'passed'}]}, outcomes)


def test_unrequested_lint_is_advisory_but_build_and_tests_still_block():
    outcomes, checks = _python_repair()
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}, {'id': 'o2', 'status': 'passed'}]}
    lint = {'id': '.:lint:0', 'origin': 'project', 'phase': 'lint', 'passed': False, 'revision_id': 'revision'}
    summary = acceptance(outcomes, [*checks, lint], verdict, 'revision')
    assert not summary['accepted'] and summary['advisory'] == ['.:lint:0']
    for phase in ('build', 'test', 'launch', 'setup'):
        assert not acceptance(outcomes, [*checks, {**lint, 'phase': phase}], verdict, 'revision')['accepted']
    assert not acceptance(outcomes, [*checks, {**lint, 'classification': 'existing_failure', 'phase': 'test'}], verdict, 'revision')['accepted']


def test_stalled_editor_still_gets_its_durable_repair_rounds():
    """Three frozen jobs ended with zero of two repair rounds used."""
    outcomes, checks = _python_repair()
    broken = {**checks[1], 'passed': False, 'classification': 'application_defect', 'log_tail': 'AssertionError'}
    job = {'checks': [checks[0], broken], 'editor_stopped': 'The file-specific edits changed no source', 'repair_round': 0}
    summary = acceptance(outcomes, job['checks'], {}, 'revision')
    decision = decide(job, summary, {})
    assert decision['action'] == 'repair'
    repeated = decide({**job, 'repair_round': 1, 'failure_signature': decision['signature']}, summary, {})
    assert repeated['action'] == 'candidate' and repeated['limit'] == 'no_progress'


def test_mechanical_audit_metadata_slips_cost_no_correction_round(tmp_path):
    """Frozen: zero-byte audit.json (4 jobs), evidence_types ["tests"], bare python3 with Playwright, copied ledger.py."""
    from coder_policy7 import normalize_audit
    project = tmp_path / 'project'; project.mkdir()
    (project / 'ledger.py').write_text('VALUE = 1\n'); (project / 'README.md').write_text('# Ledger\nUsage.\n')
    outcomes = [{'id': 'o1', 'text': 'Total', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o2', 'text': 'README', 'evidence_types': ['documentation'], 'component': '.'}]
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'audit.json').write_text('')
    (audit / 'ledger.py').write_text('VALUE = 1\n')
    (audit / 'test_behavior.py').write_text('import os\nfrom playwright.sync_api import sync_playwright\nassert os.environ\n')
    notes = normalize_audit(audit, outcomes, project)
    assert not (audit / 'ledger.py').exists() and notes
    rows = audit_checks(audit, outcomes, project)
    assert rows[0]['outcomes'] == ['o1'] and rows[0]['command'].startswith('"$DAEDALUS_AUDIT_PYTHON"')
    assert rows[1]['kind'] == 'file' and rows[1]['path'] == 'README.md' and rows[1]['outcomes'] == ['o2']
    (audit / 'audit.json').write_text(json.dumps({'checks': [{'command': 'python3 {audit}/test_behavior.py',
        'outcomes': ['o1', 'o9'], 'evidence_types': ['tests'], 'cwd': '.'}]}))
    normalize_audit(audit, outcomes, project)
    rows = audit_checks(audit, outcomes, project)
    assert rows[0]['evidence_types'] == ['behavior'] and rows[0]['outcomes'] == ['o1']
    assert rows[0]['command'] == '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_behavior.py'
    # Nothing is invented when the model delivered no executable audit.
    empty = tmp_path / 'empty'; empty.mkdir(); (empty / 'audit.json').write_text('')
    normalize_audit(empty, outcomes[:1], project)
    with pytest.raises(ValueError):
        audit_checks(empty, outcomes[:1], project)


def test_review_retries_format_and_challenges_stale_baseline_reasoning(tmp_path):
    """Frozen upload-python: reviewer failed a green repair by citing the pre-edit baseline failure."""
    from coder_policy7_ops import Operations
    from context_policy import DEFAULTS
    outcomes, checks = _python_repair()
    replies = ['Here is my review: not json',
        json.dumps({'scope_complete': True, 'missing_outcomes': ['o1', 'o2'], 'diagnoses': [], 'outcomes': [
            {'id': 'o1', 'status': 'failed', 'reason': 'Marked as a baseline failure'}, {'id': 'o2', 'status': 'passed', 'reason': 'ran'}]}),
        json.dumps({'scope_complete': True, 'diagnoses': [], 'outcomes': [
            {'id': 'o1', 'status': 'passed', 'reason': 'current check passes'}, {'id': 'o2', 'status': 'passed', 'reason': 'ran'}]})]
    prompts = []
    def chat(store, op, role, messages, **kwargs):
        prompts.append(messages[0]['content'])
        return replies[len(prompts) - 1]
    class Repo:
        root = tmp_path
        def git(self, *args):
            return b''
    job = {'task': 'Fix the ledger total', 'settings': dict(DEFAULTS), 'brief': {'outcomes': outcomes}, 'checks': checks,
           'revision_id': 'revision', 'baseline_revision': 'base', 'audit_dir': str(tmp_path / 'audit'),
           'baseline_checks': [{'id': '.:test:0', 'passed': False, 'command': 'python3 tests/check_ledger.py'}]}
    verdict = Operations(None, 'op', Repo(), tmp_path, job, chat=chat).review('op')
    assert len(prompts) == 2 and 'Correct this review format' in prompts[1]
    assert all('Re-examine outcomes o1' not in prompt for prompt in prompts)
    assert 'FIXED: passes in checks' in prompts[0]
    assert not acceptance(outcomes, checks, verdict, 'revision')['accepted']


def test_audit_paths_derived_from_file_are_pointed_at_the_project_root(tmp_path):
    """Smoke 2026-09-18 upload-python: the audit looked for ledger.py beside itself through two corrections."""
    from coder_policy7 import project_root_paths
    script = tmp_path / 'test_behavior.py'
    script.write_text('import pathlib, os\nroot = pathlib.Path(__file__).resolve().parents[0]\n'
                      'other = os.path.dirname(os.path.abspath(__file__))\nledger = root / "ledger.py"\nassert ledger\n')
    assert project_root_paths(script)
    text = script.read_text()
    assert '__file__' not in text and text.count('DAEDALUS_PROJECT_ROOT') == 2
    os_environ = {'DAEDALUS_PROJECT_ROOT': '/project'}
    import os
    previous = dict(os.environ); os.environ.update(os_environ)
    try:
        scope = {}
        exec(compile(text, 'audit', 'exec'), scope)
    finally:
        os.environ.clear(); os.environ.update(previous)
    assert str(scope['ledger']) == '/project/ledger.py' and scope['other'] == '/project'
    untouched = tmp_path / 'plain.py'; untouched.write_text('print(__file__)\n')
    assert not project_root_paths(untouched) and untouched.read_text() == 'print(__file__)\n'


def test_unchanged_user_test_that_now_passes_is_behavior_evidence():
    outcomes = [{'id': 'o1', 'text': 'Total is correct', 'evidence_types': ['behavior'], 'component': '.'}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    test = evidence(id='.:test:0', origin='project', is_test=True, outcomes=[], evidence_types=None, repair_demonstrated=True,
                    test_files=[{'path': 'tests/check_ledger.py', 'sha256': 'b'*64}])
    assert not acceptance(outcomes, [test], verdict, 'revision')['accepted']
    for delta in ({'repair_demonstrated': False}, {'coverage_observed': False}, {'source_bindings': []}, {'origin': 'independent'}):
        assert not acceptance(outcomes, [{**test, **delta}], verdict, 'revision')['accepted']


def test_function_api_wording_and_file_components_do_not_invent_unreachable_evidence():
    """Smoke 2026-09-18 upload-web: all checks green and every outcome reviewed passed, yet withheld."""
    from coder_policy7_evidence import outcome
    task = ('Preserve the dropTodo(items,id) API. Keep npm test passing, add executable browser regression checks and a README.')
    brief = make_brief({'outcomes': [{'text': 'dropTodo API unchanged', 'component': 'model.js'}], 'batches': [{'task': 'Fix'}]}, task)
    assert brief['outcomes'][0]['component'] == '.'
    assert not any('api' in o['test_interfaces'] for o in brief['outcomes'])
    assert outcome('Executable API tests for POST /api/tasks', 0)['test_interfaces'] == ['api']
    assert outcome({'text': 'Backend tests', 'component': 'backend'}, 0)['component'] == 'backend'


def test_smoke3_documentation_claims_kept_audits_and_reviewer_findings(tmp_path):
    """Smoke3 upload-python: a command row claimed documentation, so the README check was never registered."""
    from coder_policy7 import normalize_audit
    project = tmp_path / 'project'; project.mkdir(); (project / 'README.md').write_text('Run python3 ledger.py expenses.csv\n')
    outcomes = [{'id': 'o1', 'text': 'Total', 'evidence_types': ['behavior', 'documentation'], 'component': '.'}]
    audit = tmp_path / 'audit'; audit.mkdir(); (audit / 'test_behavior.py').write_text('assert True\n')
    (audit / 'audit.json').write_text(json.dumps({'checks': [{'command': '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_behavior.py',
        'outcomes': ['o1'], 'evidence_types': ['behavior', 'documentation'], 'cwd': '.'}]}))
    normalize_audit(audit, outcomes, project)
    rows = audit_checks(audit, outcomes, project)
    assert rows[0]['evidence_types'] == ['behavior'] and rows[1]['kind'] == 'file' and rows[1]['path'] == 'README.md'
    # A reviewer-failed outcome with complete evidence and no failing check is a bounded repair, not a dead end.
    summary = {'outcomes': [{'id': 'o1', 'status': 'failed', 'missing_evidence': [], 'reason': 'README lacks test instructions',
                             'evidence_types': ['documentation'], 'component': '.'}]}
    job = {'checks': [evidence()], 'repair_round': 0}
    decision = decide(job, summary, {})
    assert decision['action'] == 'repair' and decision['feedback'][0]['reason'] == 'README lacks test instructions'
    assert decide({**job, 'repair_round': 2}, summary, {})['limit'] == 'application_repairs'


def test_playwright_scripts_run_under_the_controller_interpreter(tmp_path):
    """Smoke3 upload-web: tests/test_browser.py ran in a fresh venv without Playwright."""
    from tests.test_daedalus_policy7 import fixture
    from coder_profiles import discover
    repo, _ = fixture(tmp_path, {'tests/test_browser.py': 'from playwright.sync_api import sync_playwright\nif __name__ == "__main__":\n    print(sync_playwright)\n',
                                 'tests/test_plain.py': 'if __name__ == "__main__":\n    print(1)\n'})
    commands = [c['command'] for c in discover(repo)['checks'] if c.get('phase') == 'test']
    assert any(c.startswith('"$DAEDALUS_AUDIT_PYTHON" ') and 'test_browser.py' in c for c in commands), commands
    assert any(c.startswith('.venv/bin/python ') and 'test_plain.py' in c for c in commands), commands


def test_stalled_audit_author_is_nudged_once_and_never_passes_as_a_docs_only_audit(tmp_path):
    """Smoke3 upload-medium: the author replied "Let me first examine..." and only a README check was registered."""
    from coder_policy7_ops import Operations
    from coder_policy7_evidence import outcome
    project = tmp_path / 'project'; project.mkdir(); (project / 'README.md').write_text('Run node server.js\n')
    (project / 'server.js').write_text('module.exports = 1\n')
    class Repo:
        root = project
    outcomes = [{'id': 'o1', 'text': 'Filter works', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o2', 'text': 'README', 'evidence_types': ['documentation'], 'component': '.'}]
    calls = []
    def editor(store, op, root, task, **kwargs):
        calls.append(task)
        if len(calls) == 1:
            return {'changed': [], 'category': 'narration'}
        (root / 'test_behavior.py').write_text('import os\nassert os.environ\n')
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    job = {'task': 'Fix filter', 'brief': {'outcomes': outcomes}, 'checks': []}
    result = Operations(None, 'op', Repo(), tmp_path, job, editor=editor).author('op')
    assert len(calls) == 2 and calls[1].startswith('Your previous reply wrote no files')
    assert not result.get('audit_error') and [c.get('kind') for c in result['audit_checks']] == [None, 'file']
    stalled = Operations(None, 'op2', Repo(), tmp_path, job, editor=lambda *a, **k: {'changed': [], 'category': 'narration'}).author('op2')
    assert 'no executable behavior test' in stalled['audit_error'] and stalled['audit_checks'] == []
    assert outcome({'text': 'API tests', 'component': 'tests', 'evidence_types': ['tests']}, 0)['component'] == '.'
    assert outcome({'text': 'API tests', 'component': 'backend/tests', 'evidence_types': ['tests']}, 0)['component'] == 'backend'


def test_followup_repairs_target_the_defect_and_root_readme_documents_subpackages(tmp_path):
    """Smoke4 follow-up: one docs gap queued nine files; the root README was "missing" for component public."""
    from coder_policy7_controller import repair_targets
    from coder_policy7 import documentation_files
    job = {'brief': {'batches': [{'task': 'x', 'files': ['server.js', 'public/app.js', 'tests/test_api.py', 'README.md']}]}}
    failing = {'id': 'tests:test:1', 'cwd': 'tests', 'test_files': [{'path': 'tests/test_browser.py'}],
               'source_bindings': [{'path': 'tests/test_browser.py'}]}
    docs = {'id': 'o8:documentation', 'outcome': {'id': 'o8'}}
    assert repair_targets(job, [failing, docs]) == ['tests/test_browser.py', 'README.md']
    assert repair_targets(job, [{'id': 'o2:review', 'outcome': {'id': 'o2'}}]) == ['server.js', 'public/app.js', 'README.md']
    assert repair_targets(job, [{'id': 'x'}]) == ['server.js', 'public/app.js', 'tests/test_api.py', 'README.md']
    project = tmp_path / 'p'; (project / 'public').mkdir(parents=True); (project / 'README.md').write_text('# App\n')
    assert documentation_files(project, 'public') == ['README.md']
    outcomes = [{'id': 'o1', 'text': 'Filter docs', 'evidence_types': ['documentation'], 'component': 'public'}]
    check = evidence(evidence_types=['documentation'], file_bindings=[{'path': 'README.md', 'sha256': 'a'*64}])
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    assert acceptance(outcomes, [check], verdict, 'revision')['accepted']
    other = {**check, 'file_bindings': [{'path': 'backend/README.md', 'sha256': 'a'*64}]}
    assert not acceptance(outcomes, [other], verdict, 'revision')['accepted']


def test_requested_controls_are_checked_by_the_controller_not_the_models_own_tests(tmp_path):
    """Smoke5 web-edit1 FALSE ACCEPTANCE: request named #save-expense; app, delivered test, audit and reviewer all used #add-expense."""
    from coder_policy7_evidence import requested_interfaces
    task = ('Add an Edit button. Clicking it loads that row; #save-expense saves its changed amount without a duplicate. '
            'Recalculate #total. Support --category and --json. See issue &#35; and color #fff.')
    wanted = requested_interfaces(task)
    assert wanted['selectors'] == ['save-expense', 'total'] and requested_interfaces('Keep #task, #add and #todos; use #1a2b3c')['selectors'] == ['task', 'add', 'todos'] and wanted['flags'] == ['--category', '--json']
    outcomes = [{'id': 'o1', 'text': 'Save edits', 'evidence_types': ['behavior'], 'component': '.'}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    missing = {'id': 'interface:#save-expense', 'passed': False, 'origin': 'controller', 'classification': 'application_defect',
               'revision_id': 'revision', 'execution_id': 'op:interface', 'reason': 'Requested interface #save-expense is absent'}
    summary = acceptance(outcomes, [evidence(), missing], verdict, 'revision')
    assert not summary['accepted']
    decision = decide({'checks': [evidence(), missing], 'repair_round': 0}, summary, verdict)
    assert decision['action'] == 'repair' and decision['feedback'][0]['id'] == 'interface:#save-expense'
    # The audit itself must touch each named control.
    from coder_policy7_ops import Operations
    project = tmp_path / 'project'; project.mkdir(); (project / 'index.html').write_text('<button id="save-expense">')
    class Repo:
        root = project
    def editor(store, op, root, task, **kwargs):
        (root / 'test_behavior.py').write_text('page = None\nassert "#add-expense"\n')
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    job = {'task': 'Clicking #save-expense saves the row.', 'brief': {'outcomes': outcomes}, 'checks': []}
    result = Operations(None, 'op', Repo(), tmp_path, job, editor=editor).author('op')
    assert 'never exercises requested interface(s): #save-expense' in result['audit_error']


def test_preflight_failure_is_handed_to_the_next_batch():
    """Smoke5 medium: requirements.txt listed sqlite3; setup stayed broken through five batches."""
    job = {'task': 'Build a task board', 'batch': 1, 'brief': {'batches': [{'task': 'Backend', 'files': ['app.py']},
        {'task': 'Frontend', 'files': ['frontend/src/App.jsx']}]},
        'last_patch': {'source_hashes': {'requirements.txt': 'hash'}},
        'preflight_failures': [{'id': '.:setup:0', 'cwd': '.', 'command': 'pip install -r requirements.txt',
                                'log_tail': 'ERROR: No matching distribution found for sqlite3'}]}
    task, targets = implementation_task(job)
    assert 'CURRENTLY FAILING' in task and 'sqlite3' in task
    assert targets[0] == 'frontend/src/App.jsx' and 'requirements.txt' in targets
    clean_task, clean_targets = implementation_task({**job, 'preflight_failures': []})
    assert 'CURRENTLY FAILING' not in clean_task and clean_targets == ['frontend/src/App.jsx']


def test_variance_run_audit_corrections_are_informed_and_outlive_a_noop_repair():
    """Variance 2026-09-18: two working Node deliveries were withheld by the audit step."""
    outcomes = [{'id': 'o1', 'text': 'Filter', 'evidence_types': ['behavior'], 'component': '.'}]
    failing = evidence(passed=False, classification='unresolved', exit_code=1, log_tail='HTTP Error 404: Not Found')
    job = {'checks': [failing], 'repair_round': 1, 'unchanged_source': True, 'audit_corrections': 0}
    verdict = {'diagnoses': [{'check_id': 'audit-1', 'kind': 'application_defect'}]}
    summary = acceptance(outcomes, job['checks'], verdict, 'revision')
    first = decide({**job, 'unchanged_source': False, 'repair_round': 0}, summary, verdict)
    assert first['action'] == 'repair'
    again = decide({**job, 'failure_signature': first['signature']}, summary, verdict)
    assert again['action'] == 'audit' and 'fix the audit itself' in again['feedback'][0]['reason']
    spent = decide({**job, 'failure_signature': first['signature'], 'audit_corrections': 2}, summary, verdict)
    assert spent['action'] == 'candidate' and spent['limit'] == 'no_progress'
    # A failing delivered project test is never reassigned to the audit author.
    project = {**failing, 'origin': 'project', 'is_test': True, 'classification': 'application_defect'}
    mixed = decide({**job, 'checks': [project]}, acceptance(outcomes, [project], {}, 'revision'), {})
    assert mixed['action'] == 'repair'
    from coder_policy7_controller import operation_inputs
    assert operation_inputs({'audit_feedback': [{'id': 'o7:behavior'}]})['audit_feedback'] == [{'id': 'o7:behavior'}]


def test_policy7_is_selected_only_for_edits_when_enabled(monkeypatch, tmp_path):
    """Builds keep the default policy; uploaded repairs/edits opt in through Settings."""
    import asyncio
    import coder_jobs, context_policy
    from context_policy import DEFAULTS
    assert DEFAULTS['daedalus_policy7_edits'] is False
    assert context_policy.validate_patch({'daedalus_policy7_edits': True}, dict(DEFAULTS))['daedalus_policy7_edits'] is True
    with pytest.raises(ValueError):
        context_policy.validate_patch({'daedalus_policy7_edits': 'yes'}, dict(DEFAULTS))
    source = Path(coder_jobs.__file__).read_text()
    assert DEFAULTS['daedalus_policy7_builds'] is False
    assert 'mode == "edit_project" and settings.get("daedalus_policy7_edits")' in source
    assert 'mode == "build_from_prompt" and settings.get("daedalus_policy7_builds")' in source
    assert 'policy_version=policy,' in source


def test_variance2_unrouted_audit_failure_and_empty_manifests(tmp_path):
    """Variance2: a 404 in the audit's own request was dropped; a zero-byte .daedalus.json broke the contract."""
    outcomes = [{'id': 'o1', 'text': 'Filter', 'evidence_types': ['behavior'], 'component': '.'}]
    failing = evidence(passed=False, classification='unresolved', exit_code=1, log_tail='HTTP Error 404: Not Found',
                       source_bindings=[], service_bindings=[])
    ok = evidence(id='audit-2', execution_id='op:2')
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}], 'diagnoses': []}
    job = {'checks': [ok, failing], 'repair_round': 0, 'audit_corrections': 0}
    decision = decide(job, acceptance(outcomes, job['checks'], verdict, 'revision'), verdict)
    assert decision['action'] == 'audit' and decision['feedback'][0]['id'] == 'audit-1'
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import contract
    repo, _ = fixture(tmp_path, {'index.html': '<p>x</p>', '.daedalus.json': '', '.daedalus-run.json': '  \n'})
    assert isinstance(contract(repo, None)['checks'], list)


def test_compiled_runners_supply_observation_and_provenance(tmp_path):
    """Policy 7 could not accept any Java or Go project: only Python/Node runs were observable."""
    from coder_project_runtime import compiled_bindings
    root = tmp_path / 'p'
    for name in ('src/main/java/app/Calc.java', 'src/test/java/app/CalcTest.java', 'target/classes/app/Calc.java', 'calc.go', 'calc_test.go'):
        (root / name).parent.mkdir(parents=True, exist_ok=True); (root / name).write_text('x')
    java = [b['path'] for b in compiled_bindings('mvn test', root, root)]
    assert java == ['src/main/java/app/Calc.java', 'src/test/java/app/CalcTest.java']
    assert [b['path'] for b in compiled_bindings('go test -v ./...', root, root)] == ['calc.go', 'calc_test.go']
    assert compiled_bindings('python3 tests/check.py', root, root) == []


def test_audit_static_faults_are_nudged_before_execution(tmp_path):
    """builds1 java-unitconv: the audit rebuilt with mvn in the read-only sandbox and used print/sys.exit, not assert."""
    from coder_policy7 import static_audit_faults
    from coder_policy7_ops import Operations
    bad = ('import os, subprocess, sys\nroot = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
           'r = subprocess.run(["mvn", "-q", "package"], cwd=root)\nif r.returncode != 0:\n    sys.exit(1)\n')
    good = ('import os, subprocess\nroot = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
            'r = subprocess.run(["java", "-cp", "target/classes", "app.UnitConv", "1", "km", "m"], cwd=root, capture_output=True, text=True)\n'
            'assert r.stdout.strip() == "1000.00", r.stdout\n')
    audit = tmp_path / 'a'; audit.mkdir(); (audit / 'test_behavior.py').write_text(bad)
    faults = static_audit_faults(audit)
    assert len(faults) == 2 and 'no assert statement' in faults[0] and 'ALREADY built' in faults[1]
    (audit / 'test_behavior.py').write_text(good)
    assert static_audit_faults(audit) == []
    project = tmp_path / 'project'; project.mkdir(); (project / 'pom.xml').write_text('<project/>')
    class Repo:
        root = project
    outcomes = [{'id': 'o1', 'text': 'Convert units', 'evidence_types': ['behavior'], 'component': '.'}]
    calls = []
    def editor(store, op, root, task, **kwargs):
        calls.append(task)
        (root / 'test_behavior.py').write_text(bad if len(calls) == 1 else good)
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    result = Operations(None, 'op', Repo(), tmp_path, {'task': 'Build a converter', 'brief': {'outcomes': outcomes}, 'checks': []}, editor=editor).author('op')
    assert len(calls) == 2 and calls[1].startswith('Fix these defects') and not result.get('audit_error')
    stuck = Operations(None, 'op2', Repo(), tmp_path, {'task': 'Build a converter', 'brief': {'outcomes': outcomes}, 'checks': []},
                       editor=lambda store, op, root, task, **kw: ((root / 'test_behavior.py').write_text(bad), {'changed': ['test_behavior.py']})[1]).author('op2')
    assert 'not executable evidence' in stuck['audit_error']


def test_builds1_cross_ecosystem_contamination_and_interface_guard_reach(tmp_path):
    """builds1: my planner wording put package.json/.daedalus-run.json into C++/C#/Go/Rust projects; the guard missed .cs sources."""
    from coder_policy7_evidence import requested_interfaces
    assert requested_interfaces('print `- [Title](#slug)` and keep #total; support --max-depth N')['selectors'] == ['total']
    from tests.test_daedalus_policy7 import fixture
    from coder_profiles import discover
    repo, _ = fixture(tmp_path, {'CMakeLists.txt': 'cmake_minimum_required(VERSION 3.16)\nproject(x C)\n', 'package.json': ''})
    profile = discover(repo)
    assert not any('npm' in c['command'] for c in profile['checks']), [c['command'] for c in profile['checks']]
    import coder_policy7_ops
    source = Path(coder_policy7_ops.__file__).read_text()
    assert "'.cs'" in source and "'.go'" in source and 'NEVER add another ecosystem' in source


def test_hybrid_builder_adapts_agent_results_and_is_only_used_for_new_projects(tmp_path):
    """builds1/2: one-shot Aider edits wrote blind; new projects now use the agent-loop builder behind the same gate."""
    from coder_policy7_builder import sdk_build, guidance, key_error_lines
    from tests.test_daedalus_policy7 import fixture
    repo, store = fixture(tmp_path, {'README.md': 'x\n'})
    payload = store.get('check')['payload']
    job = {'brief': {'outcomes': [{'id': 'o1', 'text': 'CLI works'}], 'batches': [{'task': 'Build', 'files': ['main.c']}]},
           'repair_round': 1, 'repair_feedback': [{'id': '.:build:0', 'command': 'cmake --build build',
               'log_tail': 'gmake[2]: *** Error 1\nsrc/main.c:4:5: error: unknown type name foo\n'}]}
    def runner(store, op, repository):
        seen = store.get(op)['payload']
        assert seen['brief'] == job['brief'] and seen['milestone_id'] == 'build-r1' and 'unknown type name foo' in seen['builder_guidance']
        (repository.root / 'main.c').write_text('int main(void){return 0;}\n')
        return {'agent_finished': True, 'execution_status': 'finished', 'session_id': 's', 'events': 3}
    result = sdk_build(store, 'check', repo, job, payload, runner=runner)
    assert result['changed'] == ['main.c'] and result['category'] == 'source_changed' and 'main.c' in result['source_hashes']
    assert edit_transition({**job, 'builder': 'sdk'}, result)['state'] == 'checking'  # agent_finished
    idle = sdk_build(store, 'check', repo, job, payload, runner=lambda *a: {'agent_finished': False})
    assert idle['category'] == 'narration'
    retry = edit_transition({**job, 'builder': 'sdk'}, idle)
    assert retry == {'state': 'editing', 'recovery_used': True}
    assert edit_transition({**job, 'builder': 'sdk', 'recovery_used': True}, idle)['stop_limit'] == 'no_progress'
    crashed = sdk_build(store, 'check', repo, job, payload, runner=lambda *a: (_ for _ in ()).throw(ValueError('output limit')))
    assert crashed['category'] == 'environment' and 'output limit' in crashed['log_tail']
    assert key_error_lines(job['repair_feedback']) == ['src/main.c:4:5: error: unknown type name foo']
    assert 'ctest' in guidance({'repair_round': 0}) and 'REPAIR ROUND' not in guidance({'repair_round': 0})
    import coder_policy7_controller
    source = Path(coder_policy7_controller.__file__).read_text()
    assert "builder = 'sdk' if job['mode'] == 'build_from_prompt' else 'aider'" in source


def test_repairs_target_the_file_the_failure_names_and_only_its_own_manifest():
    """builds1 node-mdtoc: SyntaxError in the test file, yet only package.json and two config files were editable."""
    from coder_policy7_controller import repair_targets
    hashes = {name: 'h' for name in ('package.json', 'mdtoc.js', 'test/mdtoc.test.js', 'requirements.txt', 'Cargo.toml',
                                     'src/lib.rs', '.daedalus-run.json', '.daedalus.json', 'README.md')}
    job = {'brief': {'batches': [{'task': 'x', 'files': ['mdtoc.js', 'test/mdtoc.test.js', 'README.md']}]},
           'last_patch': {'source_hashes': hashes}}
    node = {'id': '.:test:0', 'cwd': '.', 'command': 'npm run test', 'test_files': [], 'source_bindings': [],
            'log_tail': "/w/execution/op-17/project/test/mdtoc.test.js:1\nimport fs from 'fs'\nSyntaxError: Unexpected identifier 'fs'\n"}
    targets = repair_targets(job, [node])
    assert targets[0] == 'test/mdtoc.test.js' and 'package.json' in targets
    assert 'requirements.txt' not in targets and 'Cargo.toml' not in targets
    rust = {'id': '.:build:0', 'cwd': '.', 'command': 'cargo build --workspace', 'log_tail': 'error[E0308]: mismatched types\n  --> src/lib.rs:47:60\n'}
    assert repair_targets(job, [rust])[:2] == ['src/lib.rs', 'Cargo.toml'] and 'package.json' not in repair_targets(job, [rust])
    contract = {'id': 'execution-contract', 'log_tail': 'A long-running service must use the controller {port}'}
    assert repair_targets(job, [contract]) == ['.daedalus-run.json', '.daedalus.json']


def test_plans_drop_globs_build_output_and_duplicate_targets():
    """builds2 java-unitconv: the plan listed target/surefire-reports/*.txt and pom.xml twice."""
    brief = make_brief({'outcomes': ['Convert units'], 'batches': [
        {'task': 'Build', 'files': ['pom.xml', 'src/main/java/app/UnitConv.java', 'target/surefire-reports/*.txt']},
        {'task': 'Docs', 'files': ['pom.xml', 'README.md', 'build/out.txt', 'docs/']}]}, 'Build a converter')
    assert [b['files'] for b in brief['batches']] == [['pom.xml', 'src/main/java/app/UnitConv.java'], ['README.md']]


def test_builds3_mechanical_fixes_dotnet_run_bare_ctest_and_nested_git(tmp_path):
    """builds3: C# audit kept `dotnet run`; C++ .daedalus.json said bare `ctest`; `cargo new` left a nested .git."""
    from coder_policy7 import mechanical_audit_rewrites, static_audit_faults
    audit = tmp_path / 'a'; audit.mkdir()
    (audit / 'test_behavior.py').write_text('import os, subprocess\nr = subprocess.run(["dotnet", "run", "--project", "CsvStats", "--", "x"], '
        'cwd=os.environ["DAEDALUS_PROJECT_ROOT"], capture_output=True, text=True)\nassert r.returncode == 0\n')
    assert any('builds, tests or installs' in f for f in static_audit_faults(audit))
    assert mechanical_audit_rewrites(audit) and '"run", "--no-build", "--project"' in (audit / 'test_behavior.py').read_text()
    assert static_audit_faults(audit) == [] and mechanical_audit_rewrites(audit) == []
    from tests.test_daedalus_policy7 import fixture
    from coder_profiles import discover
    (tmp_path / 'c').mkdir()
    repo, store = fixture(tmp_path / 'c', {'CMakeLists.txt': 'cmake_minimum_required(VERSION 3.16)\nproject(x CXX)\n',
                                       '.daedalus.json': json.dumps({'packages': {'.': {'test': 'ctest'}}})})
    commands = [c['command'] for c in discover(repo)['checks'] if c.get('phase') == 'test']
    assert commands == ['ctest --test-dir build --output-on-failure'], commands
    from coder_policy7_builder import sdk_build
    def runner(store, op, repository):
        nested = repository.root / 'tool' / '.git'; nested.mkdir(parents=True); (nested / 'HEAD').write_text('ref')
        (repository.root / 'tool' / 'main.rs').write_text('fn main() {}\n')
        return {'agent_finished': True}
    job = {'brief': {'outcomes': [], 'batches': [{'task': 'x', 'files': []}]}}
    result = sdk_build(store, 'check', repo, job, store.get('check')['payload'], runner=runner)
    assert result['changed'] == ['tool/main.rs'] and not (repo.root / 'tool' / '.git').exists()
    assert repo.snapshot('after agent')['revision']


def test_agent_builder_continues_through_partial_checkpoints_before_checks():
    """builds4: the agent stopped after one or two files; checking the half-written project burned both repair rounds."""
    from coder_policy7_ops import MAX_BUILD_CONTINUATIONS
    job = {'builder': 'sdk', 'brief': {'batches': [{'task': 'x', 'files': []}]}}
    partial = {'category': 'source_changed', 'changed': ['pom.xml'], 'agent_finished': False}
    step = edit_transition(job, partial)
    assert step == {'state': 'editing', 'build_continuations': 1, 'preflight_pending': True}
    done = edit_transition({**job, **step}, {'category': 'source_changed', 'changed': ['README.md'], 'agent_finished': True})
    assert done['state'] == 'checking' and done['build_continuations'] == 0
    # A stall after earlier progress is checked, not abandoned; a stall with no progress gets one retry.
    assert edit_transition({**job, 'build_continuations': 2}, {'category': 'narration', 'changed': []})['state'] == 'checking'
    assert edit_transition(job, {'category': 'narration', 'changed': []}) == {'state': 'editing', 'recovery_used': True}
    capped = edit_transition({**job, 'build_continuations': MAX_BUILD_CONTINUATIONS}, partial)
    assert capped['state'] == 'checking'
    from coder_policy7_builder import guidance
    assert 'CONTINUE:' in guidance({'build_continuations': 1}) and 'CONTINUE:' not in guidance({})


def test_native_test_runner_is_not_hidden_by_a_wrapper_override(tmp_path):
    """builds6 c-rpncalc: .daedalus.json copied the docs example (python3 tests/check_app.py) over a registered CTest suite."""
    from tests.test_daedalus_policy7 import fixture
    from coder_profiles import discover
    repo, _ = fixture(tmp_path, {'CMakeLists.txt': 'cmake_minimum_required(VERSION 3.16)\nproject(x C)\nenable_testing()\n',
        '.daedalus.json': json.dumps({'packages': {'.': {'test': 'python3 tests/check_app.py'}}}), 'tests/check_app.py': 'print(1)\n'})
    tests = [c['command'] for c in discover(repo)['checks'] if c.get('phase') == 'test']
    assert 'ctest --test-dir build --output-on-failure' in tests and 'python3 tests/check_app.py' not in tests, tests


def test_audits_launch_the_application_with_its_own_runtime(tmp_path):
    """builds6 go/node: working apps withheld because the audit ran `python -m logsummary` and `python mdtoc.js`."""
    from coder_policy7 import mechanical_audit_rewrites, static_audit_faults
    node = tmp_path / 'node'; node.mkdir(); (node / 'package.json').write_text('{}')
    audit = tmp_path / 'a'; audit.mkdir()
    (audit / 'test_behavior.py').write_text('import os, subprocess, sys\nr = subprocess.run([sys.executable, "mdtoc.js", "x.md"], '
        'cwd=os.environ["DAEDALUS_PROJECT_ROOT"], capture_output=True, text=True)\nassert r.returncode == 0\n')
    assert any('Node program' in f for f in static_audit_faults(audit, node))
    assert mechanical_audit_rewrites(audit, node) and '["node", "mdtoc.js"' in (audit / 'test_behavior.py').read_text()
    assert static_audit_faults(audit, node) == []
    go = tmp_path / 'go'; go.mkdir(); (go / 'go.mod').write_text('module logsummary\n')
    (audit / 'test_behavior.py').write_text('import os, subprocess, sys\nr = subprocess.run([sys.executable, "-m", "logsummary", "f.log"], '
        'cwd=os.environ["DAEDALUS_PROJECT_ROOT"])\nassert r.returncode == 0\n')
    mechanical_audit_rewrites(audit, go)
    assert '["go", "run", ".", "f.log"]' in (audit / 'test_behavior.py').read_text() and static_audit_faults(audit, go) == []
    python = tmp_path / 'py'; python.mkdir(); (python / 'requirements.txt').write_text('')
    (audit / 'test_behavior.py').write_text('import subprocess, sys\nr = subprocess.run([sys.executable, "wordfreq.py"])\nassert r.returncode == 0\n')
    assert mechanical_audit_rewrites(audit, python) == [] and static_audit_faults(audit, python) == []


# Live job cw3-f7209a44 (Local Task Board): 120/120 calls, blocked before any audit or review.
def sdk_job(**extra):
    return {'builder': 'sdk', 'brief': {'batches': [{'task': 'app', 'files': ['server.js']}]}, **extra}


def test_a_stalled_or_out_of_calls_builder_is_checked_not_continued():
    for flag in ('stalled', 'allowance_spent'):
        assert edit_transition(sdk_job(build_continuations=2), {'category': flag, flag: True, 'changed': []})['state'] == 'checking'
        assert edit_transition(sdk_job(), {'category': 'source_changed', flag: True, 'changed': ['server.js']})['state'] == 'checking'
        empty = edit_transition(sdk_job(), {'category': flag, flag: True, 'changed': []})
        assert empty['state'] == 'candidate' and empty['stop_limit'] in {'no_progress', 'model_calls'}


def test_the_builder_result_names_a_stall_and_spent_calls(tmp_path):
    from coder_policy7_builder import sdk_build
    from tests.test_daedalus_policy7 import fixture
    for message, flag, category in (('Builder stalled: it kept re-reading the same files', 'stalled', 'stalled'),
                                    ('Model-call allowance exhausted; continue from the checkpoint', 'allowance_spent', 'allowance')):
        (tmp_path / flag).mkdir()
        repo, store = fixture(tmp_path / flag, {'server.js': 'module.exports = 1\n'})
        payload = store.get('check')['payload']
        def runner(*_):
            raise RuntimeError(message)
        result = sdk_build(store, 'check', repo, {'brief': {'outcomes': [], 'batches': []}},
                           {**payload, 'revision_id': 'r1', 'settings': {'daedalus_exclude_dirs': []}}, runner=runner)
        assert result[flag] and result['category'] == category and not result['changed']


def test_the_verification_reserve_keeps_calls_for_the_audit_and_review():
    from coder_jobs import verification_reserve
    assert verification_reserve({'daedalus_model_calls': 120}) == 30
    assert verification_reserve({'daedalus_model_calls': 40}) == 10


def test_an_express_app_is_started_with_npm_start_and_its_public_folder_is_not_a_second_service(tmp_path):
    from coder_project_runtime import contract
    from tests.test_daedalus_policy7 import fixture
    repo, _ = fixture(tmp_path, {
        'package.json': json.dumps({'scripts': {'start': 'node server.js', 'dev': 'nodemon server.js'}, 'dependencies': {'express': '^4'}}),
        'server.js': "require('express')().listen(process.env.PORT)\n", 'public/index.html': '<h1>Board</h1>\n'})
    profile = contract(repo, None)
    assert profile['packages'] == ['.'] and [s['cwd'] for s in profile['services']] == ['.'], profile['services']
    assert 'run start' in profile['services'][0]['command'] and 'run dev' not in profile['services'][0]['command']


def test_a_static_site_beside_python_tooling_is_still_launched(tmp_path):
    from coder_project_runtime import contract
    from tests.test_daedalus_policy7 import fixture
    repo, _ = fixture(tmp_path, {'requirements.txt': 'pytest\n', 'web/index.html': '<h1>Site</h1>\n'})
    assert [s['cwd'] for s in contract(repo, None)['services']] == ['web']


def test_an_undeclared_script_binary_is_an_application_defect_not_a_sandbox_fault():
    from coder_project_runtime import failure_class, host_fault
    assert not host_fault(127, '> kanban@1.0.0 dev\n> nodemon server.js\n\nsh: 1: nodemon: not found\n')
    assert host_fault(127, 'sh: 1: npm: not found\n') and host_fault(127, '') and not host_fault(1, 'sh: 1: npm: not found')
    row = {'id': 'launch:app', 'phase': 'launch', 'passed': False, 'origin': 'project',
           'environment_fault': host_fault(127, 'sh: 1: nodemon: not found')}
    assert failure_class(row) != 'environment'


def test_tests_the_user_never_requested_are_not_an_acceptance_gate():
    request = 'Build a Kanban board with Node, Express and SQLite. Include a README with run instructions using npm start.'
    planned = {'outcomes': [{'text': 'A fully functional Kanban board with SQLite-backed task management', 'evidence_types': ['behavior', 'documentation', 'tests'],
                             'test_interfaces': ['api']}], 'batches': [{'task': 'Build it', 'files': ['server.js']}]}
    brief = make_brief(planned, request)
    kinds = {kind for row in brief['outcomes'] for kind in row['evidence_types']}
    assert 'tests' not in kinds and 'documentation' in kinds and not any(row['test_interfaces'] for row in brief['outcomes'])
    again = make_brief({'outcomes': brief['outcomes'], 'batches': planned['batches']}, request)
    assert 'tests' not in {kind for row in again['outcomes'] for kind in row['evidence_types']}
    asked = make_brief(planned, request + ' Add automated tests.')
    assert 'tests' in {kind for row in asked['outcomes'] for kind in row['evidence_types']}


def test_an_application_writing_its_own_database_is_not_source_tampering(tmp_path):
    # Isolated rerun of the live Kanban job: a working app was vetoed because launching it touched tasks.db.
    from coder_project_runtime import run_revision
    from tests.test_daedalus_policy7 import fixture
    files = {'server.js': 'original\n', 'tasks.db': 'SQLite format 3\x00 page one', 'data/cache.bin': 'SQLite format 3\x00 old'}
    (tmp_path / 'app').mkdir(); (tmp_path / 'tamper').mkdir()
    repo, store = fixture(tmp_path / 'app', files)
    profile = {'profiles': [], 'services': [], 'checks': [
        {'id': 'run', 'command': "printf 'SQLite format 3\\000 page two' > tasks.db && printf 'SQLite format 3\\000 new' > data/cache.bin", 'phase': 'build'}]}
    assert not any(c['id'] == 'immutable-source' for c in run_revision(store, 'check', repo, profile, project_id='project')['checks'])
    profile['checks'][0]['command'] = 'echo tampered > server.js'
    repo, store = fixture(tmp_path / 'tamper', files)
    assert run_revision(store, 'check', repo, profile, project_id='project')['checks'][-1]['failure_kind'] == 'source_mutation'


def test_the_agents_trial_database_is_not_delivered(tmp_path):
    from coder_policy7_builder import sdk_build
    from tests.test_daedalus_policy7 import fixture
    repo, store = fixture(tmp_path, {'seed.db': 'SQLite format 3\x00 shipped on purpose'})
    def runner(*_):
        (repo.root / 'server.js').write_text('listen()\n')
        (repo.root / 'tasks.db').write_bytes(b'SQLite format 3\x00 rows from the agent trying the app')
        return {'agent_finished': True}
    payload = {**store.get('check')['payload'], 'revision_id': 'r1', 'settings': {'daedalus_exclude_dirs': []}}
    result = sdk_build(store, 'check', repo, {'brief': {'outcomes': [], 'batches': [{'task': 'app', 'files': ['server.js']}]}}, payload, runner=runner)
    assert result['changed'] == ['server.js'] and not (repo.root / 'tasks.db').exists() and (repo.root / 'seed.db').exists()


def test_http_audits_are_told_about_state_assumptions_and_unrequested_endpoints():
    from coder_audit_hygiene import show_failing_line, web_audit_faults
    request = 'GET /api/tasks - list\nPOST /api/tasks\nPUT /api/tasks/:id\nDELETE /api/tasks/:id'
    audit = ('import os, requests\nAPP_URL = os.environ["DAEDALUS_APP_URL"]\n'
             'tasks = requests.get(f"{APP_URL}/api/tasks").json()\nassert len(tasks) == 1\n'
             'requests.get(f"{APP_URL}/api/tasks/{task_id}")\nrequests.put(f"{APP_URL}/api/tasks/{task_id}", json={})\n')
    faults = web_audit_faults('test_behavior.py', audit, request)
    assert len(faults) == 2 and 'exact number' in faults[0] and 'GET /api/tasks/{task_id}' in faults[1] and 'PUT' not in faults[1].split('Use ONLY')[0]
    assert not web_audit_faults('test_cli.py', 'assert len(rows) == 1\n', request)
    assert not web_audit_faults('t.py', audit.replace('assert len(tasks) == 1\n', '').replace('requests.get(f"{APP_URL}/api/tasks/{task_id}")\n', ''), request)
    handler = 'try:\n    run()\nexcept Exception as e:\n    print(f"Test failed: {e}")\n'
    assert 'traceback.print_exc()' in show_failing_line(handler) and show_failing_line(show_failing_line(handler)).count('print_exc') == 1


def test_a_no_change_repair_of_audit_only_failures_goes_straight_back_to_the_checks():
    # Isolated python-wordfreq run: a correct program, a wrong audit expectation, 56 calls of "repair" that edited nothing.
    audit_only = sdk_job(repair_round=1, repair_feedback=[{'id': 'audit-1', 'origin': 'independent'}])
    assert edit_transition(audit_only, {'category': 'narration', 'changed': []})['state'] == 'checking'
    mixed = sdk_job(repair_round=1, repair_feedback=[{'id': 'audit-1', 'origin': 'independent'}, {'id': '.:test:0', 'origin': 'project'}])
    assert edit_transition(mixed, {'category': 'narration', 'changed': []}) == {'state': 'editing', 'recovery_used': True}
    from coder_policy7_builder import guidance
    assert 'change nothing and call finish' in guidance(audit_only) and 'change nothing' not in guidance(mixed)


# Code audit 2026-09-21: findings traced by reading the gate, one regression each.
def test_deleting_or_deselecting_the_failing_user_test_earns_no_repair_credit():
    from coder_policy7_evidence import repair_demonstrated
    easy, hard = {'path': 'tests/test_easy.py', 'sha256': 'a' * 64}, {'path': 'tests/test_hard.py', 'sha256': 'b' * 64}
    old = {'id': '.:test:0', 'passed': False, 'test_files': [easy, hard], 'test_count': 5}
    now = {'id': '.:test:0', 'passed': True, 'origin': 'project', 'test_files': [easy, hard], 'test_count': 5}
    assert repair_demonstrated(now, old)
    # The model removed the failing test file: the byte-identical remainder passes.
    assert not repair_demonstrated({**now, 'test_files': [easy], 'test_count': 3}, old)
    # Same files, but a config change deselects the failing case (pytest.ini addopts, -k): fewer tests ran.
    assert not repair_demonstrated({**now, 'test_count': 4}, old)
    # An edited test, an added test, a non-project row and a still-failing row never counted.
    assert not repair_demonstrated({**now, 'test_files': [easy, {**hard, 'sha256': 'c' * 64}]}, old)
    assert not repair_demonstrated({**now, 'test_files': [easy, hard, {'path': 'tests/test_new.py', 'sha256': 'd' * 64}]}, old)
    assert not repair_demonstrated({**now, 'origin': 'independent'}, old) and not repair_demonstrated({**now, 'passed': False}, old)
    assert not repair_demonstrated(now, {**old, 'passed': True}) and not repair_demonstrated(now, None)
    # A runner whose count is unknown on either side is judged on the files alone.
    assert repair_demonstrated({**now, 'test_count': None}, old) and repair_demonstrated(now, {**old, 'test_count': None})


def test_a_repaired_user_test_proves_a_single_outcome_job_only():
    # "Fix the total AND add a --json flag": the old test says nothing about the new flag.
    repaired = evidence(id='.:test:0', origin='project', is_test=True, outcomes=[], evidence_types=None, repair_demonstrated=True,
                        test_files=[{'path': 'tests/check_ledger.py', 'sha256': 'b' * 64}])
    two = [{'id': 'o1', 'text': 'Total is correct', 'evidence_types': ['behavior'], 'component': '.'},
           {'id': 'o2', 'text': 'A --json flag prints JSON', 'evidence_types': ['behavior'], 'component': '.'}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}, {'id': 'o2', 'status': 'passed'}]}
    result = acceptance(two, [repaired], verdict, 'revision')
    assert not result['accepted'] and [o['missing_evidence'] for o in result['outcomes']] == [['behavior'], ['behavior']]
    # Independent evidence per outcome still accepts the same job.
    audits = [evidence(id='audit-1', outcomes=['o1']), evidence(id='audit-2', execution_id='op:2', outcomes=['o2'])]
    assert not acceptance(two, [repaired, *audits], verdict, 'revision')['accepted']
    # A documentation outcome beside the single behavior outcome does not disturb the credit.
    from coder_policy7_evidence import needs_behavior_audit
    docs = [two[0], {'id': 'o2', 'text': 'README explains usage', 'evidence_types': ['documentation'], 'component': '.'}]
    assert not needs_behavior_audit(docs, [repaired]) and needs_behavior_audit(two, [repaired])
    assert needs_behavior_audit(docs, []) and not needs_behavior_audit([docs[1]], [])
    # Separate packages are separate claims: a backend repair does not cover the frontend outcome.
    split = [{**two[0], 'component': 'backend'}, {**two[1], 'component': 'frontend'}]
    backend = {**repaired, 'source_bindings': [{'path': 'backend/app.py', 'sha256': 'a' * 64}]}
    rows = acceptance(split, [backend], verdict, 'revision')['outcomes']
    assert [o['missing_evidence'] for o in rows] == [['behavior'], ['behavior']]


def test_docs_and_testing_wording_keeps_the_deliverable_gates():
    # "with docs and automated testing" matched neither \breadme|documentation|document\b nor \btests?\b,
    # so make_brief WAIVED the planner's correct tests/documentation kinds and the job could accept without them.
    planned = {'outcomes': [{'text': 'Converter works', 'evidence_types': ['behavior', 'tests', 'documentation']}], 'batches': [{'task': 'Build'}]}
    for request in ('Build a unit converter with docs and automated testing',
                    'Build a unit converter. It must be documented and unit-tested.',
                    'Build a unit converter, include a usage guide and good test coverage'):
        kinds = {k for o in make_brief(json.loads(json.dumps(planned)), request)['outcomes'] for k in o['evidence_types']}
        assert {'tests', 'documentation'} <= kinds, (request, kinds)
    # A request that asks for neither still waives what the planner added on its own.
    plain = make_brief(json.loads(json.dumps(planned)), 'Build a unit converter CLI')['outcomes']
    assert {k for o in plain for k in o['evidence_types']} == {'behavior'}


def test_files_the_request_says_to_leave_alone_are_protected_without_an_api_list():
    # Policy 6 grounded protected files in the request text; policy 7 only honoured an API-supplied list.
    from coder_policy7_evidence import protected_paths
    paths = ['ledger.py', 'tests/check_ledger.py', 'config/settings.yaml', 'README.md']
    assert protected_paths('Fix the rounding bug in ledger.py.\nDo not modify tests/check_ledger.py.', paths) == ['tests/check_ledger.py']
    assert protected_paths("Add a --json flag. Leave config/settings.yaml untouched and don't touch README.md", paths) == ['config/settings.yaml', 'README.md']
    assert protected_paths('settings.yaml must remain byte-for-byte unchanged', paths) == ['config/settings.yaml']
    # A constraint ABOUT a file is not a ban on editing it, and an unknown or ambiguous name protects nothing.
    assert protected_paths('Fix ledger.py but do not change the public API of ledger.py', paths) == []
    assert protected_paths('Do not modify the database schema', paths) == []
    assert protected_paths('Do not modify settings.yaml', [*paths, 'other/settings.yaml']) == []


def test_a_protected_path_missing_at_baseline_fails_its_row_not_the_whole_check(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import execute_operation
    repo, store = fixture(tmp_path, {'app.py': 'print(1)\n', 'tests/check.py': 'import app\nassert True\n'})
    operation = store.get('check')
    job = {'brief': {'outcomes': [{'id': 'o1', 'text': 'Works', 'evidence_types': ['behavior'], 'component': '.'}], 'batches': []},
           'protected_files': ['app.py', 'gone.txt'], 'baseline_revision': operation['payload']['revision_id'], 'baseline_checks': []}
    store.update('check', payload={**operation['payload'], 'original_task': 'Keep app.py', 'project_id': 'p', 'policy7_job': job})
    rows = {c['id']: c for c in execute_operation(store, 'check', repo)['checks']}
    assert rows['protected:app.py']['passed'] and not rows['protected:gone.txt']['passed']


def test_the_plan_carries_request_protected_files_and_keeps_their_preservation_gate(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import Operations
    from context_policy import DEFAULTS
    repo, store = fixture(tmp_path, {'ledger.py': 'def total(): return 1\n', 'tests/check_ledger.py': 'assert True\n'})
    revision = store.get('check')['payload']['revision_id']
    answer = json.dumps({'outcomes': [{'text': 'Total is correct', 'evidence_types': ['behavior', 'preservation']}],
                         'batches': [{'task': 'Fix', 'files': ['ledger.py']}]})
    job = {'task': 'Fix the total in ledger.py.\nDo not modify tests/check_ledger.py.', 'inherited': [], 'settings': dict(DEFAULTS),
           'baseline_revision': revision, 'revision_id': revision}
    result = Operations(store, 'check', repo, tmp_path, job, chat=lambda *a, **k: answer).plan('check')
    assert result['protected_files'] == ['tests/check_ledger.py']
    assert 'preservation' in result['brief']['outcomes'][0]['evidence_types']
    unguarded = Operations(store, 'check', repo, tmp_path, {**job, 'task': 'Fix the total in ledger.py.'}, chat=lambda *a, **k: answer).plan('check')
    assert unguarded['protected_files'] == [] and 'preservation' not in unguarded['brief']['outcomes'][0]['evidence_types']


def test_an_empty_services_list_cannot_switch_off_the_launch_and_page_checks(tmp_path):
    # {"services": []} is not None, so auto-derived services were suppressed: no launch row, no page guard.
    from coder_project_runtime import contract
    from tests.test_daedalus_policy7 import fixture
    repo, _ = fixture(tmp_path, {'index.html': '<h1>Board</h1>\n', '.daedalus-run.json': json.dumps({'dependencies': {}, 'services': []})})
    assert [s['id'] for s in contract(repo, None)['services']] == ['app']
    (tmp_path / 'cli').mkdir()
    cli, _ = fixture(tmp_path / 'cli', {'tool.py': 'print(1)\n', '.daedalus-run.json': json.dumps({'services': []})})
    assert contract(cli, None)['services'] == []


def test_a_file_check_on_source_code_is_not_documentation_evidence(tmp_path):
    # {"kind":"file","path":"app.py"} satisfied a README outcome; acceptance() never consults missing_deliverables.
    from coder_policy7 import normalize_audit
    project, audit = tmp_path / 'project', tmp_path / 'audit'
    project.mkdir(); audit.mkdir()
    (project / 'app.py').write_text('print(1)\n')
    outcomes = [{'id': 'o1', 'text': 'README explains usage', 'evidence_types': ['documentation'], 'component': '.'}]
    claim = {'checks': [{'kind': 'file', 'path': 'app.py', 'outcomes': ['o1'], 'evidence_types': ['documentation'], 'assertions': [{'kind': 'exists'}]}]}
    (audit / 'audit.json').write_text(json.dumps(claim))
    notes = normalize_audit(audit, outcomes, project)
    assert any('not documentation' in n for n in notes)
    assert not [r for r in json.loads((audit / 'audit.json').read_text()).get('checks', []) if r.get('path') == 'app.py']
    # With a real README the controller registers the genuine check; a model row that names it survives.
    (project / 'README.md').write_text('# Usage\nRun app.py\n')
    (audit / 'audit.json').write_text(json.dumps(claim))
    normalize_audit(audit, outcomes, project)
    assert [r['path'] for r in json.loads((audit / 'audit.json').read_text())['checks']] == ['README.md']
    (audit / 'audit.json').write_text(json.dumps({'checks': [{**claim['checks'][0], 'path': 'docs/guide.rst'}]}))
    (project / 'docs').mkdir(); (project / 'docs' / 'guide.rst').write_text('Guide\n')
    normalize_audit(audit, outcomes, project)
    assert [r['path'] for r in json.loads((audit / 'audit.json').read_text())['checks']] == ['docs/guide.rst']


def test_compiled_evidence_needs_the_runners_own_summary_not_any_printed_number():
    # `test: ; @echo "Total: 5"` under make, or an app printing "7 out of 10", became five executed tests.
    from coder_native_checks import compiled_count
    assert compiled_count('make test', 'Total: 5\n') is None and compiled_count('make test', 'Processed 7 out of 10 rows\n') is None
    assert compiled_count('ctest --test-dir build --output-on-failure', '100% tests passed, 0 tests failed out of 2\n\nTotal Test time (real) = 0.01 sec') == 2
    assert compiled_count('mvn test', '[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS') == 3
    assert compiled_count('cargo test --workspace', 'test result: ok. 2 passed; 0 failed;\n\ntest result: ok. 1 passed; 0 failed;') == 3
    assert compiled_count('go test -v ./...', '=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\n--- PASS: TestSub (0.00s)\nPASS\nok  \tcalc\t0.002s') == 2
    assert compiled_count('dotnet test Calc.sln --no-build', 'Passed!  - Failed:     0, Passed:     4, Skipped:     0, Total:     4, Duration: 5 ms') == 4
    assert compiled_count('go test -v ./...', 'ok  \tcalc\t0.002s [no tests to run]') == 0
    assert compiled_count('./gradlew test', 'BUILD SUCCESSFUL in 2s') is None      # Gradle is counted from its JUnit XML
    assert compiled_count('python3 run.py', 'Tests run: 9, Failures: 0, Errors: 0') is None


def test_compiled_subprocess_provenance_needs_a_real_subprocess_call(tmp_path):
    # The words "subprocess" and "DAEDALUS_PROJECT_ROOT" in a comment were enough to bind every compiled source.
    from coder_project_runtime import launches_project
    (tmp_path / 'talk.py').write_text('# would use subprocess with DAEDALUS_PROJECT_ROOT\nassert True\n')
    assert not launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('import os, subprocess\nroot = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
                                      'out = subprocess.run([root + "/build/calc", "2", "3"], capture_output=True, text=True)\nassert out.stdout.strip() == "5"\n')
    assert launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('import os\nfrom subprocess import check_output\nroot = os.getenv("DAEDALUS_PROJECT_ROOT")\nassert check_output([root + "/calc"])\n')
    assert launches_project(tmp_path)
    (tmp_path / 'talk.py').write_text('def broken(:\n')
    assert not launches_project(tmp_path)


def test_a_requested_element_id_must_appear_as_an_id_not_as_any_word():
    # The request names #total; `total = 0` in Python or "# compute the total" satisfied the guard.
    from coder_policy7_evidence import interface_files
    noise = {'app.py': 'total = 0  # compute the total\n', 'style.css': '.total { color: red }\n'}
    assert interface_files('selector', 'total', noise) == []
    for name, text in {'index.html': '<span id="total">0</span>', 'plain.html': '<span id=total>0</span>', 'App.jsx': 'return <b id={"total"}/>',
                       'app.js': "document.getElementById('total').textContent = sum", 'q.js': 'document.querySelector("#total")',
                       'site.css': '#total { font-weight: bold }', 'view.vue': '<b :id="`total`"></b>'}.items():
        assert interface_files('selector', 'total', {**noise, name: text}) == [name], name
    assert interface_files('selector', 'save-expense', {'index.html': '<button id="save-expense-btn">'}) == []
    # Flags keep their whole-token rule.
    assert interface_files('flag', '--json', {'cli.py': "parser.add_argument('--json')", 'x.py': "'--json-lines'"}) == ['cli.py']


def test_browser_evidence_is_a_browser_request_not_a_mozilla_user_agent():
    # 'Mozilla/' in User-Agent alone: requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}) manufactured it.
    from coder_project_runtime import from_browser
    chromium = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/140.0', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    assert from_browser(chromium) and from_browser({**chromium, 'Sec-Fetch-Mode': 'cors'})
    assert not from_browser({'User-Agent': 'Mozilla/5.0 (spoofed by a Python client)'})
    assert not from_browser({'User-Agent': 'python-requests/2.32', 'Sec-Fetch-Mode': 'navigate'}) and not from_browser({})


def test_real_chromium_traffic_through_the_proxy_counts_as_browser_and_a_spoofed_client_does_not(tmp_path):
    pytest.importorskip('playwright.sync_api')
    import os, urllib.request
    from playwright.sync_api import sync_playwright
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import services
    repo, store = fixture(tmp_path, {'index.html': '<h1 id="t">ok</h1><script>fetch("/index.html")</script>\n'})
    folder = tmp_path / 'run'; folder.mkdir()
    definition = [{'id': 'app', 'cwd': '.', 'command': 'python3 -m http.server {port} --bind 127.0.0.1', 'ready_path': '/'}]
    with services(store, 'check', repo.root, definition, dict(os.environ), folder) as (env, rows):
        launch = next(r for r in rows if r['id'] == 'launch:app')
        assert launch['passed'], launch
        before = len(launch['traffic'])
        request = urllib.request.Request(env['DAEDALUS_APP_URL'], headers={'User-Agent': 'Mozilla/5.0 (spoofed)'})
        urllib.request.urlopen(request, timeout=5).read()
        assert not any(r['browser'] for r in launch['traffic'][before:])
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True)
                page = browser.new_page(); page.goto(env['DAEDALUS_APP_URL'], wait_until='networkidle'); browser.close()
        except Exception as error:
            pytest.skip('Chromium is not installed here: ' + str(error)[:120])
        seen = launch['traffic'][before + 1:]
        assert seen and all(r['browser'] for r in seen), seen   # the navigation AND the page's own fetch()


def test_a_hanging_test_is_a_failed_check_the_repair_can_read_not_a_failed_operation(tmp_path):
    # execute() raised TimeoutError out of run_revision: the job blocked, and Continue hung again for the full limit.
    from tests.test_daedalus_policy7 import fixture
    from coder_project_runtime import contract, run_revision
    # No Python/Node manifest: the fixture needs no dependency setup, so the one-second limit times only the test.
    repo, store = fixture(tmp_path, {'tool.sh': 'echo 1\n', 'tests/check.sh': 'test "$(bash tool.sh)" = 1 && sleep 30\n',
                                     '.daedalus.json': json.dumps({'packages': {'.': {'test': 'bash tests/check.sh'}}})})
    operation = store.get('check')
    store.update('check', payload={**operation['payload'], 'settings': {**operation['payload']['settings'], 'daedalus_command_seconds': 1}})
    result = run_revision(store, 'check', repo, contract(repo, None), project_id='project')
    row = next(c for c in result['checks'] if c.get('phase') == 'test')
    assert not row['passed'] and row['exit_code'] == 124 and row['classification'] == 'application_defect'
    assert 'did not finish within 1' in row['reason'] and not row['environment_fault']
    decision = decide({'checks': result['checks'], 'repair_round': 0}, {'outcomes': []}, {})
    assert decision['action'] == 'repair' and decision['feedback'][0]['id'] == row['id']


def test_checks_that_rewrite_tracked_files_are_routed_to_a_repair_with_the_paths():
    # 'immutable-source' has no origin: decide() matched no branch and ended with the generic
    # "could not verify every requested outcome" candidate, no repair round, no diagnosis.
    mutation = {'id': 'immutable-source', 'passed': False, 'failure_kind': 'source_mutation', 'classification': 'source_mutation',
                'paths': ['data/todos.json'], 'revision_id': 'revision', 'execution_id': 'op:9'}
    decision = decide({'checks': [mutation], 'repair_round': 0}, {'outcomes': []}, {})
    assert decision['action'] == 'repair'
    assert 'data/todos.json' in decision['feedback'][0]['reason'] and 'temporary' in decision['feedback'][0]['reason']
    spent = decide({'checks': [mutation], 'repair_round': 2}, {'outcomes': []}, {})
    assert spent['action'] == 'candidate' and spent['limit'] == 'application_repairs'
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    outcomes = [{'id': 'o1', 'text': 'Works', 'evidence_types': ['behavior'], 'component': '.'}]
    assert not acceptance(outcomes, [evidence(), mutation], verdict, 'revision')['accepted']


# Design review of the 2026-09-21 fixes: what the first versions still got wrong.
def test_a_text_outcome_that_mentions_documents_or_testing_keeps_its_behavior_gate():
    # Regex-derived kinds REPLACED the implicit behavior gate: "Manage uploaded documents" needed only a README.
    from coder_policy7_evidence import outcome
    assert outcome('Manage uploaded documents', 0)['evidence_types'] == ['behavior', 'documentation']
    assert outcome('A load testing tool for HTTP APIs', 0)['evidence_types'] == ['behavior', 'tests']
    assert outcome('Totals are correct', 0)['evidence_types'] == ['behavior']
    # Kinds the planner or controller states explicitly are taken as written.
    assert outcome({'text': 'Deliver the requested tests', 'evidence_types': ['tests']}, 0)['evidence_types'] == ['tests']
    assert outcome({'text': 'README explains usage', 'evidence_types': ['documentation']}, 0)['evidence_types'] == ['documentation']


def test_protection_ignores_possessives_and_longer_file_names_and_is_not_inherited():
    from coder_policy7_evidence import protected_paths
    paths = ['utils.py', 'app.py', 'README.md']
    assert protected_paths("Do not change utils.py's public API", paths) == []
    assert protected_paths('Do not modify app.py.bak', paths) == []
    assert protected_paths('Do not modify `app.py`.', paths) == ['app.py']


def test_only_api_supplied_protection_is_inherited_by_a_follow_up_job():
    # "Do not modify README.md" in one request must not forbid "now update README.md" in the next.
    from coder_jobs import inherited_protection
    source = {'protected_files': ['config.yaml', 'README.md'], 'grounded_protected_files': ['README.md']}
    assert inherited_protection(source, ['LICENSE']) == ['config.yaml', 'LICENSE']
    assert inherited_protection({}, []) == [] and inherited_protection(None, ['a.txt']) == ['a.txt']


def test_a_protected_file_the_editor_touched_is_restored_from_the_baseline(tmp_path):
    from tests.test_daedalus_policy7 import fixture
    from coder_policy7_ops import restore_protected
    repo, store = fixture(tmp_path, {'app.py': 'print(1)\n', 'tests/check.py': 'assert True\n', 'notes.md': 'keep\n'})
    baseline = store.get('check')['payload']['revision_id']
    (repo.root / 'tests/check.py').write_text('assert 1 == 1  # weakened\n'); (repo.root / 'notes.md').unlink()
    (repo.root / 'app.py').write_text('print(2)\n')
    assert restore_protected(repo, baseline, ['tests/check.py', 'notes.md', 'never-existed.txt']) == ['tests/check.py', 'notes.md']
    assert (repo.root / 'tests/check.py').read_text() == 'assert True\n' and (repo.root / 'notes.md').read_text() == 'keep\n'
    assert (repo.root / 'app.py').read_text() == 'print(2)\n' and restore_protected(repo, baseline, ['tests/check.py']) == []


def test_plain_text_data_files_are_not_documentation_and_the_gate_checks_it_too():
    from coder_policy7_evidence import is_documentation
    for name in ('README.md', 'README', 'docs/guide.rst', 'docs/notes.txt', 'USAGE.txt', 'CHANGELOG', 'manual.adoc'):
        assert is_documentation(name), name
    for name in ('words.txt', 'data/expected_output.txt', 'requirements.txt', 'CMakeLists.txt', 'app.py', 'todo.txt', ''):
        assert not is_documentation(name), name
    outcomes = [{'id': 'o1', 'text': 'README explains usage', 'evidence_types': ['documentation'], 'component': '.'}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    claim = evidence(kind='file', evidence_types=['documentation'], file_bindings=[{'path': 'words.txt', 'sha256': 'a' * 64}])
    assert not acceptance(outcomes, [claim], verdict, 'revision')['accepted']
    assert acceptance(outcomes, [{**claim, 'file_bindings': [{'path': 'README.md', 'sha256': 'a' * 64}]}], verdict, 'revision')['accepted']


def test_make_is_not_a_compiled_test_runner_and_newer_dotnet_summaries_count():
    from coder_native_checks import compiled_count
    from coder_project_runtime import COMPILED_RUNNERS
    assert 'make' not in COMPILED_RUNNERS     # a Makefile can echo any summary line; EXECUTION_HELP already says so
    assert compiled_count('make test', '100% tests passed, 0 tests failed out of 3\n') is None
    assert compiled_count('dotnet test App.sln --no-build', 'Test summary: total: 3, failed: 0, succeeded: 3, skipped: 0, duration: 0.5s') == 3


def test_the_id_guard_needs_matching_quotes_and_reads_server_templates():
    from coder_policy7_evidence import interface_files
    from coder_policy7_ops import INTERFACE_SUFFIXES
    assert interface_files('selector', 'total', {'a.js': 'const label = "total amount due"'}) == []
    assert interface_files('selector', 'total', {'a.js': 'x = "total"', 'b.pug': 'span#total 0', 'c.html': '<b id=total>'}) == ['a.js', 'b.pug', 'c.html']
    assert {'.ejs', '.erb', '.hbs', '.pug', '.jinja', '.j2', '.twig', '.cshtml', '.razor', '.astro'} <= INTERFACE_SUFFIXES


def test_a_changed_command_or_newly_skipped_tests_earn_no_repair_credit():
    from coder_policy7_evidence import repair_demonstrated
    files = [{'path': 'tests/test_app.py', 'sha256': 'a' * 64}]
    old = {'passed': False, 'test_files': files, 'test_count': 4, 'command': 'npm test', 'log_tail': 'Tests: 1 failed, 3 passed, 4 total'}
    now = {'passed': True, 'origin': 'project', 'test_files': files, 'test_count': 4, 'command': 'npm test', 'log_tail': 'Tests: 4 passed, 4 total'}
    assert repair_demonstrated(now, old)
    # Jest counts skipped tests in its total, so the count rule alone misses `-t pattern` / it.skip.
    assert not repair_demonstrated({**now, 'log_tail': 'Tests: 1 skipped, 3 passed, 4 total'}, old)
    assert not repair_demonstrated({**now, 'command': 'npm test -- -t easy'}, old)


def test_an_existing_target_too_big_for_a_third_of_the_context_is_still_given_to_the_editor(tmp_path):
    # Only NON-existent targets were re-added. A 30 KB server.js was dropped, Aider got zero editable files,
    # answered with prose, and the job ended "the editor made no source changes" with a durable limit.
    from coder_patch_runtime import edit_targets
    root = tmp_path / 'p'; root.mkdir()
    (root / 'server.js').write_text('// route\n' * 3400)          # ~30 KB, ~10k estimated tokens
    (root / 'util.js').write_text('module.exports = 1\n')
    (root / 'notes.md').write_text('reference\n')
    files, reference = edit_targets(root, 'Fix the route in server.js', 28000, (), ['server.js'])
    assert files == ['server.js'] and set(reference) <= {'util.js', 'notes.md'}
    (root / 'big_reference.js').write_text('// other\n' * 2800)  # ~8k tokens: fits a third, not what is left beside the target
    files, reference = edit_targets(root, 'Fix the route in server.js', 22000, (), ['server.js'])
    assert files == ['server.js'] and 'big_reference.js' not in reference   # the target goes in; references make room
    (root / 'big_reference.js').unlink()
    # A target that cannot fit at all is named honestly and leaves the job resumable (ValueError -> blocked).
    (root / 'huge.js').write_text('x = 1;\n' * 40000)             # ~280 KB
    with pytest.raises(ValueError, match=r'huge\.js .*Daedalus context'):
        edit_targets(root, 'Fix huge.js', 28000, (), ['huge.js'])
    # In a batch, the files that fit are edited now; the oversized one is left for its own narrowed pass.
    files, _ = edit_targets(root, 'Fix both', 28000, (), ['util.js', 'huge.js'])
    assert files == ['util.js']
    # New files are still passed through for creation.
    assert edit_targets(root, 'Add api.js', 28000, (), ['api.js'])[0] == ['api.js']


def test_a_builder_made_virtualenv_cannot_wedge_the_job(tmp_path):
    # `python3 -m venv env` links env/bin/python3 -> /usr/bin/python3. Only .venv/venv are excluded, so
    # tree_hashes -> validate_links raised outside sdk_build's try, the fallback snapshot raised too (swallowed),
    # the round's work was lost and every later operation raised again.
    import os
    from coder_policy7_builder import sdk_build
    from tests.test_daedalus_policy7 import fixture
    repo, store = fixture(tmp_path, {'app.py': 'print(1)\n'})
    def runner(*_):
        (repo.root / 'main.py').write_text('print(2)\n')
        (repo.root / 'env' / 'bin').mkdir(parents=True); (repo.root / 'env' / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        os.symlink('/usr/bin/env', repo.root / 'env' / 'bin' / 'python3')
        os.symlink('/etc/hosts', repo.root / 'hosts-link')
        return {'agent_finished': True}
    payload = {**store.get('check')['payload'], 'settings': {'daedalus_exclude_dirs': ['.venv', 'venv']}}
    result = sdk_build(store, 'check', repo, {'brief': {'outcomes': [], 'batches': [{'task': 'app', 'files': ['main.py']}]}}, payload, runner=runner)
    assert result['changed'] == ['main.py'] and sorted(result['removed']) == ['env', 'hosts-link']
    assert not (repo.root / 'env').exists() and not (repo.root / 'hosts-link').is_symlink()
    assert repo.snapshot('after', parent=payload['revision_id'])['revision']


def test_a_transient_tool_probe_failure_is_not_saved_as_a_permanent_text_mode():
    # _check_tool_support returns False on an Ollama hiccup "for THIS run, don't poison the cache", but run_coder
    # wrote {"mode": "text"} under the model-digest key with no expiry: every later build ran prompt-based.
    from types import SimpleNamespace
    from coder_sdk_runtime import tool_mode_is_definitive
    url, model = 'http://ollama:11434', 'qwen3-coder:30b'
    assert tool_mode_is_definitive(SimpleNamespace(_tool_support_cache={}), url, model, native=True)
    assert tool_mode_is_definitive(SimpleNamespace(_tool_support_cache={f'{url}:{model}': False}), url, model, native=False)
    assert not tool_mode_is_definitive(SimpleNamespace(_tool_support_cache={}), url, model, native=False)      # probe raised
    assert not tool_mode_is_definitive(SimpleNamespace(), url, model, native=False)


# Code audit 2026-09-21: small fail-closed robustness items.
def test_false_is_not_an_inherit_value_for_a_context_setting():
    from context_policy import positive_int
    assert positive_int(0, 'ctx', inherit=True) == 0 and positive_int('inherit', 'ctx', inherit=True) == 0
    for bad in (False, True):
        with pytest.raises(ValueError):
            positive_int(bad, 'ctx', inherit=True)


def test_a_truncated_trace_file_costs_its_assertions_not_the_whole_check(tmp_path):
    from coder_native_checks import provenance
    (tmp_path / 'python-1.json').write_text(json.dumps({'imports': [{'path': 'app.py', 'sha256': 'a' * 64}], 'assertions': 2}))
    (tmp_path / 'python-2.json').write_text('{"imports": [{"path": "b.py"')      # child killed mid-write
    bindings, assertions = provenance(tmp_path, tmp_path)
    assert assertions == 2 and [b['path'] for b in bindings] == ['app.py']


def test_documentation_lookup_stops_at_a_symlinked_project_root(tmp_path):
    from coder_policy7 import documentation_files
    real = tmp_path / 'real'; (real / 'pkg').mkdir(parents=True)
    (real / 'README.md').write_text('# Usage\n'); (tmp_path / 'OUTSIDE.md').write_text('not this project\n')
    (tmp_path / 'link').symlink_to(real, target_is_directory=True)
    assert documentation_files(tmp_path / 'link', 'pkg') == ['README.md']


def test_audit_rows_carry_only_known_keys_into_the_check_pipeline(tmp_path):
    audit = tmp_path / 'audit'; audit.mkdir()
    (audit / 'test_behavior.py').write_text('assert True\n')
    (audit / 'audit.json').write_text(json.dumps({'checks': [{'command': 'python3 {audit}/test_behavior.py', 'outcomes': ['o1'],
        'evidence_types': ['behavior'], 'classification': 'environment', 'passed': True, 'repair_demonstrated': True,
        'file_bindings': [{'path': 'README.md'}], 'origin': 'controller', 'name': 'adds numbers'}]}))
    row = audit_checks(audit, [{'id': 'o1'}])[0]
    assert row['origin'] == 'independent' and row['name'] == 'adds numbers'
    assert not {'classification', 'passed', 'repair_demonstrated', 'file_bindings'} & set(row)


def test_a_finished_question_is_not_labelled_needs_attention():
    from coder_presentation import presentation
    assert presentation({'state': 'completed', 'answer': 'It uses SQLite.'})['phase'] == 'Answered'
    assert presentation({'state': 'completed', 'artifact': {'id': 'a'}, 'artifact_status': 'delivered'})['phase'] == 'Complete'
    assert presentation({'state': 'blocked'})['phase'] == 'Needs attention'


# Live run 2026-09-22, c-rpncalc (cw3-fa698e74a7c79be8b3df9565): the audit copied the prompt's shell quoting into a
# subprocess LIST argument, so the program received literal quote characters and exited 2 - twice, after both corrections.
def test_shell_quotes_inside_a_subprocess_list_argument_are_stripped(tmp_path):
    from coder_policy7 import mechanical_audit_rewrites
    audit = tmp_path / 'audit'; audit.mkdir()
    script = ('import os, subprocess\nroot = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
              'r = subprocess.run([root + "/build/rpncalc", \'"3 4 +"\'], capture_output=True, text=True)\n'
              'assert r.stdout.strip() == "7.00"\n'
              "r = subprocess.run([root + '/build/rpncalc', \"'7 2 /'\"], capture_output=True, text=True)\n"
              'shell = subprocess.run(root + \'/build/rpncalc "1 1 +"\', shell=True, capture_output=True, text=True)\n'
              'print("\\"quoted\\" output")\n')
    (audit / 'test_behavior.py').write_text(script)
    notes = mechanical_audit_rewrites(audit, None)
    fixed = (audit / 'test_behavior.py').read_text()
    assert any('quote' in n for n in notes)
    assert "'3 4 +'" in fixed and "'7 2 /'" in fixed and "\"'7 2 /'\"" not in fixed   # list arguments lose their inner quotes
    assert '"1 1 +"' in fixed                                   # a shell=True command string keeps its quoting
    assert 'print("\\"quoted\\" output")' in fixed              # unrelated string literals untouched
    import ast; ast.parse(fixed)


# Live run 2026-09-22, kanban-live (cw3-67ef448bf0c65aeecc02cb55): the path regex stopped at the first quote inside an
# f-string interpolation, so DELETE /api/tasks/{created_task['id']} read as an unrequested endpoint and BOTH audit
# corrections were spent on a valid audit.
def test_interpolated_ids_in_audit_urls_are_the_requested_wildcard_endpoints():
    from coder_audit_hygiene import web_audit_faults
    request = 'GET /api/tasks\nPOST /api/tasks\nPUT /api/tasks/:id\nDELETE /api/tasks/:id'
    audit = ('import os, requests\nAPP_URL = os.environ["DAEDALUS_APP_URL"]\n'
             'created = requests.post(f"{APP_URL}/api/tasks", json={"title": "probe"}).json()\n'
             'requests.put(f"{APP_URL}/api/tasks/{created[\'id\']}", json={"status": "done"})\n'
             'requests.delete(f"{APP_URL}/api/tasks/{created["id"]}")\n'
             'requests.get(f"{APP_URL}/api/tasks")\n'
             'tid = created["id"]\nrequests.put(f"{APP_URL}/api/tasks/{tid}", json={})\n')
    assert web_audit_faults('test_behavior.py', audit, request) == []
    unrequested = audit + 'requests.get(f"{APP_URL}/api/tasks/{created[\'id\']}/history")\n'
    faults = web_audit_faults('test_behavior.py', unrequested, request)
    assert len(faults) == 1 and 'GET /api/tasks/{created[\'id\']}/history' in faults[0]


# Live run 2026-09-22, node-mdtoc (cw3-4de01cc04fa7c9de6ca24a74): the audit misread its own fixture (## Conclusion at
# level 1); the reviewer agreed three times; the builder chased it, then answered "nothing to change" (no_op, finished)
# at the end of both rounds. The audit-correction branch needed an identical failure signature and sat after the repair
# cap, so a correct program ended with application_repairs and both corrections unused.
def test_a_builder_that_finds_nothing_to_change_disputes_an_audit_only_failure_even_at_the_repair_cap():
    audit = {'id': 'audit-1', 'origin': 'independent', 'passed': False, 'classification': 'unresolved', 'revision_id': 'r',
             'execution_id': 'op:1', 'log_tail': 'AssertionError: Output mismatch', 'coverage_observed': True,
             'source_bindings': [{'path': 'mdtoc.js', 'sha256': 'a' * 64}]}   # a confirmed defect, as the reviewer diagnosed it
    verdict = {'diagnoses': [{'check_id': 'audit-1', 'kind': 'application_defect', 'outcomes': ['o1']}]}
    summary = {'outcomes': [{'id': 'o1', 'status': 'failed', 'missing_evidence': [], 'reason': 'mismatch'}]}
    finished_no_op = {'category': 'no_op', 'changed': [], 'agent_finished': True}
    # Round 2 ended with "nothing to change" and a DIFFERENT signature than round 1: correct the audit, do not stop.
    job = {'checks': [audit], 'repair_round': 2, 'audit_corrections': 0, 'failure_signature': 'other', 'unchanged_source': False,
           'last_patch': finished_no_op}
    decision = decide(job, summary, verdict)
    assert decision['action'] == 'audit' and decision['feedback'][0]['id'] == 'audit-1' and 'fixture' in decision['feedback'][0]['reason']
    # Same below the cap, without waiting for the builder to repeat itself.
    assert decide({**job, 'repair_round': 1}, summary, verdict)['action'] == 'audit'
    # Both corrections spent: the durable stop.
    assert decide({**job, 'audit_corrections': 2}, summary, verdict)['limit'] == 'application_repairs'
    # A builder that DID change source and did not finish is still a repair; an application defect at the cap still ends.
    active = {**job, 'repair_round': 1, 'last_patch': {'category': 'source_changed', 'changed': ['app.js'], 'agent_finished': False}}
    assert decide(active, summary, verdict)['action'] == 'repair'
    project = {**audit, 'id': '.:test:0', 'origin': 'project', 'is_test': True, 'classification': 'application_defect', 'exit_code': 1}
    assert decide({**job, 'checks': [project]}, summary, verdict)['limit'] == 'application_repairs'


def test_sweep3_kanban_replacing_the_placeholder_test_is_not_a_regression():
    """sweep3-c Kanban p1 / q35coder / verify3 p2: the model replaced the skeleton's health test with real CRUD tests that
    failed; id-only matching called that a regression, rolled back a revision where the form and persistence worked, and parked."""
    import hashlib
    from coder_policy7_ops import repair_regressions, placeholder_test_hashes
    from coder_skeletons import describe, files_for
    spec = describe('express-sqlite', 'Build an Express + SQLite board', 'board')
    placeholder = hashlib.sha256(files_for('express-sqlite', {})['tests/api.test.js'].encode()).hexdigest()
    assert placeholder in placeholder_test_hashes({'skeleton': spec}) and placeholder_test_hashes({}) == set()
    old = {'id': '.:test:0', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 1,
           'test_files': [{'path': 'tests/api.test.js', 'sha256': placeholder}]}
    job = {'repair_checkpoint': 'before', 'repair_checks': [old], 'skeleton': spec}
    rewritten = {**old, 'passed': False, 'test_count': 6, 'test_files': [{'path': 'tests/api.test.js', 'sha256': 'b' * 64}]}
    assert repair_regressions(job, [rewritten], {'tests/api.test.js': 'b' * 64}) == []
    assert repair_regressions(job, [], {}) == []   # a dropped placeholder is progress too
    # Without a skeleton the same-suite rule still holds: a rewritten suite is a failing-test defect, not a regression.
    real = {**old, 'test_files': [{'path': 'tests/api.test.js', 'sha256': 'a' * 64}]}
    plain = {'repair_checkpoint': 'before', 'repair_checks': [real]}
    assert repair_regressions(plain, [rewritten], {'tests/api.test.js': 'b' * 64}) == []
    assert repair_regressions(plain, [{**real, 'passed': False}], {'tests/api.test.js': 'a' * 64}) == ['.:test:0']
    assert repair_regressions(plain, [{**real, 'test_count': 0}], {'tests/api.test.js': 'a' * 64}) == ['.:test:0']
    assert repair_regressions(plain, [], {'tests/api.test.js': 'a' * 64}) == ['.:test:0']   # deselected, files untouched
    assert repair_regressions(plain, [], {'tests/crud.test.js': 'c' * 64}) == []             # suite removed: a missing-tests defect
    compiled = {'id': 'cargo:test', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 14}
    assert repair_regressions({'repair_checkpoint': 'before', 'repair_checks': [compiled]}, [{**compiled, 'passed': False}], {}) == ['cargo:test']


def test_sweep3_kanban_rollback_with_rounds_left_repairs_again_instead_of_parking():
    """sweep3-c Kanban: a genuine rollback parked the job at repair_round 1 of 2 with 80 calls left."""
    import asyncio
    from types import SimpleNamespace
    from coder_policy7_controller import step, regression_entries
    from context_policy import DEFAULTS
    failed_row = {'id': '.:test:0', 'origin': 'project', 'is_test': True, 'passed': False, 'command': 'npm test', 'cwd': '.',
                  'log_tail': 'not ok 1 - can create a task', 'test_files': [{'path': 'tests/api.test.js', 'sha256': 'a' * 64}]}
    job = {'id': 'job', 'state': 'checking', 'project_id': 'p', 'revision_id': 'failed', 'worker_operation': 'check',
           'verification_version': 3, 'task': 'Build the board.', 'repair_round': 1, 'calls_used': 40,
           'stage_allocation': {'verification_reserve': 30}, 'repair_checkpoint': 'before',
           'repair_checks': [{**failed_row, 'passed': True}],
           'repair_feedback': [{'id': 'workflow:app:drag', 'origin': 'controller', 'classification': 'application_defect',
                                'command': 'drag workflow in headless Chromium', 'reason': 'no request was sent'}],
           'last_patch': {'source_hashes': {'server.js': '1', 'public/app.js': '2', 'tests/api.test.js': '3'}},
           'brief': {'outcomes': [{'id': 'o1', 'text': 'Drag persists', 'evidence_types': ['behavior']}],
                     'batches': [{'task': 'b', 'files': ['server.js', 'public/app.js']}]}}
    async def operate(current, kind, **kwargs):
        return {'restored_snapshot': {'revision': 'before'},
                'source_hashes': {'server.js': '1', 'public/app.js': '2', 'tests/api.test.js': '0'},
                'repair_regression': {'failed_revision': 'failed', 'regressed': ['.:test:0'], 'checks': [failed_row]}}, current
    async def save(identity, **changes):
        job.update(changes); return dict(job)
    async def record(*args): pass
    controller = SimpleNamespace(store=SimpleNamespace(save=save, record_check=record), _operate=operate,
                                 context_policy=SimpleNamespace(runtime_settings=lambda: dict(DEFAULTS)))
    asyncio.run(step(dict(job), controller))
    assert job['state'] == 'coding' and job['repair_round'] == 2 and job['revision_id'] == 'before' and job['checks'][0]['passed']
    first = job['repair_feedback'][0]
    assert first['id'] == '.:test:0' and first['priority'] and 'refs/daedalus/failed-repairs' in first['reason'] and 'can create a task' in first['log_tail']
    assert job['repair_feedback'][1]['id'] == 'workflow:app:drag' and job['repair_packet'] and job['repair_packet'][0]['id'] == '.:test:0'
    assert job['last_patch']['source_hashes']['tests/api.test.js'] == '0' and len(job['repair_regressions']) == 1
    # At the repair cap the rollback parks exactly as before.
    job.update(state='checking', revision_id='failed2')
    asyncio.run(step(dict(job), controller))
    assert job['state'] == 'candidate_packaging' and job['revision_id'] == 'before' and len(job['repair_regressions']) == 2
    assert not job['verification_summary']['accepted'] and job['resume_state'] == 'checking'
    removed = regression_entries({'failed_revision': 'f' * 40, 'regressed': ['documentation:.', 'deliverable:README.md'], 'checks': []})
    assert [e['id'] for e in removed] == ['documentation:.', 'deliverable:README.md'] and all(e['priority'] and e['classification'] == 'application_defect' for e in removed)
    assert 'removed the delivered documentation for .' in removed[0]['reason'] and 'file README.md' in removed[1]['reason']


def test_sweep3_rust_readme_inside_the_cargo_package_is_not_missing(tmp_path):
    """sweep3-c Rust p1: base64tool/README.md existed, the outcome said 'documentation is missing', the no-op repair parked the job."""
    from coder_policy7 import documented, documentation_files, normalize_audit
    from coder_policy7_ops import missing_repair_deliverables
    from coder_repository import Repository
    project = tmp_path / 'p'; (project / 'base64tool' / 'src').mkdir(parents=True); (project / 'scripts').mkdir()
    (project / 'base64tool' / 'README.md').write_text('# base64tool\n'); (project / 'scripts' / 'run.sh').write_text('echo\n')
    assert documentation_files(project, '.') == []
    assert documented(project, '.', ['base64tool']) == ['base64tool/README.md']
    assert documented(project, '.', ['.', 'scripts']) == [] and documented(project, 'scripts', ['base64tool']) == []
    repository = Repository(project, tmp_path / 'revisions')
    checkpoint = repository.snapshot()['revision']
    job = {'repair_checkpoint': checkpoint, 'repair_documentation': ['.'], 'execution_profile': {'profiles': [{'cwd': 'base64tool'}]}}
    assert missing_repair_deliverables(job, repository) == []
    assert missing_repair_deliverables({**job, 'execution_profile': {}}, repository) == ['documentation:.']
    audit = tmp_path / 'audit'; audit.mkdir(); (audit / 'test_behavior.py').write_text('assert True\n')
    outcomes = [{'id': 'o1', 'text': 'Decode', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o2', 'text': 'README', 'evidence_types': ['documentation'], 'component': '.'}]
    normalize_audit(audit, outcomes, project, packages=['base64tool'])
    rows = json.loads((audit / 'audit.json').read_text())['checks']
    assert any(r.get('kind') == 'file' and r['path'] == 'base64tool/README.md' for r in rows)


def test_sweep3_medium_server_log_is_runtime_state_not_source(tmp_path):
    """sweep3-web medium p1: the last patch changed only server.log, a log the agent's trial run wrote."""
    from coder_policy7_builder import sdk_build, runtime_log, logs_are_input
    from tests.test_daedalus_policy7 import fixture
    repo, store = fixture(tmp_path, {'app.py': 'x\n'})
    payload = store.get('check')['payload']
    job = {'task': 'Build a FastAPI board', 'brief': {'outcomes': [], 'batches': [{'task': 'b', 'files': ['app.py']}]}}
    def runner(store, op, repository):
        (repository.root / 'server.log').write_text('INFO: Uvicorn running\n')
        return {'agent_finished': True, 'execution_status': 'finished'}
    result = sdk_build(store, 'check', repo, job, payload, runner=runner)
    assert result['changed'] == [] and result['category'] == 'no_op' and not (repo.root / 'server.log').exists()
    assert runtime_log('server.log') and runtime_log('backend/uvicorn.log') and runtime_log('npm-debug.log')
    assert not runtime_log('tests/fixtures/server.log') and not runtime_log('sample.log') and not runtime_log('data/app.log')
    # A log summarizer's own fixture is a deliverable: nothing is removed when logs are the request's input.
    assert logs_are_input('`logsummary <logfile>` prints counts per level') and not logs_are_input('Build a board')
    logs = {**job, 'task': 'Build logsummary: `logsummary <logfile>` prints counts per level.'}
    def fixture_runner(store, op, repository):
        (repository.root / 'app.log').write_text('ERROR x\n'); return {'agent_finished': True}
    assert sdk_build(store, 'check', repo, logs, payload, runner=fixture_runner)['changed'] == ['app.log']


def test_sweep3_node_audit_syntax_error_gets_a_second_in_operation_nudge(tmp_path):
    """sweep3-c Node p2: both audit corrections were spent on one syntax error the author kept re-emitting."""
    from coder_policy7_ops import Operations, MAX_STATIC_NUDGES
    project = tmp_path / 'project'; project.mkdir(); (project / 'mdtoc.js').write_text('module.exports = 1\n')
    class Repo:
        root = project
    outcomes = [{'id': 'o1', 'text': 'TOC works', 'evidence_types': ['behavior'], 'component': '.'}]
    bad, good = 'import os\nassert (\n', 'import os\nassert os.environ\n'
    calls = []
    def editor(store, op, root, task, **kwargs):
        calls.append(task); (root / 'test_behavior.py').write_text(bad if len(calls) < 3 else good)
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    job = {'task': 'Build mdtoc', 'brief': {'outcomes': outcomes}, 'checks': []}
    result = Operations(None, 'op', Repo(), tmp_path, job, editor=editor).author('op')
    assert len(calls) == 1 + MAX_STATIC_NUDGES == 3 and all(c.startswith('Fix these defects') and 'syntax error' in c for c in calls[1:])
    assert not result.get('audit_error') and result['audit_checks']
    stuck = []
    def always_bad(store, op, root, task, **kwargs):
        stuck.append(task); (root / 'test_behavior.py').write_text(bad); return {'changed': ['test_behavior.py']}
    result = Operations(None, 'op2', Repo(), tmp_path, job, editor=always_bad).author('op2')
    assert len(stuck) == 3 and 'not executable evidence' in result['audit_error']


def test_audit_author_receives_request_clauses_and_the_input_class_instruction(tmp_path, monkeypatch):
    """sweep3 Rust/Node: no audit ever fed an invalid or missing input; the author was never asked to."""
    import coder_policy7_ops
    from coder_policy7_ops import Operations, request_clauses
    task = ('Build base64tool. `base64tool decode <base64>` prints the decoded text. Invalid Base64 input prints an error to '
            'stderr and exits with code 2; a README is included.\nAdd tests under tests/.')
    clauses = request_clauses(task)
    assert 'Invalid Base64 input prints an error to stderr and exits with code 2' in clauses and 'Add tests under tests/' in clauses
    assert len(request_clauses('. '.join(f'clause {i}' for i in range(40)))) == 24
    project = tmp_path / 'project'; project.mkdir(); (project / 'Cargo.toml').write_text('[package]\nname = "base64tool"\n')
    class Repo:
        root = project
    outcomes = [{'id': 'o1', 'text': 'Decode works', 'evidence_types': ['behavior'], 'component': '.'}]
    seen = []
    def editor(store, op, root, task, **kwargs):
        seen.append(task); (root / 'test_behavior.py').write_text('import os\nassert os.environ\n')
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    result = Operations(None, 'op', Repo(), tmp_path, {'task': task, 'brief': {'outcomes': outcomes}, 'checks': []}, editor=editor).author('op')
    assert not result.get('audit_error') and len(seen) == 1
    payload = json.loads(seen[0].split('\n', 1)[1])
    assert payload['request_clauses'] == clauses and 'names an input class' in seen[0] and 'stated exit code, stderr and stdout' in seen[0]
    # A browser audit that ignores the labels the request names gets ONE in-operation nudge, never a correction round.
    monkeypatch.setattr(coder_policy7_ops, 'active_interfaces', lambda job: {'selectors': [], 'flags': [], 'labels': ['Project name', 'Task title']})
    browser = ('import os\nfrom playwright.sync_api import sync_playwright\nurl = os.environ["DAEDALUS_APP_URL"]\n'
               'assert url\n')
    labelled = browser.replace('assert url', 'assert url and "Project name"')
    nudged = []
    def browser_editor(store, op, root, task, **kwargs):
        nudged.append(task); (root / 'test_behavior.py').write_text(browser if len(nudged) == 1 else labelled)
        return {'changed': ['test_behavior.py'], 'category': 'source_changed'}
    result = Operations(None, 'op2', Repo(), tmp_path, {'task': 'Use accessible labels Project name, Task title.', 'brief': {'outcomes': outcomes}, 'checks': []},
                        editor=browser_editor).author('op2')
    assert len(nudged) == 2 and 'get_by_label' in nudged[1] and 'Project name' in nudged[1] and not result.get('audit_error')


def test_fix4_node_audit_writing_into_the_read_only_project_is_a_static_fault(tmp_path):
    # fix4-c pass1-node-mdtoc (2026-09-26): the audit created its fixture with open(<project>/test_input.md, "w"),
    # hit the read-only mount and spent BOTH audit corrections on it. A static fault is a free in-operation nudge.
    from coder_policy7 import static_audit_faults
    from coder_policy7_ops import mechanical_fault
    audit = tmp_path / 'audit'; audit.mkdir()
    script = audit / 'test_behavior.py'
    script.write_text('import os, subprocess\n'
                      'root = os.environ["DAEDALUS_PROJECT_ROOT"]\n'
                      'with open(os.path.join(root, "test_input.md"), "w") as f:\n    f.write("# x")\n'
                      'open("relative.md", "w").write("# y")\n'
                      'assert subprocess.run(["node", "mdtoc.js", "test_input.md"], cwd=root).returncode == 0\n')
    faults = static_audit_faults(audit, tmp_path, '')
    writes = [f for f in faults if 'writes a file into the project' in f]
    assert len(writes) == 2 and 'test_input.md' in writes[0] and 'relative.md' in writes[1] and 'DAEDALUS_AUDIT_DIR' in writes[0]
    assert all(mechanical_fault(f) for f in writes)
    script.write_text('import os, subprocess, tempfile\nfrom pathlib import Path\n'
                      'fixtures = os.environ["DAEDALUS_AUDIT_DIR"]\n'
                      'with open(os.path.join(fixtures, "test_input.md"), "w") as f:\n    f.write("# x")\n'
                      '(Path(tempfile.mkdtemp()) / "b.md").write_text("# y")\n'
                      'open("/tmp/c.md", "w").write("z")\n'
                      'assert subprocess.run(["node", "mdtoc.js", f.name], cwd=os.environ["DAEDALUS_PROJECT_ROOT"]).returncode == 0\n')
    assert not [f for f in static_audit_faults(audit, tmp_path, '') if 'writes a file into the project' in f]
