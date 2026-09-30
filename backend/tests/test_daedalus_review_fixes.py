"""Four review findings: regressions through execution, scope and lifecycle boundaries."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

from coder_policy7 import audit_checks, make_brief
from coder_policy7_evidence import acceptance, blocking, decide
from coder_scope import ScopeClarification
from tests.test_daedalus_policy7 import fixture


def test_advisory_failure_does_not_skip_real_tests(tmp_path):
    from coder_project_runtime import run_revision
    repo, store = fixture(tmp_path, {'app.py': 'VALUE=1\n', 'test_app.py': 'from app import VALUE\nassert VALUE == 1\n'})
    checks = [{'id': 'lint', 'origin': 'project', 'phase': 'lint', 'cwd': '.', 'command': 'false'},
              {'id': 'types', 'origin': 'project', 'phase': 'typecheck', 'cwd': '.', 'command': 'false'},
              {'id': 'tests', 'origin': 'project', 'phase': 'test', 'cwd': '.', 'is_test': True, 'command': 'python3 test_app.py'}]
    result = run_revision(store, 'check', repo, {'profiles': [], 'checks': checks, 'services': []}, project_id='advisory')
    assert [r['id'] for r in result['checks']] == ['lint', 'types', 'tests']
    assert result['checks'][-1]['passed'] and result['checks'][-1]['execution_succeeded']
    assert not any(blocking(c) for c in result['checks'])
    job = {'checks': result['checks'][:2], 'execution_profile': {'checks': [checks[-1]]}}
    summary = {'outcomes': [{'id': 'o1', 'component': '.', 'missing_evidence': ['tests']}]}
    assert decide(job, summary, {})['action'] == 'execute'


def test_build_failure_still_prevents_tests(tmp_path):
    from coder_project_runtime import run_revision
    repo, store = fixture(tmp_path, {'app.py': 'VALUE=1\n'})
    checks = [{'id': 'build', 'origin': 'project', 'phase': 'build', 'command': 'false'},
              {'id': 'test', 'origin': 'project', 'phase': 'test', 'is_test': True, 'command': 'true'}]
    result = run_revision(store, 'check', repo, {'profiles': [], 'checks': checks, 'services': []}, project_id='blocked')
    assert [r['id'] for r in result['checks']] == ['build']
    assert blocking(result['checks'][0])


def _scope(action='replace', quote='Replace CSV with JSON'):
    return {'outcomes': ['Store records in JSON'], 'batches': [{'task': 'Change storage', 'files': ['app.py']}],
            'inheritance': [{'parent_id': 'o1', 'action': action, 'request_quote': quote, 'replacement_outcomes': [1],
                             'question': 'Which storage format should replace CSV?'},
                            {'parent_id': 'o2', 'action': 'retain'}]}


PARENTS = [{'id': 'o1', 'text': 'Store records in CSV', 'evidence_types': ['behavior']},
           {'id': 'o2', 'text': 'Reject negative amounts', 'evidence_types': ['behavior']}]


def test_followup_replaces_conflict_preserves_unrelated_and_lineage():
    brief = make_brief(_scope(), 'Replace CSV with JSON', PARENTS)
    assert [o['text'] for o in brief['outcomes']] == ['Reject negative amounts', 'Store records in JSON']
    history = brief['requirement_history']
    assert history[0]['parent_outcome']['text'] == 'Store records in CSV'
    assert history[0]['active_outcome_ids'] == ['o2']
    assert history[1]['active_outcome_ids'] == ['o1']
    assert PARENTS[0]['text'] == 'Store records in CSV'


def test_scope_rejects_missing_unknown_and_unquoted_dispositions():
    for value in ({**_scope(), 'inheritance': []}, {**_scope(), 'inheritance': [{'parent_id': 'not-real', 'action': 'remove'}]},
                  _scope(quote='The model wishes to replace CSV')):
        with pytest.raises(ValueError):
            make_brief(value, 'Replace CSV with JSON', PARENTS)
    with pytest.raises(ScopeClarification, match='Which storage'):
        make_brief(_scope(action='clarify'), 'Change the storage', PARENTS)


def test_old_compiled_audit_evidence_cannot_accept():
    from tests.test_daedalus_policy7_repair import evidence
    checks = [evidence(provenance_kind='compiled_subprocess')]
    outcomes = [{'id': 'o1', 'text': 'Works', 'evidence_types': ['behavior']}]
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    assert not acceptance(outcomes, checks, verdict, 'revision')['accepted']
    checks[0]['process_evidence'] = {'version': 1, 'revision_id': 'stale', 'processes': [{'pid': '1'}]}
    assert not acceptance(outcomes, checks, verdict, 'revision')['accepted']


@pytest.mark.skipif(not all(shutil.which(t) for t in ('strace', 'bwrap', 'cc')), reason='Linux process tracing and C compiler')
@pytest.mark.parametrize('launch', [False, True])
def test_compiled_audit_requires_actual_launch(tmp_path, launch):
    from coder_project_runtime import run_revision
    repo, store = fixture(tmp_path, {'app.c': 'int main(void) { return 7; }\n'})
    directory = tmp_path / 'audit-source'; directory.mkdir()
    script = ('import os, subprocess\nfrom pathlib import Path\ndef application():\n'
              '    result = subprocess.run([str(Path(os.environ["DAEDALUS_PROJECT_ROOT"])/"app")])\n'
              '    assert result.returncode == 7\n' + ('application()\n' if launch else 'assert 2 + 2 == 4\n'))
    (directory / 'test_behavior.py').write_text(script)
    (directory / 'audit.json').write_text(json.dumps({'checks': [{'command': '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_behavior.py', 'outcomes': ['o1']}]}))
    outcomes = [{'id': 'o1', 'text': 'The application exits with seven', 'evidence_types': ['behavior']}]
    checks = audit_checks(directory, outcomes, repo.root)
    profile = {'profiles': [], 'services': [], 'checks': [{'id': 'build', 'origin': 'project', 'phase': 'build', 'command': 'cc app.c -o app'}]}
    result = run_revision(store, 'check', repo, profile, project_id='compiled', audit_source=directory, checks=checks)
    row = next(c for c in result['checks'] if c['id'] == 'audit-1')
    assert row['passed'] is launch, row
    if launch:
        assert row['provenance_kind'] == 'compiled_subprocess' and row['process_evidence']['processes']
    else:
        assert row['classification'] == 'missing_provenance', row
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed'}]}
    assert not acceptance(outcomes, result['checks'], verdict, result['revision_id'])['accepted']  # generated evidence is diagnostic


def test_scope_parks_before_code_and_clarification_survives_resume(monkeypatch, tmp_path):
    import config
    import database as db
    import coder_jobs as controller
    from db import coder_jobs as store
    from context_policy import DEFAULTS
    from coder_policy7_controller import step, operation_inputs
    from coder_scope import effective_task
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    async def operate(job, kind, **extra):
        assert kind == 'plan'
        return {'scope_question': 'Which format?'}, job
    monkeypatch.setattr(controller, '_operate', operate)
    async def run():
        await db.init_db(); await db.create_conversation('conv', model='local')
        job = await controller.create('conv', 'Change storage', model='local')
        job = await store.save(job['id'], state='planning', repair_round=1, audit_corrections=1)
        assert await step(job, controller)
        parked = await store.get(job['id'])
        assert parked['state'] == 'waiting_for_input'
        with pytest.raises(ValueError, match='Answer the scope'):
            await controller.resume(job['id'])
        resumed = await controller.resume(job['id'], clarification='Use JSON instead of CSV')
        assert resumed['resume_after_inspect'] == 'planning'
        assert resumed['repair_round'] == 1 and resumed['audit_corrections'] == 1
        assert 'Use JSON' in effective_task(resumed)
        assert operation_inputs(resumed)['scope_clarifications']
    asyncio.run(run())


def test_policy7_visual_routes_enabled_ui_and_preserves_measurement_failures(monkeypatch, tmp_path):
    from tests.test_daedalus_policy7_lifecycle import _drive, BRIEF
    async def operate(job, kind, **extra):
        assert kind == 'visual' and job['ui_required']
        return {'status': 'failed', 'measurements': [{'id': 'visual:app', 'passed': False, 'origin': 'controller',
                'phase': 'visual', 'classification': 'application_defect', 'revision_id': 'r',
                'reason': 'Horizontal overflow'}]}, job
    job = _drive(monkeypatch, tmp_path, {'state': 'visual_review', 'brief': BRIEF, 'revision_id': 'r',
        'ui_required': True, 'visual_policy': {'enabled': True}, 'checks': []}, operate)
    assert job['state'] == 'accepting' and job['checks'][0]['id'] == 'visual:app'
    assert decide(job, {'outcomes': []}, {})['action'] == 'repair'


def test_required_visual_skip_parks_but_optional_skip_is_advisory(monkeypatch, tmp_path):
    from tests.test_daedalus_policy7_lifecycle import _drive, BRIEF
    async def operate(job, kind, **extra):
        return {'status': 'skipped', 'reason': 'No vision model'}, job
    job = _drive(monkeypatch, tmp_path, {'state': 'visual_review', 'brief': BRIEF, 'revision_id': 'r',
        'ui_required': True, 'visual_required': True, 'checks': []}, operate)
    assert job['state'] == 'waiting_for_input' and job['resume_state'] == 'visual_review'
    row = {'origin': 'controller', 'phase': 'visual', 'passed': False, 'skipped': True, 'optional_visual': True}
    assert not blocking(row)
    assert blocking({**row, 'optional_visual': False})


def test_process_observer_ignores_failed_exec_and_unrelated_runtime(tmp_path):
    from coder_process_evidence import artifacts, observe
    (tmp_path / 'app').write_bytes(b'\x7fELF' + b'fixture')
    (tmp_path / 'App.class').write_bytes(b'\xca\xfe\xba\xbe' + b'fixture')
    known = artifacts(tmp_path, ['.'])
    trace = tmp_path / 'trace'
    trace.write_text('10 execve("/usr/bin/python3", ["python3"], []) = 0\n'
                     f'10 openat(AT_FDCWD, "{tmp_path}/App.class", O_RDONLY) = 3<{tmp_path}/App.class>\n'
                     f'10 execve("{tmp_path}/app", ["app"], []) = -1 ENOENT\n10 +++ exited with 0 +++\n')
    assert observe(trace, tmp_path, tmp_path, known, 'r')['processes'] == []
    trace.write_text(f'10 execve("/usr/bin/java", ["java", "App"], []) = 0\n'
                     f'10 openat(AT_FDCWD, "{tmp_path}/App.class", O_RDONLY) = 3<{tmp_path}/App.class>\n10 +++ exited with 7 +++\n')
    result = observe(trace, tmp_path, tmp_path, known, 'r')
    assert result['processes'][0]['artifacts'][0]['path'] == 'App.class'
    (tmp_path / 'App.class').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='changed'):
        observe(trace, tmp_path, tmp_path, known, 'r')


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Linux worker browser and sandbox')
def test_visual_capture_is_job_scoped_and_layout_is_measured(tmp_path):
    from coder_project_runtime import run_revision
    from coder_evidence import resolve
    repo, store = fixture(tmp_path, {'index.html': '<!doctype html><title>Visual fixture</title><div style="width:1800px">Wide content</div>'})
    payload = store.get('check')['payload']
    store.update('check', payload={**payload, 'visual_policy': {'enabled': True}})
    profile = {'profiles': [], 'checks': [], 'services': [{'id': 'app', 'cwd': '.', 'command': 'python3 -m http.server {port} --bind 127.0.0.1'}]}
    result = run_revision(store, 'check', repo, profile, project_id='visual')
    row = next(c for c in result['checks'] if c['id'] == 'visual:app')
    assert row['classification'] == 'application_defect' and row['layout_defects'], row
    assert {s['viewport'] for s in row['screenshots']} == {v['name'] for v in payload['settings']['daedalus_browser_viewports']}
    assert result['ui_required']
    for shot in row['screenshots']:
        record = resolve(store, 'job', shot['evidence']['id'])
        assert record['revision_id'] == payload['revision_id']
        with pytest.raises(FileNotFoundError):
            resolve(store, 'other-job', shot['evidence']['id'])


def test_explicit_protected_file_changes_require_clarification():
    from coder_scope import protected_conflict
    assert protected_conflict('Update README.md', ['README.md'])
    assert not protected_conflict('Do not modify README.md', ['README.md'])
    assert not protected_conflict('Update README.md.backup', ['README.md'])


def test_explicit_visual_request_excludes_negated_requests():
    from coder_policy7_visual import required
    assert required('Include visual verification of this web page')
    assert required('Review the screenshots')
    assert not required('Do not perform visual review')
    assert not required('Skip visual verification')


def test_replaced_interface_is_retired_only_after_scope_review():
    from coder_scope import active_interfaces
    job = {'task': 'Replace #export-csv with #export-json; retain #add', 'brief': {
        'outcomes': [{'text': 'Export with #export-json'}, {'text': 'Keep #add'}],
        'requirement_history': [{'parent_id': 'o1', 'action': 'replace', 'parent_outcome': {'text': 'Export with #export-csv'}}]}}
    assert 'export-csv' in active_interfaces(job)['selectors']
    job['brief']['scope_review'] = [{'parent_id': 'o1', 'supported': True}]
    assert active_interfaces(job)['selectors'] == ['export-json', 'add']


def test_continue_waits_for_cancelled_controller_to_exit(monkeypatch, tmp_path):
    import config
    import database as db
    import coder_jobs as controller
    from db import coder_jobs as store
    from context_policy import DEFAULTS
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'race.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    monkeypatch.setattr(controller, '_RUNNERS', {})
    async def run():
        await db.init_db(); await db.create_conversation('conv', model='local')
        job = await controller.create('conv', 'Build', model='local')
        await store.save(job['id'], state='cancelled', resume_state='coding', repair_round=1)
        release = asyncio.Event()
        async def old_controller():
            await release.wait()
            current = await store.get(job['id'])
            if current['state'] != 'cancelled':
                await store.save(job['id'], state='blocked', blocker='Stale cancelled operation')
        old = asyncio.create_task(old_controller()); controller._RUNNERS[job['id']] = old
        resumed = asyncio.create_task(controller.resume(job['id']))
        await asyncio.sleep(.02)
        assert (await store.get(job['id']))['state'] == 'cancelled'
        release.set(); await resumed
        current = await store.get(job['id'])
        assert current['state'] == 'queued' and current['repair_round'] == 1
        assert current['resume_after_inspect'] == 'coding'
    asyncio.run(run())


def test_scope_review_recovers_format_without_replanning_or_waiving_parents():
    from coder_scope import review_scope
    brief = make_brief(_scope(), 'Replace CSV with JSON', PARENTS)
    responses = iter([{'decisions': {'o1': {'supported': True, 'reason': 'Explicit replacement'}}},
        {'decisions': {'o1': {'supported': True, 'reason': 'Explicit replacement'},
                       'o2': {'supported': True, 'reason': 'Retained unchanged'}}}])
    prompts, retained = [], []
    def chat(prompt, schema):
        prompts.append(prompt)
        assert schema['properties']['decisions']['required'] == ['o1', 'o2']
        if len(prompts) == 1:
            proposals = json.loads(prompt.split('\n', 1)[1])['proposals_by_parent']
            assert proposals['o1']['original_requirement'] == PARENTS[0]['text']
            assert proposals['o1']['proposed_action'] == 'replace'
            assert proposals['o1']['resulting_requirements'][0]['text'] == 'Store records in JSON'
            assert 'id' not in proposals['o1']['resulting_requirements'][0]
            assert proposals['o2']['proposed_action'] == 'retain'
        return json.dumps(next(responses))
    rows = review_scope(brief, 'Replace CSV with JSON', chat, retained)
    assert len(rows) == len(retained) == 2
    assert 'Correct only the response format' in prompts[1]
    def unsupported(*args):
        return json.dumps({'decisions': {'o1': {'supported': False, 'reason': 'Replacement is ambiguous'},
                                         'o2': {'supported': True, 'reason': 'Retained'}}})
    with pytest.raises(ScopeClarification, match='ambiguous'):
        review_scope(brief, 'Change storage', unsupported, [])
    with pytest.raises(ScopeClarification, match='could not verify'):
        review_scope(brief, 'Change storage', lambda *args: '{}', [])
    formats = []
    def no_schema(prompt, schema):
        formats.append(schema)
        if schema:
            raise ValueError('Runtime rejected response format')
        return json.dumps({'decisions': {name: {'supported': True, 'reason': 'Supported'} for name in ('o1', 'o2')}})
    assert len(review_scope(brief, 'Replace CSV with JSON', no_schema, [])) == 2
    assert formats[0] and formats[1] is None


def test_repeated_retained_outcomes_are_not_duplicated_and_lineage_stays_bound():
    answer = {'outcomes': [{'text': p['text'], 'evidence_types': p['evidence_types']} for p in PARENTS],
              'batches': [{'task': 'Keep behavior'}],
              'inheritance': [{'parent_id': p['id'], 'action': 'retain'} for p in PARENTS]}
    brief = make_brief(answer, 'Keep behavior', PARENTS)
    assert len(brief['outcomes']) == 2
    assert [r['active_outcome_ids'] for r in brief['requirement_history']] == [['o1'], ['o2']]


def test_followup_schema_limits_quotes_to_current_request_and_accepts_keyed_dispositions():
    from coder_scope import plan_schema
    request = 'Replace CSV with JSON. Keep validation unchanged.'
    schema = plan_schema(request, PARENTS)
    inherited = schema['properties']['inheritance']
    assert inherited['required'] == ['o1', 'o2']
    quotes = inherited['properties']['o1']['properties']['request_quote']['enum']
    assert all(quote in request for quote in quotes)
    assert PARENTS[0]['text'] not in quotes
    answer = _scope(quote='Replace CSV with JSON.')
    answer['inheritance'] = {row['parent_id']: {k: v for k, v in row.items() if k != 'parent_id'} for row in answer['inheritance']}
    brief = make_brief(answer, request, PARENTS)
    assert [o['text'] for o in brief['outcomes']] == ['Reject negative amounts', 'Store records in JSON']


@pytest.mark.parametrize('eventually_supported', [False, True])
def test_scope_review_replans_a_contradictory_retention_once_before_parking(tmp_path, eventually_supported):
    from coder_policy7_ops import Operations
    from context_policy import DEFAULTS
    repo, store = fixture(tmp_path, {'app.py': 'VALUE=1\n'})
    seen = []
    def chat(store, op, role, messages, **kwargs):
        seen.append(role)
        if role == 'architect':
            return json.dumps(_scope())
        supported = eventually_supported and len([r for r in seen if r == 'reviewer']) > 1
        return json.dumps({'decisions': {'o1': {'supported': supported, 'reason': 'Replace old CSV behavior'},
                                         'o2': {'supported': True, 'reason': 'Retain validation'}}})
    job = {'task': 'Replace CSV with JSON', 'settings': dict(DEFAULTS), 'inherited': PARENTS,
           'revision_id': store.get('check')['payload']['revision_id']}
    ops = Operations(store, 'check', repo, tmp_path, job)
    ops.chat = chat
    result = ops.plan('check')
    assert ('brief' in result) is eventually_supported
    assert ('scope_question' in result) is not eventually_supported
    assert seen == ['architect', 'reviewer', 'architect', 'reviewer']
    assert len(result['scope_review_text']) == 2


@pytest.mark.parametrize('runtime', ['java', 'dotnet'])
def test_managed_compiled_audit_observes_real_application_load(tmp_path, runtime):
    from coder_project_runtime import run_revision
    if not all(shutil.which(t) for t in ('strace', 'bwrap', runtime)):
        pytest.skip('Managed runtime requires Linux Codebox')
    files = {'App.java': 'public class App { public static void main(String[] args) { System.exit(7); } }'} if runtime == 'java' else {
        'App.csproj': '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>',
        'Program.cs': 'System.Environment.Exit(7);'}
    command = 'javac App.java' if runtime == 'java' else 'dotnet build --nologo -v:q'
    args = '["java", "-cp", root, "App"]' if runtime == 'java' else '["dotnet", root+"/bin/Debug/net8.0/App.dll"]'
    repo, store = fixture(tmp_path, files)
    payload = store.get('check')['payload']; store.update('check', payload={**payload, 'seconds_remaining': 180})
    audit = tmp_path / 'audit-source'; audit.mkdir()
    (audit / 'test_run.py').write_text('import os, subprocess\nroot=os.environ["DAEDALUS_PROJECT_ROOT"]\n'
        + 'result=subprocess.run(' + args + ')\nassert result.returncode==7\n')
    (audit / 'audit.json').write_text(json.dumps({'checks': [{'command': '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_run.py', 'outcomes': ['o1']}]}))
    outcomes = [{'id': 'o1', 'text': 'Exits with seven', 'evidence_types': ['behavior']}]
    profile = {'profiles': [], 'services': [], 'checks': [{'id': 'build', 'phase': 'build', 'origin': 'project', 'command': command}]}
    result = run_revision(store, 'check', repo, profile, project_id=runtime, audit_source=audit, checks=audit_checks(audit, outcomes, repo.root))
    row = next(c for c in result['checks'] if c['id'] == 'audit-1')
    assert row['passed'], row
    assert row['provenance_kind'] == 'compiled_subprocess'
    assert row['process_evidence']['processes']
