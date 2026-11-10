"""Real chat delivery and durable job routing with deterministic model streams."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import config
import database as db
import coder_jobs
from db import coder_jobs as store
from agents import chat
from main import ChatRequest
from agents.coder_dispatch import DispatchGuard, claims_start, may_correct


PROMPT = ('Add --loud: java -jar target/app.jar --loud Ada prints HELLO, ADA!. Preserve the default greeting, '
          'package the executable JAR and add tests.')
# Exact failed reply retained from the 2026-11-05 composer campaign.
CLAIM = ('Your `--loud` feature update is underway! 🛠️\n\n**What\'s being added:**\n'
         '- A `--loud` flag that uppercases both "HELLO" and the name → `HELLO, ADA!`\n'
         '- Default greeting (`Hello, world!`) preserved exactly as before\n'
         '- JUnit tests for the new loud mode (named + default) alongside existing tests\n'
         '- Executable JAR packaging via Maven with the `Main-Class` manifest entry intact\n\n'
         "The job controller will plan, implement, verify, and deliver. You'll see progress in the card above "
         "— once it's done I'll let you know so you can review and accept the final candidate.")


def action(name='start_coder_workflow', **args):
    return {'function': {'name': name, 'arguments': args or {'task': PROMPT, 'mode': 'edit_project', 'project_id': 'p'}}}


@pytest.mark.parametrize('prompt', [
    'Add a progress bar with Stop and Reset buttons.',
    'Create a web status dashboard.',
    'Can you build a CLI that explains errors with examples?',
    'Fix the parser without changing its public API.',
])
def test_feature_vocabulary_does_not_turn_changes_into_status_questions(prompt):
    assert may_correct(prompt)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS_CODER', 4)
    monkeypatch.setattr(config, 'CODER_MODEL', 'fixture')
    monkeypatch.setattr(config, 'PLANNING_MODEL', 'fixture')
    monkeypatch.setenv('DAEDALUS_ENGINE_URL', 'http://worker:8587')
    spawn = []
    monkeypatch.setattr(coder_jobs, 'spawn', lambda job: spawn.append(job))
    monkeypatch.setattr(db, 'get_model_configs', AsyncMock(return_value=[{
        'id': 'agent', 'name': 'Daedalus', 'tool_ids': ['codeagent'],
        'parameters': {'profile_type': 'agent'}, 'system_prompt': ''}]))

    async def initialize():
        await db.init_db()
        await db.create_conversation('c', model='fixture', model_config_id='agent', tool_ids=['codeagent'])
        await db.upsert_coding_project('p', 'Existing project', conversation_id='c', openhands_project_id='upload')

    async def run(responses, prompt=PROMPT, options=None):
        payloads = []
        def handle(request):
            if request.url.path == '/api/show':
                return httpx.Response(200, json={'capabilities':['completion', 'tools']})
            if request.url.path == '/api/tags':
                return httpx.Response(200, json={'models':[{'name':'fixture','digest':'fixture'}]})
            assert request.url.path == '/api/chat', request.url
            payloads.append(json.loads(request.content))
            response = responses[len(payloads)-1]
            message = {'role':'assistant','content':response} if isinstance(response, str) else response
            # Split prose from the terminal packet: no transient false progress may escape.
            packets = [{'message':message,'done':False}, {'message':{'content':''},'done':True,
                'done_reason':'stop','prompt_eval_count':12,'eval_count':4,'eval_duration':1000000}]
            return httpx.Response(200, text=''.join(json.dumps(p)+'\n' for p in packets))
        req = ChatRequest(conversation_id='c', model='fixture', persona_id='agent',
            messages=[{'role':'user','content':prompt}], tool_ids=['codeagent'], use_memories=False,
            think_budget=0)
        token = coder_jobs.REQUEST_OPTIONS.set({'project_id':'p', **(options or {})})
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                events = [json.loads(s[5:]) async for s in chat.chat_stream_generate(
                    req, http, SimpleNamespace(emit=AsyncMock()), {}, {})]
        finally:
            coder_jobs.REQUEST_OPTIONS.reset(token)
        conv = await db.get_conversation('c')
        return events, payloads, conv

    return initialize, run, spawn


@pytest.mark.parametrize('encoding', ['native', 'text', 'lost_response'])
def test_claim_recovers_once_through_real_controller_and_does_not_replay(harness, monkeypatch, encoding):
    initialize, run, spawn = harness
    if encoding == 'lost_response':
        original = chat.exec_tool
        async def lose(*args, **kwargs):
            await original(*args, **kwargs)
            raise httpx.ReadError('Response lost after controller commit')
        monkeypatch.setattr(chat, 'exec_tool', lose)
    response = ({'role':'assistant','content':CLAIM,'tool_calls':[action()]} if encoding != 'text' else
                '<tool_call>'+json.dumps({'name':'start_coder_workflow','arguments':action()['function']['arguments']})+'</tool_call>')
    async def scenario():
        await initialize()
        events, payloads, conv = await run([CLAIM, response])
        assert not [e for e in events if e['type'] == 'error']
        assert len(payloads) == 2 and len(spawn) == 1
        visible = ''.join(e.get('content','') for e in events if e['type']=='token')
        assert 'underway' not in visible and spawn[0] in visible and '**queued**' in visible
        user, assistant = conv['messages'][-2:]
        assert user['metadata']['daedalus_dispatch']['first_unverified_response'] == CLAIM
        assert user['metadata']['daedalus_dispatch']['correction_used']
        assert len(assistant['metadata']['usage_calls']) == 2 and assistant['metadata']['total_gen_tokens'] == 8
        job = await store.get(spawn[0])
        assert job['chat_request_key'] == str(user['id']) and job['origin_message_id'] == assistant['id']
        assert job['mode'] == 'edit_project' and job['project_id'] == 'p' and job['original_user_request'] == PROMPT
        assert job['calls_used'] == 0 and job['repair_round'] == 0
        # A reconnect and a direct duplicate tool reattach to the committed job.
        again, calls, _ = await run([])
        assert not calls and spawn[0] in ''.join(e.get('content','') for e in again)
        same = json.loads(await coder_jobs.route_tool('start_coder_workflow', {'task':'rephrased'}, 'c', None))
        assert same['workflow_id'] == job['id'] and len(spawn) == 1
    asyncio.run(scenario())


def test_repeated_false_claim_is_truthful_bounded_charged_and_durable(harness):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        events, payloads, conv = await run([CLAIM, CLAIM])
        assert len(payloads) == 2 and not spawn
        assert 'No Daedalus job was started' in conv['messages'][-1]['content']
        assert 'underway' not in ''.join(e.get('content','') for e in events)
        assert conv['messages'][-1]['metadata']['total_gen_tokens'] == 8
        _, calls, _ = await run([CLAIM])
        assert len(calls) == 1 and not spawn  # reconnect does not reset the correction ledger
    asyncio.run(scenario())


def test_no_correction_beyond_existing_chat_round_limit(harness, monkeypatch):
    initialize, run, spawn = harness
    monkeypatch.setattr(config, 'MAX_AGENT_ROUNDS_CODER', 1)
    async def scenario():
        await initialize()
        _, calls, conv = await run([CLAIM])
        assert len(calls) == 1 and not spawn
        assert 'No Daedalus job was started' in conv['messages'][-1]['content']
    asyncio.run(scenario())


@pytest.mark.parametrize('prompt', [
    'Which file handles the greeting? Just explain it.', 'What is the job status?', 'Stop.',
    'Create a plan only for adding --loud.', 'Explain this quoted request: "Add --loud".',
    'Create a plan for adding --loud.', 'Write an implementation plan for the Java changes.',
    '> Add --loud\nWhat does this instruction mean?', 'Do not edit anything; describe the change.',
])
def test_false_claim_on_read_only_or_quoted_request_never_dispatches(harness, prompt):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        _, calls, conv = await run([CLAIM], prompt)
        assert len(calls) == 1 and not spawn
        assert 'No Daedalus job was started' in conv['messages'][-1]['content']
    asyncio.run(scenario())


@pytest.mark.parametrize('reply', ['No job is underway.', 'I have not started a job.',
    'The phrase "your update is underway" describes progress.', '> The job has started.\nThis is a quote.',
    'Which output format do you want?'])
def test_non_claiming_neighbors_are_unchanged(harness, reply):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        _, calls, conv = await run([reply], 'Explain the wording.')
        assert len(calls) == 1 and not spawn and conv['messages'][-1]['content'] == reply
    asyncio.run(scenario())


def test_request_identity_is_frozen_when_a_new_message_arrives(harness, monkeypatch):
    initialize, run, spawn = harness
    original = chat.exec_tool
    async def new_message(*args, **kwargs):
        await db.add_message('c', 'user', 'Only explain the previous request.')
        return await original(*args, **kwargs)
    monkeypatch.setattr(chat, 'exec_tool', new_message)
    async def scenario():
        await initialize()
        _, _, conv = await run([{'role':'assistant','content':'','tool_calls':[action()]}])
        user = next(m for m in conv['messages'] if m['role']=='user')
        job = await store.get(spawn[0])
        assert job['chat_request_key']==str(user['id']) and job['original_user_request']==PROMPT
    asyncio.run(scenario())


def test_atomic_receipt_owner_project_and_mode_boundaries(harness):
    initialize, _, spawn = harness
    async def scenario():
        await initialize()
        user = await db.add_message('c','user',PROMPT)
        origin = await db.add_message('c','assistant','')
        key = str(user)
        job = await coder_jobs.create('c',PROMPT,'edit_project','p',key=key,original_request=PROMPT,
                                      chat_request_key=key,origin_message_id=origin)
        guard = DispatchGuard('c',user,PROMPT,project_id='p')
        assert (await guard.receipt())['id']==job['id']
        for options in [{'project_id':'other'}, {'new_project':True}]:
            with pytest.raises(ValueError, match='no additional job'):
                await DispatchGuard('c',user,PROMPT,**options).receipt()
        token = db.set_current_user_id('foreign')
        try:
            assert await guard.receipt() is None
            with pytest.raises(LookupError):
                await store.record_chat_dispatch_attempt('c',user,CLAIM,allow_correction=True)
        finally:
            db.reset_current_user_id(token)
        assert len(spawn)==1
    asyncio.run(scenario())


def test_failure_between_commit_and_spawn_retains_atomic_chat_receipt(harness, monkeypatch):
    initialize, run, spawn = harness
    def interrupted(job):
        spawn.append(job)
        raise RuntimeError('Transport interrupted immediately after commit')
    monkeypatch.setattr(coder_jobs, 'spawn', interrupted)
    async def scenario():
        await initialize()
        _, calls, conv = await run([{'role':'assistant','content':CLAIM,'tool_calls':[action()]}])
        assert len(calls)==1 and len(spawn)==1 and spawn[0] in conv['messages'][-1]['content']
        job = await store.get(spawn[0])
        assert job['chat_request_key']==str(conv['messages'][-2]['id'])
        assert job['state']=='queued'  # the normal reconciler can resume this exact row
    asyncio.run(scenario())


def test_project_question_remains_read_only(harness):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        before = await db.get_coding_project('p')
        _, calls, conv = await run([{'role':'assistant','content':'','tool_calls':[
            action('ask_project',question='Which file handles the greeting?',project_id='p',mode='edit_project')]}],
            'Which file handles the greeting? Just explain it.')
        job = await store.get(spawn[0])
        assert job['mode']=='ask_uploaded_project' and len(calls)==1
        assert 'Project question' in conv['messages'][-1]['content']
        after = await db.get_coding_project('p')
        for key in ('accepted_job_id','accepted_revision_id','working_job_id','working_revision_id'):
            assert before.get(key)==after.get(key)
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', [False, True])
def test_status_and_stop_preserve_original_job_without_new_dispatch(harness, stop):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        original = await coder_jobs.create('c','Existing request','edit_project','p',key='previous')
        reply = 'The job was cancelled.' if stop else 'The job is underway.'
        name = 'cancel_coder_workflow' if stop else 'get_coder_workflow'
        _, calls, conv = await run([{'role':'assistant','content':'','tool_calls':[
            action(name,workflow_id=original['id'])]}, reply], 'Stop.' if stop else 'What is the job status?')
        assert len(spawn)==1 and len(calls)==2
        saved = await store.get(original['id'])
        assert saved['state']==('cancelled' if stop else 'queued')
        assert saved['calls_used']==0 and saved['repair_round']==0
        if not stop:
            assert original['id'] in conv['messages'][-1]['content']
            assert 'No new job was started' in conv['messages'][-1]['content']
    asyncio.run(scenario())


@pytest.mark.parametrize('encoding', ['native', 'text', 'lost_response'])
def test_copied_queued_receipt_cannot_replace_reset_edit(harness, monkeypatch, encoding):
    initialize, run, spawn = harness
    prompt = 'Add a Reset button that clears the inputs and result. Preserve calculation and validation.'
    if encoding == 'lost_response':
        execute = chat.exec_tool
        async def lose(*args, **kwargs):
            await execute(*args, **kwargs)
            raise httpx.ReadError('Response lost after committing the edit')
        monkeypatch.setattr(chat, 'exec_tool', lose)
    tool = action(task=prompt, mode='edit_project', project_id='p')
    response = ({'role':'assistant','content':'','tool_calls':[tool]} if encoding != 'text' else
                '<tool_call>'+json.dumps({'name':'start_coder_workflow','arguments':tool['function']['arguments']})+'</tool_call>')
    async def scenario():
        await initialize()
        old = await coder_jobs.create('c', 'Earlier calculator build', 'edit_project', 'p', key='earlier')
        copied = DispatchGuard.describe(old)
        revision = 'a' * 40
        await store.save(old['id'], state='completed', revision_id=revision, acceptance={'accepted':True})
        connection = await db.get_db()
        try:
            await connection.execute('UPDATE coding_projects SET accepted_job_id=?,accepted_revision_id=?,working_job_id=?,working_revision_id=? WHERE id=?',
                                     (old['id'],revision,old['id'],revision,'p'))
            await connection.commit()
        finally:
            await connection.close()
        before = await db.get_coding_project('p')
        events, calls, conv = await run([copied, response], prompt)
        assert len(calls) == 2 and len(spawn) == 2
        visible = ''.join(e.get('content','') for e in events if e['type']=='token')
        assert old['id'] not in visible and spawn[-1] in visible
        job = await store.get(spawn[-1])
        user = conv['messages'][-2]
        assert job['original_user_request'] == prompt and job['chat_request_key'] == str(user['id'])
        assert job['source_job_id'] == old['id'] and job['source_revision_id'] == revision
        assert job['mode'] == 'edit_project' and job['project_id'] == 'p'
        assert user['metadata']['daedalus_dispatch']['first_unverified_response'] == copied
        after = await db.get_coding_project('p')
        assert (after['accepted_job_id'],after['accepted_revision_id']) == (before['accepted_job_id'],before['accepted_revision_id'])
        _, again, _ = await run([], prompt)
        assert not again and len(spawn) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('identity', ['cw3-'+'f'*24, 'invented-job'])
def test_fabricated_canonical_receipt_is_not_visible_progress(harness, identity):
    initialize, run, spawn = harness
    receipt = f'Daedalus job `{identity}` is saved with status **queued**.'
    async def scenario():
        await initialize()
        events, calls, conv = await run([receipt, receipt])
        assert len(calls) == 2 and not spawn
        assert 'No Daedalus job was started' in conv['messages'][-1]['content']
        assert identity not in ''.join(e.get('content','') for e in events if e['type']=='token')
    asyncio.run(scenario())


def test_canonical_status_receipt_reports_requested_historical_job(harness):
    initialize, run, spawn = harness
    async def scenario():
        await initialize()
        old = await coder_jobs.create('c','Older build','edit_project','p',key='older')
        copied = DispatchGuard.describe(old)
        await store.save(old['id'],state='completed')
        latest = await coder_jobs.create('c','Newer build','edit_project','p',key='newer')
        _, calls, conv = await run([copied], f'What is the status of {old["id"]}?')
        reply = conv['messages'][-1]['content']
        assert len(calls) == 1 and len(spawn) == 2
        assert old['id'] in reply and latest['id'] not in reply
        assert '**completed**' in reply and '**queued**' not in reply
        assert 'No new job was started' in reply
    asyncio.run(scenario())


@pytest.mark.parametrize('wrapper', ['"{}"', '```\n{}\n```', '`{}`', '> {}'])
def test_quoted_canonical_receipts_do_not_claim_dispatch(wrapper):
    receipt = 'Daedalus job `cw3-'+'f'*24+'` is saved with status **queued**.'
    assert claims_start(receipt)
    # Inline prose code uses no nested backticks around its identifier.
    quoted = receipt.replace('`','') if wrapper == '`{}`' else receipt
    assert not claims_start(wrapper.format(quoted))


def test_current_receipt_does_not_trust_the_reported_id_or_state(harness):
    initialize, _, spawn = harness
    async def scenario():
        await initialize()
        message = await db.add_message('c','user',PROMPT)
        job = await coder_jobs.create('c',PROMPT,'edit_project','p',key=str(message),
            original_request=PROMPT,chat_request_key=str(message))
        await store.save(job['id'],state='completed')
        guard = DispatchGuard('c',message,PROMPT,project_id='p')
        reply, retry = await guard.review('Daedalus job `invented` is saved with status **queued**.',can_retry=True)
        assert not retry and job['id'] in reply and '**completed**' in reply and 'invented' not in reply
        assert len(spawn) == 1
        # Corrupt the stored receipt deliberately; normal saves correctly reject
        # edits to terminal workflows.
        connection = await db.get_db()
        try:
            await connection.execute("UPDATE coder_workflows SET job_json=json_set(job_json,'$.mode','build_from_prompt') WHERE id=?", (job['id'],))
            await connection.commit()
        finally:
            await connection.close()
        with pytest.raises(ValueError,match='mode differs'):
            await guard.receipt()
    asyncio.run(scenario())
