import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from evals import engine_protocol_checkpoint as checkpoint


@pytest.fixture
def saved(tmp_path):
    identity={'limits':[16384],'cases':list(checkpoint.CASES),'components':{},'version':1}
    expected={'model':'coder','digest':'frozen','settings':{'limit':3},'rows':[]}
    rows=[{'output_limit':16384,'repeat':1,'case':case,'calls':i+1,'passed':not i}
          for i,case in enumerate(checkpoint.CASES[:2])]
    operations={f"16384-1-{r['case']}":dict(calls=r['calls'],status='succeeded' if r['passed'] else 'failed',
        payload={'model':'coder','model_digest':'frozen','settings':{'limit':3}}) for r in rows}
    path=tmp_path/'report.json';report={**expected,'freeze':identity,'rows':rows};path.write_text(json.dumps(report))
    return path,expected,identity,SimpleNamespace(get=lambda key: operations.get(key)),operations


def test_resume_retains_successes_and_failures_without_new_dispatch(saved):
    path,expected,identity,store,operations=saved
    original=path.read_bytes()
    result=checkpoint.load(path,expected,identity,store,resume=True)
    assert [r['passed'] for r in result['rows']]==[True,False]
    assert result['resumes'][0]['retained_rows']==2
    assert path.with_name('report.before-resume.json').read_bytes()==original
    assert path.read_bytes()==original


@pytest.mark.parametrize('field', ['model','settings','freeze','usage','status'])
def test_changed_inputs_or_usage_block_resume(saved, field):
    path,expected,identity,store,operations=saved
    if field=='model':expected['model']='other'
    elif field=='settings':expected['settings']={'limit':99}
    elif field=='freeze':identity={**identity,'limits':[999]}
    elif field=='usage':next(iter(operations.values()))['calls']=99
    else:next(iter(operations.values()))['status']='running'
    with pytest.raises(ValueError):checkpoint.load(path,expected,identity,store,resume=True)
    assert not path.with_name('report.before-resume.json').exists()


def test_resume_is_explicit(saved):
    path,expected,identity,store,_=saved
    with pytest.raises(ValueError,match='--resume'):checkpoint.load(path,expected,identity,store)


@pytest.mark.parametrize('status,calls,cancelled',[('running',0,False),('succeeded',1,False),('queued',1,False),('queued',0,True)])
def test_uncertain_probe_never_replays(status,calls,cancelled):
    store=SimpleNamespace(get=lambda _:dict(status=status,calls=calls,cancel_requested=cancelled),
                          create=lambda *a:pytest.fail('Replayed probe'))
    with pytest.raises(ValueError,match='replay is forbidden'):checkpoint.admit(store,'probe',{}, {},lambda:None)


def test_admission_wait_is_durable_and_bounded(monkeypatch):
    clock=[100.];monkeypatch.setattr(checkpoint.time,'time',lambda:clock[0])
    monkeypatch.setattr(checkpoint.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    created=[];snapshots=[]
    def busy(*args):created.append(args);raise BlockingIOError()
    store=SimpleNamespace(get=lambda _:None,create=busy)
    report={};payload={'settings':{'daedalus_preparation_seconds':5}}
    with pytest.raises(TimeoutError):checkpoint.admit(store,'probe',payload,report,lambda:snapshots.append(copy.deepcopy(report)))
    assert len(created)==3 and report['admission']['probe']['waits']==3
    with pytest.raises(TimeoutError):checkpoint.admit(store,'probe',payload,report,lambda:None)
    assert len(created)==3 and snapshots[-1]['admission']['probe']['deadline']==105


def test_admission_recovers_before_one_dispatch(monkeypatch):
    monkeypatch.setattr(checkpoint.time,'sleep',lambda _:None)
    calls=[];updates=[]
    def create(*args):
        calls.append(args)
        if len(calls)==1:raise BlockingIOError()
    store=SimpleNamespace(get=lambda _:None,create=create,update=lambda *a,**k:updates.append((a,k)))
    report={};checkpoint.admit(store,'probe',{'settings':{}},report,lambda:None)
    assert len(updates)==1 and report['admission']['probe']['waits']==1


def test_probe_identity_detects_prompt_and_assertion_changes(tmp_path):
    runner=Path(__file__).parents[1]/'evals/run_engine_protocol.py'
    original=checkpoint.probe_hash(runner)
    for old,new in [('a=17','a=18'),("strip()=='42'","strip()=='43'")]:
        target=tmp_path/'runner.py';target.write_text(runner.read_text().replace(old,new))
        assert checkpoint.probe_hash(target)!=original


def test_legacy_checkpoint_requires_matching_immutable_sources(saved, tmp_path):
    import hashlib
    path,expected,identity,store,_=saved
    raw=json.loads(path.read_text());raw.pop('freeze');path.write_text(json.dumps(raw))
    runner=Path(__file__).parents[1]/'evals/run_engine_protocol.py'
    release=tmp_path/'release';original=release/'backend/evals/run_engine_protocol.py'
    original.parent.mkdir(parents=True);original.write_bytes(runner.read_bytes())
    manifest=release/'qualification-files.json'
    manifest.write_text(json.dumps({'backend/evals/run_engine_protocol.py':hashlib.sha256(original.read_bytes()).hexdigest(),
                                    'backend/controller.py':'verified'}))
    identity={**identity,'probes':checkpoint.probe_hash(runner),'components':{'backend/controller.py':'verified'}}
    with pytest.raises(ValueError,match='frozen inputs'):checkpoint.load(path,expected,identity,store,resume=True)
    result=checkpoint.load(path,expected,identity,store,resume=True,adopt=manifest)
    assert result['checkpoint_adoption']['previous_report_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    original.write_text(original.read_text().replace('a=17','a=18'))
    with pytest.raises(ValueError,match='prompts or assertions'):checkpoint.load(path,expected,identity,store,resume=True,adopt=manifest)
