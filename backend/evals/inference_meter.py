"""Isolated evaluation-only Ollama proxy: count and bound every inference call.

Legacy and persistent runs share this boundary. Metadata/model-loading requests
are forwarded but are not counted as inference. No production config is written.
"""
import asyncio
import json
import socket
import time

import httpx
import uvicorn
from fastapi import FastAPI,Request
from fastapi.responses import Response,StreamingResponse

from context_policy import resolve,thinking_options


class InferenceMeter:
    def __init__(self,upstream,settings,model,log,*,enforce_profile=False):
        self.upstream=upstream.rstrip('/');self.settings=settings;self.model=model;self.log=log
        self.calls=0;self.started=time.monotonic();self.request='';self.records=[]
        self.enforce_profile=enforce_profile
        self.app=FastAPI()
        self.app.add_api_route('/{path:path}',self.forward,methods=['GET','POST'])

    def begin(self,request):
        self.calls=0;self.started=time.monotonic();self.request=request

    async def forward(self,request:Request,path:str):
        role='builder'
        if path.startswith('roles/'):
            _,role,path=path.split('/',2)
        raw=await request.body();body=json.loads(raw) if raw else None
        inference=bool(body and (path.endswith('chat') or path.endswith('chat/completions') or path.endswith('generate') and body.get('prompt')))
        if inference:
            if self.calls>=self.settings['daedalus_model_calls'] or time.monotonic()-self.started>=self.settings['daedalus_job_seconds']:
                return Response(json.dumps({'error':'Evaluation execution allowance exhausted'}),status_code=429,media_type='application/json')
            requested=body.get('model','').removeprefix('ollama/').removeprefix('ollama_chat/')
            if requested not in {self.model,self.model+':latest'}:
                return Response(json.dumps({'error':'Evaluation model differs from recorded model'}),status_code=400,media_type='application/json')
            self.calls+=1
            policy=resolve(role,self.settings)
            mode=body.get('think','provider_default')
            if self.enforce_profile and path.startswith('api/'):
                body['options']={**body.get('options',{}),'num_ctx':policy.num_ctx,'num_predict':policy.num_predict}
                thinking,mode=thinking_options(role,{'settings':self.settings,'policy_version':3,'model':self.model,
                    'disable_thinking':True},self.details)
                body.update(thinking)
            elif self.enforce_profile:
                body['max_tokens']=policy.num_predict;mode='provider_request'
            self.records.append({'request':self.request,'call':self.calls,'role':role,'path':path,'model':body.get('model'),
                'context':body.get('options',{}),'max_tokens':body.get('max_tokens'),'thinking':mode,'started':time.time()})
            self.log.write_text(json.dumps(self.records,indent=2))
            raw=json.dumps(body).encode()
        remaining=max(1,self.settings['daedalus_job_seconds']-(time.monotonic()-self.started)) if inference else 60
        upstream=await self.client.send(self.client.build_request(request.method,self.upstream+'/'+path,
            params=request.query_params,content=raw,headers={'content-type':'application/json'},timeout=remaining),stream=True)
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes(): yield chunk
            finally: await upstream.aclose()
        return StreamingResponse(chunks(),status_code=upstream.status_code,media_type=upstream.headers.get('content-type'))

    async def __aenter__(self):
        self.client=httpx.AsyncClient()
        response=await self.client.post(self.upstream+'/api/show',json={'model':self.model});response.raise_for_status()
        self.details=response.json()
        if self.details.get('remote_host') or self.details.get('remote_model'): raise ValueError('Remote inference is not allowed')
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen()
        self.url='http://127.0.0.1:'+str(self.socket.getsockname()[1])
        self.server=uvicorn.Server(uvicorn.Config(self.app,log_level='error'))
        self.task=asyncio.create_task(self.server.serve(sockets=[self.socket]))
        while not self.server.started:
            if self.task.done(): await self.task
            await asyncio.sleep(.01)
        return self

    async def __aexit__(self,*args):
        self.server.should_exit=True
        await self.task;await self.client.aclose()
