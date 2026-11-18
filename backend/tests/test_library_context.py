import asyncio
import json
import pytest
import database as db
import library_search
import context_inspector


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'library.db'))
    asyncio.run(db.init_db())


def test_index_tracks_edits_deletions_and_ownership(isolated):
    async def run():
        await db.create_conversation(id='conversation',title='Search fixture')
        mid=await db.add_message('conversation','user','uniquecobalt evidence')
        await db.create_note('note',title='Cobalt note',content='uniquecobalt reminder')
        hits=(await library_search.search('uniquecobalt'))['results']
        assert {h['kind'] for h in hits}=={'chat','note'}
        await db.update_message(mid,content='replacement')
        assert len((await library_search.search('uniquecobalt'))['results'])==1
        await db.delete_note('note')
        assert not (await library_search.search('uniquecobalt'))['results']
        other=await db.create_user('Other');token=db.set_current_user_id(other['id'])
        try: assert not (await library_search.search('replacement'))['results']
        finally: db.reset_current_user_id(token)
        await library_search.rebuild()
        assert len((await library_search.search('replacement'))['results'])==1
    asyncio.run(run())


def test_context_is_captured_without_credentials_or_hidden_reasoning(isolated):
    async def run():
        await db.create_conversation(id='conversation',title='Context')
        mid=await db.add_message('conversation','assistant','visible')
        assert not (await context_inspector.inspect(mid))['available']
        await context_inspector.capture(mid,model='selected',provider='ollama',
            options={'num_ctx':32000,'api_key':'SECRET'},
            memories={'global':{'memory_ids':['m1'],'context':'api_key=TOPSECRET preference blue'}},
            retrieved='Saved passage',sources=[{'title':'Source','url':'https://example.com/?api_key=HIDDEN'}],
            tools=['research'],messages=[{'role':'assistant','content':'PRIVATE REASONING','thinking':'HIDDEN'}],original_chars=100)
        await context_inspector.record_tool(mid,'research')
        data=await context_inspector.inspect(mid);encoded=json.dumps(data)
        assert data['rounds'][0]['model']=='selected'
        assert data['rounds'][0]['executed_tools']==['research']
        assert all(s not in encoded for s in ['TOPSECRET','HIDDEN','PRIVATE REASONING','SECRET'])
        other=await db.create_user('Other');token=db.set_current_user_id(other['id'])
        try:
            with pytest.raises(LookupError): await context_inspector.inspect(mid)
        finally:db.reset_current_user_id(token)
    asyncio.run(run())


def test_panel_search_lists_a_chat_once_with_its_best_message(isolated):
    """Found 2026-11-17: "rack" listed "Plan the rack move weekend" seven times, once per message."""
    async def run():
        await db.create_conversation(id='rack',title='Plan the rack move weekend')
        await db.add_message('rack','user','uniquerack move plan')
        for n in range(6): await db.add_message('rack','assistant',f'uniquerack step {n}')
        await db.create_conversation(id='other',title='Other chat')
        await db.add_message('other','user','uniquerack once')
        await db.create_note('note',title='Rack note',content='uniquerack reminder')
        assert len((await library_search.search('uniquerack'))['results'])==9      # unchanged: one per message
        grouped=await library_search.search_grouped('uniquerack')
        assert grouped['total']==3 and len(grouped['results'])==3
        by={(r['kind'],r['parent_id'] if r['kind']=='chat' else r['source_id']):r for r in grouped['results']}
        rack=by[('chat','rack')]
        assert rack['matches']==7 and rack['title']=='Plan the rack move weekend'
        assert rack['parent_id']=='rack' and rack['excerpt']                    # a message of that chat, to open on click
        assert by[('chat','other')]['matches']==1 and by[('note','note')]['matches']==1
        page=await library_search.search_grouped('uniquerack',limit=2,offset=2)
        assert len(page['results'])==1 and page['total']==3
        assert (await library_search.search_grouped('  '))['results']==[]
    asyncio.run(run())

