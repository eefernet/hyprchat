"""Preview ownership, immutable revision and capability boundaries."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from routes.coder_preview import owned_job, published, public_session
from coder_preview import read_session, alive
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
