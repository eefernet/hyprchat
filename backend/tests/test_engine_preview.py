"""Preview ownership, immutable revision and capability boundaries."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from routes.coder_preview import owned_job, published, public_session
from coder_preview import read_session, alive
from coder_preview import expired, update
from coder_engine_ops import summarize


def test_preview_requires_delivered_revision():
    job={'candidate_artifact':{'metadata':{'revision_id':'a'*40}},'artifact':None}
    assert published(job,'a'*40)
    assert not published(job,'b'*40)
    assert not published({'artifact':None},'')


def test_worker_capability_is_never_sent_to_browser():
    assert public_session({'id':'a','token':'secret','pid':1,'vnc_port':5900,'revision':'x','status':'running'})=={
        'id':'a','revision':'x','status':'running'}


def test_preview_job_lookup_keeps_owner_scope(monkeypatch):
    from routes import coder_preview
    async def absent(identity): return None
    monkeypatch.setattr(coder_preview.store,'get',absent)
    with pytest.raises(HTTPException) as caught:asyncio.run(owned_job('another-owners-job'))
    assert caught.value.status_code==404


def test_preview_record_rejects_paths_and_other_jobs(tmp_path):
    import json
    identity='a'*32; directory=tmp_path/'previews'/identity;directory.mkdir(parents=True)
    (directory/'session.json').write_text(json.dumps({'job_id':'owned','id':identity}))
    for identity_arg,owner in [('../secret','owned'),(identity,'other')]:
        with pytest.raises(LookupError):read_session(tmp_path,identity_arg,owner)
    assert read_session(tmp_path,identity,'owned')['job_id']=='owned'
    assert not alive({'pid':999999999,'process_started':0})


def test_unit_tests_cannot_claim_gui_behavior():
    row={'passed':True,'revision_id':'new','evidence_version':3,'coverage_observed':True,
         'execution_succeeded':True,'source_bindings':[{'path':'logic.py'}]}
    assert summarize([row],'new')['checks_passed']
    assert not summarize([row],'new',['web'])['checks_passed']
    interaction={**row,'origin':'confirmed_interaction','test_interfaces':['web']}
    assert summarize([row,interaction],'new',['web'])['checks_passed']
    assert not summarize([row,interaction],'new',['desktop'])['checks_passed']


def test_pinned_lifetime_and_status_updates_preserve_revision(tmp_path):
    import json,time
    state={'id':'a'*32,'revision':'r1','lifecycle':'temporary','expires_at':time.time()-1}
    assert expired(state)
    state.update(lifecycle='pinned',expires_at=None,desired_state='running')
    assert not expired(state)
    path=tmp_path/'session.json';path.write_text(json.dumps(state))
    update(path,status='running',vnc_port=5900)
    update(path,desired_state='stopped')
    result=update(path,status='closed')
    assert result['revision']=='r1' and result['lifecycle']=='pinned' and result['desired_state']=='stopped'


def test_closed_temporary_sessions_do_not_block_pinned_restart(monkeypatch):
    import coder_preview
    monkeypatch.setattr(coder_preview,'alive',lambda state:state.get('process_alive',False))
    state={'lifecycle':'temporary','status':'closed','desired_state':'running'}
    assert not coder_preview.reserves_slot(state)
    assert coder_preview.reserves_slot({**state,'lifecycle':'pinned'})
    assert not coder_preview.reserves_slot({**state,'lifecycle':'pinned','status':'failed'})
    assert coder_preview.reserves_slot({**state,'status':'running','process_alive':True})


def test_exited_supervisor_is_not_a_live_application(monkeypatch):
    import coder_preview
    from types import SimpleNamespace
    monkeypatch.setattr(coder_preview.psutil,'Process',lambda pid:SimpleNamespace(
        create_time=lambda:123,is_running=lambda:True,status=lambda:coder_preview.psutil.STATUS_ZOMBIE))
    assert not coder_preview.alive({'pid':1,'process_started':123})


def test_clock_correction_does_not_restart_live_pinned_application(monkeypatch):
    import coder_preview
    from types import SimpleNamespace
    identity={'boot_id':'current-boot','start_ticks':12345}
    monkeypatch.setattr(coder_preview,'process_identity',lambda pid:identity)
    monkeypatch.setattr(coder_preview.psutil,'Process',lambda pid:SimpleNamespace(
        create_time=lambda:155,is_running=lambda:True,status=lambda:'sleeping'))
    state={'pid':1,'process_started':123,'process_identity':dict(identity)}
    assert coder_preview.alive(state)  # NTP moved the wall clock by 32 seconds.
    assert not coder_preview.alive({**state,'process_identity':{**identity,'start_ticks':12346}})
    assert not coder_preview.alive({**state,'process_identity':{**identity,'boot_id':'previous-boot'}})
