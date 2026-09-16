"""Offline checks for the conversational developer persona's existing-tool wiring."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import personas


def test_master_developer_seed_is_scoped_idempotent_and_preserves_model(monkeypatch):
    rows = []
    async def create(id, **payload): rows.append(dict(id=id, **payload))
    async def update(id, **payload): next(r for r in rows if r['id']==id).update(payload)
    monkeypatch.setattr(personas.db, 'get_model_configs', AsyncMock(side_effect=lambda: rows.copy()))
    monkeypatch.setattr(personas.db, 'get_kbs', AsyncMock(return_value=[{'id':'kb-owned','name':'Coder Reference Docs'}]))
    monkeypatch.setattr(personas.db, 'create_model_config', create)
    monkeypatch.setattr(personas.db, 'update_model_config', update)
    first = asyncio.run(personas.seed_master_developer())
    assert rows[0]['parameters']['profile_type']=='persona'
    assert rows[0]['tool_ids']==['quick_search']
    assert rows[0]['kb_ids']==['kb-owned']
    assert rows[0]['base_model']=='qwen3.5:27b'
    rows[0]['base_model']='user-chosen-model'
    rows[0]['kb_ids'].append('kb-foreign')
    second = asyncio.run(personas.seed_master_developer())
    assert first['id']==second['id'] and second['existed']
    assert len(rows)==1 and rows[0]['base_model']=='user-chosen-model'
    assert rows[0]['kb_ids']==['kb-owned']
    prompt=rows[0]['system_prompt']
    assert 'all programming languages' in prompt
    assert 'execute_code' in prompt and 'download_file' in prompt
    assert rows[0]['parameters']['persona']['advanced_prompt']==prompt
    assert rows[0]['parameters']['persona']['first_message']


def test_missing_owned_kb_is_reported_without_cross_user_attachment(monkeypatch):
    monkeypatch.setattr(personas.db,'get_model_configs',AsyncMock(return_value=[]))
    monkeypatch.setattr(personas.db,'get_kbs',AsyncMock(return_value=[]))
    create=AsyncMock();monkeypatch.setattr(personas.db,'create_model_config',create)
    result=asyncio.run(personas.seed_master_developer())
    assert result['kb_missing'] and result['kb_ids']==[]
    assert create.call_args.kwargs['tool_ids']==['quick_search']
