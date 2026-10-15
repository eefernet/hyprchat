import asyncio
import json
import pytest
import council
import council_runtime as runtime


def test_cloud_requires_opt_in_cap_and_exact_known_pricing():
    with pytest.raises(ValueError,match='enablement'):
        runtime.Budget({},['openai:gpt-4.1-mini'])
    with pytest.raises(ValueError,match='unknown pricing'):
        runtime.Budget({'cloud_enabled':True,'cost_limit_usd':1},['custom:anything'])
    with pytest.raises(ValueError,match='unknown pricing'):
        runtime.Budget({'cloud_enabled':True,'cost_limit_usd':1},['openai:gpt-5-future-unknown'])
    assert runtime.Budget({},['local']).cap==0


def test_parallel_reservations_cannot_overbook_and_unknown_usage_stops_paid_calls():
    budget=runtime.Budget({'cloud_enabled':True,'cost_limit_usd':.01},['openai:gpt-4.1-mini'])
    amount=budget.reserve('openai:gpt-4.1-mini',[{'role':'user','content':'hello'}],3000)
    with pytest.raises(ValueError,match='cap reached'):
        budget.reserve('openai:gpt-4.1-mini',[{'role':'user','content':'hello'}],3000)
    budget.settle('openai:gpt-4.1-mini',amount,None)
    with pytest.raises(ValueError,match='uncertain'):
        budget.reserve('openai:gpt-4.1-mini',[],1)


def test_mixed_provider_members_and_host_keep_selected_models(monkeypatch):
    async def run():
        called=[];saved=[]
        async def stream(http,model,messages,**kwargs):
            called.append(model)
            if model=='missing-local': raise RuntimeError('Selected model unavailable')
            yield {'type':'token','content':'A concise fixture response.'}
            yield {'type':'usage','prompt_tokens':10,'gen_tokens':5}
            yield {'type':'finish'}
        async def add(conv,role,content,metadata=None): saved.append((role,content,metadata))
        async def usage(*a,**k): pass
        monkeypatch.setattr(runtime.providers,'stream_chat',stream)
        monkeypatch.setattr(council.db,'add_message',add)
        monkeypatch.setattr(council.db,'record_token_usage',usage)
        config={'id':'fixture','host_model':'openai:gpt-4.1-mini','cloud_enabled':True,'cost_limit_usd':1,
            'members':[{'id':'one','model':'missing-local'},{'id':'two','model':'openai:gpt-4.1-mini'}]}
        results=[json.loads(line.removeprefix('data: ')) async for line in council.stream_council_chat(None,None,config,[{'role':'user','content':'fixture'}],'conv')]
        assert 'missing-local' in called
        assert any(r['type']=='council_error' and r['member_id']=='one' for r in results)
        assert results[-1]['usage']['gen_tokens']>0
        assert results[-1]['type']=='council_complete'
        assert saved[-1][2]['council_host']
    asyncio.run(run())
