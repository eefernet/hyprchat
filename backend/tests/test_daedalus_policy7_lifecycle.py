"""Saved-response controller journeys, using real native checks and snapshots."""
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_policy7 import Experiment


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox audit sandbox')
def test_two_failed_application_repairs_persist_across_continue(tmp_path):
    edits = []
    def editor(store, op, root, task, **kwargs):
        if kwargs.get('read_root'):
            (root / 'test_value.py').write_text('from subject import VALUE\nassert VALUE == 99, VALUE\n')
            (root / 'audit.json').write_text(json.dumps({'checks': [
                {'command': 'python3 {audit}/test_value.py', 'outcomes': ['o1']}]}))
            return {'changed': ['test_value.py', 'audit.json']}
        edits.append(len(edits) + 1)
        (root / 'subject.py').write_text(f'VALUE={edits[-1]}\n')
        return {'changed': ['subject.py']}
    def chat(store, op, role, messages, **kwargs):
        if role == 'architect':
            return json.dumps({'outcomes': ['VALUE is 99'], 'batches': [{'task': 'Set VALUE to 99'}]})
        evidence = json.loads(messages[0]['content'].split('\n', 1)[1])
        return json.dumps({'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'failed', 'reason': 'Executed assertion fails'}],
            'diagnoses': [{'check_id': c['id'], 'kind': 'application_defect', 'reason': 'VALUE differs from requested 99'}
                          for c in evidence['checks'] if not c['passed']], 'missing_outcomes': []})
    experiment = Experiment(tmp_path / 'job', 'Set VALUE to 99', model='local', ollama_url='http://unused',
                            files={'subject.py': 'VALUE=0\n'}, editor=editor, chat=chat)
    result = experiment.run()
    assert result['state'] == 'candidate', result.get('reason')
    assert result['repair_round'] == 2 and edits == [1, 2, 3], result.get('reason')
    original = Path(result['artifact']['path']).read_bytes()
    restarted = Experiment(tmp_path / 'job', editor=editor, chat=chat)
    with pytest.raises(ValueError, match='Both application repair rounds'):
        restarted.resume()
    assert restarted.job['repair_round'] == 2 and edits == [1, 2, 3]
    assert Path(result['artifact']['path']).read_bytes() == original


@pytest.mark.skipif(not shutil.which('bwrap'), reason='Codebox audit sandbox')
def test_bad_audit_path_is_corrected_without_application_repair(tmp_path):
    edits, authors = [], []
    def editor(store, op, root, task, **kwargs):
        if kwargs.get('read_root'):
            authors.append(1)
            # __file__-relative roots are normalized mechanically; this path needs a real correction.
            program = 'from pathlib import Path\nimport subprocess,sys\nr=subprocess.run([sys.executable,str(Path("/nonexistent-audit-root")/"subject.py")],capture_output=True,text=True)\nassert r.returncode==0,r.stderr\n'
            if len(authors) > 1:
                program = 'from subject import VALUE\nassert VALUE==99\n'
            (root / 'test_value.py').write_text(program)
            (root / 'audit.json').write_text(json.dumps({'checks': [{'command': 'python3 {audit}/test_value.py', 'outcomes': ['o1']}]}))
            return {'changed': ['test_value.py', 'audit.json']}
        edits.append(1); (root / 'subject.py').write_text('VALUE=99\n')
        return {'changed': ['subject.py']}
    def chat(store, op, role, messages, **kwargs):
        if role == 'architect':
            return json.dumps({'outcomes': ['VALUE is 99'], 'batches': [{'task': 'Set VALUE to 99'}]})
        return json.dumps({'scope_complete': True,
            'outcomes': [{'id': 'o1', 'status': 'passed', 'reason': 'Check the exact execution'}],
            # An incorrect model diagnosis must not override missing project provenance.
            'diagnoses': [{'check_id': 'audit-1', 'kind': 'application_defect', 'reason': 'Wrong diagnosis'}], 'missing_outcomes': []})
    experiment = Experiment(tmp_path / 'job', 'Set VALUE to 99', model='local', ollama_url='http://unused',
                            files={'subject.py': 'VALUE=0\n'}, editor=editor, chat=chat)
    result = experiment.run()
    assert result['state'] == 'accepted', result.get('reason')
    assert result['repair_round'] == 0 and result['audit_corrections'] == 1
    assert len(edits) == 1 and len(authors) == 2


# Code audit 2026-09-21: stage-machine edges, driven through the real controller step with a fake worker.
def _drive(monkeypatch, tmp_path, job_changes, operate):
    import asyncio
    import config
    import database as db
    import coder_jobs as controller
    from db import coder_jobs as store
    from context_policy import DEFAULTS
    from coder_policy7_controller import step
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(config, 'CONTEXT_SETTINGS', {**DEFAULTS, 'daedalus_v3_enabled': True, 'daedalus_policy7_builds': True})
    monkeypatch.setattr(controller, 'spawn', lambda *args: None)
    monkeypatch.setattr(controller, '_operate', operate)
    async def scenario():
        await db.init_db()
        await db.create_conversation('conversation', model='coder:local')
        job = await controller.create('conversation', 'task', model='coder:local', key='edge')
        job = await store.save(job['id'], **job_changes)
        await step(job, controller)
        return await store.get(job['id'])
    return asyncio.run(scenario())


BRIEF = {'project_name': 'App', 'outcomes': [{'id': 'o1', 'text': 'Works', 'evidence_types': ['behavior', 'tests'], 'component': '.'}],
         'batches': [{'task': 'Build', 'files': ['app.py']}]}


def test_execute_is_granted_once_per_revision_then_the_job_parks(monkeypatch, tmp_path):
    # decide() -> 'execute' had no counter: checking -> auditing -> accepting looped with a paid review each lap.
    verdict = {'scope_complete': True, 'outcomes': [{'id': 'o1', 'status': 'passed', 'reason': ''}], 'diagnoses': [], 'notes': []}
    async def operate(job, kind, **extra):
        assert kind == 'accept'
        return verdict, job
    audit = {'id': 'audit-1', 'origin': 'independent', 'passed': True, 'revision_id': 'r1', 'execution_id': 'op:1', 'outcomes': ['o1'],
             'evidence_types': ['behavior'], 'coverage_observed': True, 'execution_succeeded': True, 'source_bindings': [{'path': 'app.py', 'sha256': 'a' * 64}]}
    state = dict(state='accepting', revision_id='r1', brief=BRIEF, checks=[audit],
                 execution_profile={'checks': [{'id': '.:test:0', 'is_test': True, 'cwd': '.'}]})
    first = _drive(monkeypatch, tmp_path, state, operate)
    assert first['state'] == 'checking' and first['executed_revision'] == 'r1'
    (tmp_path / 'again').mkdir()
    second = _drive(monkeypatch, tmp_path / 'again', {**state, 'executed_revision': 'r1'}, operate)
    assert second['state'] == 'candidate_packaging' and 'already executed' in second['blocker']


def test_a_builder_out_of_its_share_is_verified_with_the_reserve_not_parked_unverified(monkeypatch, tmp_path):
    async def operate(job, kind, **extra):
        raise AssertionError('no code operation should be dispatched: ' + kind)
    spent = dict(state='coding', revision_id='r2', brief=BRIEF, builder='sdk', build_continuations=2, calls_used=95,
                 last_patch={'changed': ['app.py']})
    job = _drive(monkeypatch, tmp_path, spent, operate)
    assert job['state'] == 'checking' and not job.get('stop_limit')


def test_a_replaced_brief_starts_at_its_first_batch(monkeypatch, tmp_path):
    # batch was carried into the new brief: IndexError in implementation_task when it has fewer batches.
    async def operate(job, kind, **extra):
        return {'brief': BRIEF, 'plan_text': [], 'protected_files': []}, job
    job = _drive(monkeypatch, tmp_path, dict(state='planning', revision_id='r3', batch=3, narrowed_targets=['old.py']), operate)
    assert job['state'] == 'coding' and job['batch'] == 0 and job['narrowed_targets'] == []


def test_publication_resumes_only_onto_the_revision_that_was_accepted(monkeypatch, tmp_path):
    async def operate(job, kind, **extra):
        return {'snapshot': {'revision': 'new', 'tree': 't'}, 'inventory': {'files': 1}, 'workspace': '/w'}, job
    held = dict(state='inspecting', revision_id='old', resume_after_inspect='packaging', acceptance={'accepted': True, 'revision_id': 'old'})
    assert _drive(monkeypatch, tmp_path, held, operate)['state'] == 'checking'
    (tmp_path / 'same').mkdir()
    same = {**held, 'revision_id': 'new', 'acceptance': {'accepted': True, 'revision_id': 'new'}}
    assert _drive(monkeypatch, tmp_path / 'same', same, operate)['state'] == 'packaging'
