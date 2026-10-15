import asyncio
import json
import sys
import httpx
import pytest
import connectors
import mcp_runtime
import connector_http


def test_official_stdio_session_reuse_and_reconnect(tmp_path):
    fixture=tmp_path/'server.py'
    fixture.write_text('''from mcp.server.fastmcp import FastMCP
mcp=FastMCP('fixture')
count=0
@mcp.tool()
def increment() -> int:
    global count
    count+=1
    return count
mcp.run(transport='stdio')
''')
    async def run():
        async with httpx.AsyncClient() as http:
            server={'id':'fixture','transport':'stdio','command':sys.executable,'args':[str(fixture)]}
            try:
                listing=await connectors.mcp_exchange(http,server,'tools/list')
                assert listing['tools'][0]['name']=='increment'
                first=await connectors.mcp_exchange(http,server,'tools/call',{'name':'increment'})
                second=await connectors.mcp_exchange(http,server,'tools/call',{'name':'increment'})
                assert first['content'][0]['text']=='1' and second['content'][0]['text']=='2'
                await mcp_runtime.close()
                fresh=await connectors.mcp_exchange(http,server,'tools/call',{'name':'increment'})
                assert fresh['content'][0]['text']=='1'
            finally:await mcp_runtime.close()
    asyncio.run(run())


def test_redirect_restrictions_and_oversized_response(monkeypatch):
    checked=[]
    async def check(url,allow_private=False):checked.append(url)
    monkeypatch.setattr(connectors,'assert_url_allowed',check)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(307,headers={'location':'http://127.0.0.1/private'}))) as http:
            with pytest.raises(connectors.ConnectorError,match='origin'):
                await connector_http.request(http,'GET','https://public.example/api')
        assert 'http://127.0.0.1/private' in checked
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-length':str(connector_http.MAX_RESPONSE_BYTES+1)}))) as http:
            with pytest.raises(connectors.ConnectorError,match='8 MB'):
                await connector_http.request(http,'GET','https://public.example/api')
    asyncio.run(run())


def test_discovery_follows_pagination_without_changing_tool_ids(monkeypatch):
    async def run():
        pages=[];saved=[]
        async def get(_):return {'id':'server','transport':'stdio'}
        async def exchange(http,server,method,params):
            pages.append(params)
            return {'tools':[{'name':'second'}]} if params.get('cursor') else {'tools':[{'name':'first'}],'nextCursor':'next'}
        async def replace(kind,server,tools):saved.extend(tools)
        async def update(*args,**kwargs):pass
        monkeypatch.setattr(connectors.db,'get_mcp_server',get)
        monkeypatch.setattr(connectors.db,'replace_connector_tools',replace)
        monkeypatch.setattr(connectors.db,'update_mcp_server',update)
        monkeypatch.setattr(connectors,'mcp_exchange',exchange)
        result=await connectors.discover_mcp_server(None,'server')
        assert result['tool_count']==2 and pages==[{}, {'cursor':'next'}]
        assert saved[0]['id']==connectors.mcp_tool_to_connector({'id':'server'},{'name':'first'})['id']
    asyncio.run(run())


def test_stdio_cancellation_reaches_server_and_next_call_reconnects(tmp_path):
    fixture=tmp_path/'cancellable.py'
    marker=tmp_path/'started';stopped=tmp_path/'stopped'
    fixture.write_text(f'''import asyncio
from pathlib import Path
from mcp.server.fastmcp import FastMCP
mcp=FastMCP('cancel-fixture')
@mcp.tool()
async def wait_for_stop() -> str:
    Path({str(marker)!r}).write_text('started')
    try:
        await asyncio.sleep(60)
    finally:
        Path({str(stopped)!r}).write_text('stopped')
    return 'finished'
mcp.run(transport='stdio')
''')
    async def run():
        async with httpx.AsyncClient() as http:
            server={'id':'cancel-fixture','transport':'stdio','command':sys.executable,'args':[str(fixture)]}
            try:
                work=asyncio.create_task(connectors.mcp_exchange(http,server,'tools/call',{'name':'wait_for_stop'}))
                async with asyncio.timeout(10):
                    while not marker.exists():await asyncio.sleep(.02)
                work.cancel()
                with pytest.raises(asyncio.CancelledError):await work
                async with asyncio.timeout(5):
                    while not stopped.exists():await asyncio.sleep(.02)
                    while any(not c.task.done() for p in mcp_runtime._POOLS.values() for c in p.values()):await asyncio.sleep(.02)
                result=await connectors.mcp_exchange(http,server,'tools/list')
                assert result['tools'][0]['name']=='wait_for_stop'
            finally:await mcp_runtime.close()
    asyncio.run(run())


def test_http_streaming_tool_result_and_reconnect(monkeypatch):
    async def allowed(*a,**k):pass
    monkeypatch.setattr(connectors,'assert_url_allowed',allowed)
    initializes=[]
    class Stream(httpx.AsyncByteStream):
        def __init__(self,payload):self.payload=payload
        async def __aiter__(self):
            data=('event: message\ndata: '+json.dumps(self.payload)+'\n\n').encode()
            for offset in range(0,len(data),17):
                await asyncio.sleep(0)
                yield data[offset:offset+17]
    def handler(request):
        if request.method in {'GET','DELETE'}:return httpx.Response(405)
        body=json.loads(request.content)
        if body['method']=='notifications/initialized':return httpx.Response(202)
        if body['method']=='initialize':
            initializes.append(1)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':{'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}},headers={'mcp-session-id':'fixture-session'})
        assert request.headers['mcp-session-id']=='fixture-session'
        if body['method']=='tools/list':
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':{'tools':[{'name':'stream','inputSchema':{'type':'object'}}]}})
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream({'jsonrpc':'2.0','id':body['id'],'result':{'content':[{'type':'text','text':'streamed result'}],'isError':False}}))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            server={'id':'stream-fixture','transport':'streamable_http','url':'https://fixture.example/mcp'}
            try:
                for _ in range(2):
                    result=await connectors.mcp_exchange(http,server,'tools/call',{'name':'stream'})
                    assert result['content'][0]['text']=='streamed result'
                    await mcp_runtime.close()
                assert len(initializes)==2
            finally:await mcp_runtime.close()
    asyncio.run(run())
