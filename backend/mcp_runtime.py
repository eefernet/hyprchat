"""Official MCP SDK sessions owned by long-lived tasks with bounded requests.

A transport failure is surfaced, never replayed automatically: tools may have
external effects. A later explicit request creates a fresh session.
"""
import asyncio
import hashlib
import json
import os
import sys
import weakref
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

import connector_http
import database as db

_POOLS=weakref.WeakKeyDictionary()


class Session(ClientSession):
    async def send_request(self, request, result_type, **kwargs):
        # SDK v1 exposes requests through this method; attach protocol
        # cancellation while preserving its timeout and response validation.
        request_id=self._request_id
        try:return await super().send_request(request,result_type,**kwargs)
        except asyncio.CancelledError:
            async def send_cancel():
                try:
                    async with asyncio.timeout(2):
                        await self.send_notification(types.ClientNotification(types.CancelledNotification(
                            method='notifications/cancelled',params=types.CancelledNotificationParams(requestId=request_id,reason='User cancelled request'))))
                except Exception:pass
            await asyncio.shield(send_cancel())
            raise


class Connection:
    def __init__(self,http,server):
        self.http=http;self.server=server;self.queue=asyncio.Queue(maxsize=32);self.active=None
        self.task=asyncio.create_task(self.run())
    async def run(self):
        from connectors import ConnectorError, expand_placeholders
        current=None
        try:
            async with AsyncExitStack() as stack:
                transport=self.server.get('transport') or 'stdio'
                if transport=='stdio':
                    command=self.server.get('command') or ''
                    if not command:raise ConnectorError('MCP stdio server requires command')
                    env=expand_placeholders(self.server.get('env') or {},required=True,plain_names=True)
                    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('mcp_stdio_guard.py')),command,*map(str,self.server.get('args') or [])],env=env)
                    errlog=stack.enter_context(open(os.devnull,'w'))
                    read,write=await stack.enter_async_context(stdio_client(params,errlog=errlog))
                elif transport in ('http','streamable_http','sse'):
                    url=self.server.get('url') or ''
                    headers=expand_placeholders(self.server.get('headers') or {},required=True)
                    if transport=='sse':
                        def factory(**kwargs):
                            return connector_http.client(self.http,url,bool(self.server.get('allow_private')),kwargs.get('headers') or headers)
                        read,write=await stack.enter_async_context(sse_client(url,headers=headers,httpx_client_factory=factory))
                    else:
                        http=await stack.enter_async_context(connector_http.client(self.http,url,bool(self.server.get('allow_private')),headers))
                        read,write,_=await stack.enter_async_context(streamable_http_client(url,http_client=http))
                else:raise ConnectorError('Unsupported MCP transport')
                session=await stack.enter_async_context(Session(read,write,read_timeout_seconds=timedelta(seconds=120)))
                async with asyncio.timeout(20):await session.initialize()
                while True:
                    try:current=await asyncio.wait_for(self.queue.get(),300)
                    except asyncio.TimeoutError:return
                    method,params,future=current
                    if future.cancelled():continue
                    async def execute():
                        if method=='tools/list':result=await session.list_tools(cursor=params.get('cursor'))
                        elif method=='tools/call':result=await session.call_tool(params['name'],params.get('arguments') or {})
                        else:raise ConnectorError('Unsupported MCP method')
                        encoded=result.model_dump_json(by_alias=True,exclude_none=True)
                        if len(encoded.encode())>connector_http.MAX_RESPONSE_BYTES:raise ConnectorError('MCP response exceeds 8 MB')
                        return json.loads(encoded)
                    self.active=asyncio.create_task(execute())
                    running=self.active
                    def cancelled(done):
                        if done.cancelled() and not running.done():running.cancel()
                    future.add_done_callback(cancelled)
                    try:
                        result=await self.active
                        if not future.done():future.set_result(result)
                    except asyncio.CancelledError:
                        if self.task.cancelling():raise
                        # A cancelled tool is uncertain; end its session and
                        # require an explicit next action to reconnect.
                        return
                    finally:self.active=None
                    current=None
        except asyncio.CancelledError:raise
        except Exception as exc:
            # Server responses and exception strings may include credentials.
            error=ConnectorError(f'MCP connection failed ({type(exc).__name__}); operation outcome may be uncertain. Review before retrying.')
            if current and not current[2].done():current[2].set_exception(error)
        finally:
            if current and not current[2].done():current[2].set_exception(ConnectorError('MCP session closed; review operation outcome before retrying'))
            while not self.queue.empty():
                _,_,future=self.queue.get_nowait()
                if not future.done():future.set_exception(ConnectorError('MCP session closed before dispatch'))


async def exchange(http,server,method,params=None):
    from connectors import ConnectorError
    loop=asyncio.get_running_loop();pool=_POOLS.setdefault(loop,{})
    key=(db.current_user_id(),server['id'],hashlib.sha256(json.dumps(server,sort_keys=True,default=str).encode()).hexdigest())
    for old in list(pool):
        if pool[old].task.done():pool.pop(old)
    connection=pool.get(key)
    if not connection:
        if len(pool)>=20:raise ConnectorError('MCP session limit reached; wait for an idle session to close')
        connection=pool[key]=Connection(http,server)
    future=loop.create_future()
    async with asyncio.timeout(150):
        await connection.queue.put((method,params or {},future))
        try:return await future
        finally:
            if not future.done():future.cancel()


async def close():
    pool=_POOLS.pop(asyncio.get_running_loop(),{})
    for connection in pool.values():connection.task.cancel()
    await asyncio.gather(*(c.task for c in pool.values()),return_exceptions=True)
