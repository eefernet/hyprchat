import asyncio
import time
from types import SimpleNamespace
import httpx
import pytest
from .optional_deps import HAS_CHROMADB


@pytest.mark.skipif(not HAS_CHROMADB,reason='real RAG module requires Chroma')
def test_keyword_results_arrive_despite_embedding_outage(monkeypatch):
    import rag,config
    async def run():
        keyword_started=asyncio.Event();cancelled=asyncio.Event()
        async def embed(_):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        async def keyword(*a,**k):
            keyword_started.set()
            return [{'text':'Exact keyword','kb_id':'kb','filename':'file','chunk_index':0}]
        monkeypatch.setattr(rag,'embed_single',embed)
        monkeypatch.setattr(rag.db,'kb_fts_search',keyword)
        monkeypatch.setattr(config,'RAG_RETRIEVAL_DEADLINE_SECONDS',.04)
        started=time.monotonic()
        results=await rag.hybrid_query(['kb'],'Exact')
        assert results[0]['text']=='Exact keyword'
        assert keyword_started.is_set() and cancelled.is_set()
        assert time.monotonic()-started < .3
    asyncio.run(run())


@pytest.mark.skipif(not HAS_CHROMADB,reason='real RAG module requires Chroma')
@pytest.mark.parametrize('code,body,fallback',[(503,'offline',False),(404,'{"error":"model not found"}',False),(404,'404 page not found',True),(200,'{}',False)])
def test_embedding_fallback_only_for_unsupported_endpoint(monkeypatch,code,body,fallback):
    import rag
    async def run():
        calls=[]
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self,*a): pass
            async def post(self,url,**kwargs):
                calls.append(url)
                return httpx.Response(200,json={'embedding':[1.]}) if url.endswith('/embeddings') else httpx.Response(code,text=body)
        monkeypatch.setattr(rag.httpx,'AsyncClient',lambda **kw:Client())
        result=await rag.embed_texts(['one'])
        assert len(calls)==(2 if fallback else 1)
        assert result==([[1.]] if fallback else [None])
    asyncio.run(run())


def test_provider_concurrency_and_queue_deadline():
    import search_runtime as runtime
    async def run():
        entered=0;peak=0;release=asyncio.Event()
        class Client:
            async def get(self,*a,**kw):
                nonlocal entered,peak
                entered+=1;peak=max(peak,entered)
                try: await release.wait();return SimpleNamespace(status_code=200)
                finally: entered-=1
        client=Client()
        occupied=[asyncio.create_task(runtime.provider_get(client,'fixture')) for _ in range(2)]
        await asyncio.sleep(.01)
        token=runtime.current_run.set(runtime.SearchRun(deadline=asyncio.get_running_loop().time()+.03,context_budget=100))
        try:
            with pytest.raises(TimeoutError): await runtime.provider_get(client,'blocked')
        finally: runtime.current_run.reset(token)
        assert peak==2
        release.set();await asyncio.gather(*occupied)
    asyncio.run(run())
