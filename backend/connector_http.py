"""Destination checks and bounded responses shared by MCP and OpenAPI."""
import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, response): self.response=response
    async def __aiter__(self):
        count=0
        if self.response.is_stream_consumed:
            if len(self.response.content)>MAX_RESPONSE_BYTES:raise ValueError('Connector response exceeds 8 MB')
            yield self.response.content
            return
        async for chunk in self.response.aiter_raw():
            count+=len(chunk)
            if count>MAX_RESPONSE_BYTES:raise ValueError('Connector response exceeds 8 MB')
            yield chunk
    async def aclose(self):await self.response.aclose()


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, client, allow_private, origin):
        self.client=client;self.allow_private=allow_private;self.origin=httpx.URL(origin)
    async def handle_async_request(self, request):
        from connectors import assert_url_allowed, ConnectorError
        await assert_url_allowed(str(request.url),allow_private=self.allow_private)
        # Credential-bearing connectors must not redirect to a different origin.
        if (request.url.scheme,request.url.host,request.url.port)!=(self.origin.scheme,self.origin.host,self.origin.port):
            raise ConnectorError('Connector redirected outside its configured origin')
        request.headers['Accept-Encoding']='identity'
        response=await self.client.send(request,stream=True,follow_redirects=False)
        length=response.headers.get('content-length')
        if length and int(length)>MAX_RESPONSE_BYTES:
            await response.aclose();raise ConnectorError('Connector response exceeds 8 MB')
        if response.headers.get('content-encoding','identity')!='identity':
            await response.aclose();raise ConnectorError('Compressed connector responses are not accepted')
        return httpx.Response(response.status_code,headers=response.headers,stream=LimitedStream(response),extensions=response.extensions)


def client(http, url, allow_private=False, headers=None):
    return httpx.AsyncClient(transport=GuardedTransport(http,allow_private,url),headers=headers,
        follow_redirects=True,max_redirects=3,timeout=httpx.Timeout(120,connect=15),trust_env=False)


async def request(http, method, url, *, allow_private=False, **kwargs):
    async with client(http,url,allow_private) as bounded:
        return await bounded.request(method,url,**kwargs)
