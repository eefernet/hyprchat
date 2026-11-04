"""Modern-only routing, settings migration and read-only historical behavior."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

import coder_jobs
from coder_engine_controller import step
from coder_presentation import presentation
from coder_repository import Repository
from coder_worker_runtime import validate_qa
from context_policy import DEFAULTS, migrate_settings, validate_patch


def test_private_sdk_bookkeeping_never_writes_controller_registry(tmp_path, monkeypatch):
    import hephaestus_inventory as inventory
    from coder_worker_runtime import WorkerStore
    calls=[]
    monkeypatch.setattr(inventory, 'managed_root', lambda _: tmp_path)
    monkeypatch.setattr(inventory, 'register_store', lambda path: calls.append(path))
    private=WorkerStore(tmp_path/'private', controller_owned=False)
    private.create('operation', 'job', 'code', {})
    assert private.get('operation')['status']=='queued'
    assert calls==[] and not (tmp_path/'control').exists()
    controller=WorkerStore(tmp_path/'controller')
    assert calls==[controller.root]


def test_settings_migration_preserves_effective_models_and_inference():
    old = {'planning_model': 'chat:local', 'coder_model': 'old:local', 'builder_model': 'qwen3.6:local',
           'openhands_num_ctx': 65536, 'daedalus_model_calls': 120,
           'daedalus_role_outputs': {'builder': 16384, 'architect': 4096},
           'daedalus_role_thinking': {'builder': 'on'}, 'daedalus_engine': 'legacy'}
    original = copy.deepcopy(old)
    new = migrate_settings(old)
    assert old == original
    assert new['coder_model'] == 'qwen3.6:local'
    assert new['planning_model'] == 'chat:local'
    assert new['openhands_num_ctx'] == 65536 and new['daedalus_model_calls'] == 120
    assert new['daedalus_role_outputs'] == {'builder': 16384}
    assert new['daedalus_role_thinking'] == {'builder': 'on'}
    assert new['daedalus_engine'] == 'openhands159' and new['daedalus_settings_version'] == 2
    assert migrate_settings(new) == new
    assert migrate_settings({'coder_model':'coding'})['coder_model'] == 'coding'


@pytest.mark.parametrize('patch', [{'daedalus_engine':'legacy'}, {'daedalus_v3_enabled':False},
    {'aider_enabled':True}, {'architect_model':'x'}, {'builder_model':'x'}, {'daedalus_policy7_builds':True}])
def test_stale_settings_clients_receive_explicit_retirement_error(patch):
    with pytest.raises(ValueError, match='[Rr]etired'):
        validate_patch(patch, DEFAULTS)


def test_retired_job_resume_is_rejected_without_dispatch(monkeypatch):
    old = {'id':'old', 'engine':'legacy', 'state':'blocked', 'calls_used':12, 'repair_round':2}
    async def get(_): return copy.deepcopy(old)
    monkeypatch.setattr(coder_jobs.store, 'get', get)
    with pytest.raises(ValueError, match='retired'):
        asyncio.run(coder_jobs.resume('old', budget_mode='new_allowance'))
    assert old['calls_used'] == 12 and old['repair_round'] == 2
    view = presentation(old)
    assert not view['actions']['resume']['enabled']
    assert 'retired' in view['actions']['resume']['disabled_reason']


@pytest.mark.parametrize('version3',[True,False])
def test_retired_cancel_route_is_read_only_and_explicit(monkeypatch,version3):
    import main
    from fastapi import HTTPException
    old={'id':'old','engine':'legacy','state':'blocked','calls_used':12}
    async def modern(_):return dict(old) if version3 else None
    async def historical(_):return dict(old)
    async def mutation(*a,**k):pytest.fail('Historical state must not be changed')
    monkeypatch.setattr(main.coder_job_store,'get',modern)
    monkeypatch.setattr(main.db,'get_coder_workflow',historical)
    monkeypatch.setattr(main.db,'update_coder_workflow',mutation)
    monkeypatch.setattr(main.coder_job_store,'request_cancel',mutation)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.cancel_coder_workflow('old'))
    assert error.value.status_code==409 and 'retired' in error.value.detail


def test_question_controller_only_inspects_and_answers():
    job = {'id':'q', 'engine':'openhands159', 'mode':'ask_uploaded_project', 'state':'queued'}
    operations=[]
    async def save(_, **changes): job.update(changes); return dict(job)
    async def operate(current, kind, **kwargs):
        operations.append(kind)
        if kind == 'inspect':
            return {'inventory':{'files':1},'workspace':'private','snapshot':{'revision':'abc'}}, dict(job)
        assert kind == 'qa'
        return {'answer':'main.py:1 defines greet.'}, dict(job)
    controller=SimpleNamespace(store=SimpleNamespace(save=save), _operate=operate)
    async def run():
        assert not await step(dict(job), controller)
        assert job['state']=='answering'
        assert await step(dict(job), controller)
    asyncio.run(run())
    assert operations==['inspect','qa']
    assert job['answer']=='main.py:1 defines greet.' and job['qa_revision_id']=='abc'
    assert job['state']=='completed' and 'artifact' not in job


def test_question_citations_are_current_and_do_not_modify_source(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'main.py').write_text('def greet():\n    return "hello"\n')
    repo=Repository(root,tmp_path/'state',[])
    before=(root/'main.py').read_bytes()
    validate_qa({'answer':'main.py:1 defines greet; main.py:2 returns hello.'},repo)
    with pytest.raises(ValueError,match='Cite'):
        validate_qa({'answer':'It returns hello.'},repo)
    with pytest.raises(ValueError,match='citation'):
        validate_qa({'answer':'main.py:99 returns hello.'},repo)
    assert (root/'main.py').read_bytes()==before


def test_built_in_seed_preserves_custom_persona(monkeypatch):
    from agents import personas
    calls=[]
    async def configs():return [{'id':'custom','name':'🏛️ Daedalus','system_prompt':'My own instructions','base_model':'my-model'}]
    async def kbs():return []
    async def update(*args,**kwargs):calls.append((args,kwargs))
    monkeypatch.setattr(personas.db,'get_model_configs',configs)
    monkeypatch.setattr(personas.db,'get_kbs',kbs)
    monkeypatch.setattr(personas.db,'update_model_config',update)
    asyncio.run(personas.seed_coder_bot_v2())
    assert calls==[]
