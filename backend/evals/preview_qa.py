"""Loopback integration fixture for the actual preview routes and React client."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import httpx
import database as db
from db import coder_jobs as jobs
from context_policy import DEFAULTS
from coder_worker_runtime import WorkerStore, operation_repo
# Load the actual router without importing unrelated RAG/email route dependencies.
import types
package=types.ModuleType('preview_qa_routes');package.__path__=[str(Path(__file__).resolve().parents[1]/'routes')]
sys.modules['preview_qa_routes']=package
from preview_qa_routes import coder_preview
from preview_qa_routes.context import configure_context
ROOT=Path('/opt/daedalus-openhands159-20261028')
QA=ROOT/'preview-qa';QA.mkdir(exist_ok=True)
FIXTURE={}

@asynccontextmanager
async def lifespan(app):
    db.DATABASE_PATH=str(QA/'jobs.db');await db.init_db()
    identity='preview-'+uuid.uuid4().hex[:10]
    await db.create_conversation(identity,model='unused')
    job=await jobs.create(identity,'Preview fixture','build_from_prompt','','unused',identity,engine='openhands159',policy_version=7)
    ledger=WorkerStore(ROOT/'state')
    payload={'settings':DEFAULTS,'seconds_remaining':120,'calls_remaining':0,'engine':'openhands159'}
    operation_id='preview-fixture-'+uuid.uuid4().hex
    ledger.create(operation_id,job['id'],'inspect',payload)
    root=ROOT/'state/jobs'/job['id']/'workspace';root.mkdir(parents=True)
    (root/'index.html').write_text('<!doctype html><html><body style="font:32px sans-serif"><h1>Preview isolation test</h1><button style="font:32px sans-serif" onclick="count.textContent=Number(count.textContent)+1">Increment</button> <output id="count">0</output></body></html>')
    repository=operation_repo(ledger,ledger.get(operation_id));snapshot=repository.snapshot('Preview test')
    ledger.update(operation_id,status='succeeded',started=1,ended=2,result={'snapshot':snapshot})
    FIXTURE.update(id=job['id'],engine='openhands159',revision_id=snapshot['revision'],candidate_artifact={'metadata':{'revision_id':snapshot['revision']}})
    await jobs.save(job['id'],state='ready_for_review',revision_id=snapshot['revision'],candidate_artifact=FIXTURE['candidate_artifact'])
    os.environ['DAEDALUS_ENGINE_URL']='http://127.0.0.1:8587'
    async with httpx.AsyncClient() as http:
        configure_context(http=http,request_user_id=lambda request:request.query_params.get('user_id','default'),
            request_session_token=lambda request:request.query_params.get('session',''))
        yield
app=FastAPI(lifespan=lifespan)
app.include_router(coder_preview.router)
@app.get('/qa-fixture')
def fixture():return FIXTURE
app.mount('/',StaticFiles(directory=str(QA/'bundle'),html=True),name='qa')
