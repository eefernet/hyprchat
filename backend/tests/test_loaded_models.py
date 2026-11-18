"""The owner's limit on models loaded at once (loaded_models.py, Settings → Model & Generation).

Checked on the homelab 2026-11-16 before this was built: Ollama's own limit there is 3 and it unloads by
memory, so the setting is HyprChat's own: unload the least recently used reply models before a chat loads
its own. Fully offline: Ollama is a fixture, and one case runs the real chat loop.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import chat_payload
import config
import database as db
import loaded_models
import model_limits
import reasoning
from agents import chat
from events import EventBus
from main import ChatRequest


def entry(name, minute, family='qwen3', size=20_000_000_000, vram=None, context=65536):
    return {'name': name, 'model': name, 'size': size, 'size_vram': size if vram is None else vram,
            'context_length': context, 'details': {'family': family, 'families': [family], 'parameter_size': '27B'},
            'expires_at': f'2026-11-16T23:{minute:02d}:24.494123456-07:00'}


@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama that holds models and unloads them when asked."""
    monkeypatch.setattr(config, 'WORKSPACE_MODEL', 'qwen3.5:4b')
    monkeypatch.setattr(config, 'QUICK_SEARCH_TRIAGE_MODEL', '')
    monkeypatch.setattr(loaded_models, '_STEP', 0.01)
    state = SimpleNamespace(models=[], unloads=[], busy=set(), ps_status=200, requests=[])

    def handle(request):
        state.requests.append(request.url.path)
        if request.url.path == '/api/ps':
            return httpx.Response(state.ps_status, json={'models': state.models})
        if request.url.path == '/api/generate':
            body = json.loads(request.content)
            assert body == {'model': body['model'], 'keep_alive': 0}      # an unload must never load or generate
            state.unloads.append(body['model'])
            if body['model'] not in state.busy:
                state.models = [m for m in state.models if m['name'] != body['model']]
            return httpx.Response(200, json={'done': True, 'done_reason': 'unload'})
        raise AssertionError(request.url)

    async def call(function, *args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return await function(http, *args, **kwargs)

    return call, state


# ── The setting ────────────────────────────────────────────────────────────────

def test_the_setting_is_off_by_default_and_bounded(monkeypatch):
    monkeypatch.setattr(config, 'CHAT_MAX_LOADED_MODELS', '0')
    assert loaded_models.setting() == 0
    assert [loaded_models.setting(v) for v in (1, '3', 8, 99, -4, 'many')] == [1, 3, 8, 8, 0, 0]


def test_a_name_with_and_without_its_tag_is_one_model():
    assert loaded_models.same('nomic-embed-text', 'nomic-embed-text:latest')
    assert loaded_models.same('hf.co/unsloth/Qwen3:Q4_K_M', 'ollama:hf.co/unsloth/Qwen3:Q4_K_M')
    assert not loaded_models.same('qwen3.8:27b', 'qwen3.8:4b') and not loaded_models.same('', '')


# ── Which models go ────────────────────────────────────────────────────────────

def listing(*entries):
    return loaded_models.rows({'models': list(entries)}, {'qwen3.5:4b', 'nomic-embed-text:latest'})


def test_the_least_recently_used_model_is_unloaded_first():
    loaded = listing(entry('new:27b', 40), entry('old:27b', 10), entry('middle:27b', 25))
    assert loaded_models.victims(loaded, 'asked:27b', 3) == ['old:27b']
    assert loaded_models.victims(loaded, 'asked:27b', 2) == ['old:27b', 'middle:27b']
    assert loaded_models.victims(loaded, 'asked:27b', 1) == ['old:27b', 'middle:27b', 'new:27b']


def test_nothing_is_unloaded_while_the_limit_is_not_reached():
    loaded = listing(entry('a:27b', 10), entry('b:27b', 20))
    assert loaded_models.victims(loaded, 'c:27b', 3) == []
    assert loaded_models.victims(loaded, 'a:27b', 2) == []          # the asked-for model is one of the two
    assert loaded_models.victims(loaded, 'c:27b', 0) == []          # off


def test_a_model_that_is_already_loaded_still_counts_as_one_of_the_limit():
    loaded = listing(entry('a:27b', 10), entry('b:27b', 20), entry('c:27b', 30))
    assert loaded_models.victims(loaded, 'a:27b', 2) == ['b:27b']
    assert loaded_models.victims(loaded, 'a:27b', 1) == ['b:27b', 'c:27b']


def test_helper_and_embedding_models_are_never_counted_or_unloaded():
    loaded = listing(entry('nomic-embed-text:latest', 1, family='nomic-bert', size=300_000_000),
                     entry('qwen3.5:4b', 2, size=4_000_000_000),
                     entry('bge-m3:latest', 3, family='bert', size=1_200_000_000),
                     entry('chat:27b', 30))
    assert [row['name'] for row in loaded if row['helper']] == ['nomic-embed-text:latest', 'qwen3.5:4b', 'bge-m3:latest']
    assert loaded_models.victims(loaded, 'other:27b', 1) == ['chat:27b']
    assert loaded_models.victims(loaded, 'other:27b', 2) == []


def test_the_workspace_model_can_be_the_chat_model():
    loaded = listing(entry('chat:27b', 30))
    assert loaded_models.victims(loaded, 'qwen3.5:4b', 1) == ['chat:27b']


def test_rows_report_memory_and_the_split_between_gpu_and_cpu():
    row = listing(entry('spilled:27b', 5, size=20_000_000_000, vram=18_400_000_000, context=65536))[0]
    assert row == {'name': 'spilled:27b', 'size_bytes': 20_000_000_000, 'gpu_bytes': 18_400_000_000, 'context': 65536,
                   'parameters': '27B', 'expires_at': '2026-11-16T23:05:24.494123456-07:00', 'helper': False}


def test_an_expiry_ollama_writes_with_nanoseconds_or_a_z_is_read():
    earlier = loaded_models._when('2026-11-16T23:05:24.494123456-07:00')
    later = loaded_models._when('2026-11-17T06:05:25Z')
    assert later - earlier == pytest.approx(0.505877, abs=1e-3)
    assert loaded_models._when('') == float('inf') and loaded_models._when('soon') == float('inf')


# ── Making room ────────────────────────────────────────────────────────────────

def test_making_room_unloads_and_waits_for_the_models_to_leave(ollama):
    call, state = ollama
    state.models = [entry('old:27b', 10), entry('new:27b', 30), entry('nomic-embed-text:latest', 1, family='nomic-bert')]
    assert asyncio.run(call(loaded_models.make_room, 'asked:27b', 2)) == ['old:27b']
    assert state.unloads == ['old:27b']
    assert [m['name'] for m in state.models] == ['new:27b', 'nomic-embed-text:latest']


def test_making_room_does_nothing_when_off_or_under_the_limit(ollama):
    call, state = ollama
    state.models = [entry('a:27b', 10)]
    assert asyncio.run(call(loaded_models.make_room, 'b:27b', 0)) == []
    assert state.requests == []                                    # off: Ollama is not even asked
    assert asyncio.run(call(loaded_models.make_room, 'b:27b', 2)) == []
    assert state.unloads == []


def test_a_busy_model_does_not_hold_the_reply_for_long(ollama):
    call, state = ollama
    state.models, state.busy = [entry('busy:27b', 10)], {'busy:27b'}
    assert asyncio.run(call(loaded_models.make_room, 'asked:27b', 1, wait=0.05)) == ['busy:27b']
    assert [m['name'] for m in state.models] == ['busy:27b']       # it finishes its own request first


def test_a_failed_listing_never_fails_the_reply(ollama):
    call, state = ollama
    state.ps_status = 500
    assert asyncio.run(call(loaded_models.make_room, 'asked:27b', 1)) == []
    assert state.unloads == []


def test_status_lists_what_is_loaded_and_says_when_ollama_cannot_be_reached(ollama, monkeypatch):
    call, state = ollama
    monkeypatch.setattr(config, 'CHAT_MAX_LOADED_MODELS', 2)
    state.models = [entry('chat:27b', 30), entry('qwen3.5:4b', 2, size=4_000_000_000)]
    status = asyncio.run(call(loaded_models.status))
    assert (status['limit'], status['counted'], status['reachable']) == (2, 1, True)
    assert [row['name'] for row in status['models']] == ['qwen3.5:4b', 'chat:27b']
    state.ps_status = 502
    status = asyncio.run(call(loaded_models.status))
    assert status['reachable'] is False and status['models'] == [] and status['error']


# ── In the chat loop ───────────────────────────────────────────────────────────

@pytest.fixture
def chat_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'chat.db'))
    monkeypatch.setattr(chat, '_suggest_workspace_memories', AsyncMock())
    monkeypatch.setattr(config, 'WORKSPACE_MODEL', 'qwen3.5:4b')
    monkeypatch.setattr(loaded_models, '_STEP', 0.01)
    reasoning._cache.clear()
    model_limits._cache.clear()
    chat_payload.forget_all()
    state = SimpleNamespace(order=[], models=[entry('other:27b', 10), entry('qwen3.5:4b', 2)])

    def handle(request):
        path = request.url.path
        if path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion'], 'model_info': {}})
        if path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture', 'digest': 'fixture'}]})
        if path == '/api/ps':
            return httpx.Response(200, json={'models': state.models})
        if path == '/api/generate':
            name = json.loads(request.content)['model']
            state.order.append(f'unload {name}')
            state.models = [m for m in state.models if m['name'] != name]
            return httpx.Response(200, json={'done': True})
        assert path == '/api/chat', request.url
        state.order.append('chat')
        return httpx.Response(200, text=''.join(json.dumps(item) + '\n' for item in (
            {'message': {'role': 'assistant', 'content': 'Hello there.'}, 'done': False},
            {'message': {'content': ''}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 900,
             'eval_count': 30, 'eval_duration': 1000000})))

    async def run(model='fixture'):
        await db.init_db()
        if not await db.get_conversation('c'):
            await db.create_conversation('c', model='fixture')
        req = ChatRequest(conversation_id='c', model=model, messages=[{'role': 'user', 'content': 'Hello'}],
                          tool_ids=[], use_memories=False, think_budget=0, turn='new')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return [json.loads(s[5:]) async for s in chat.chat_stream_generate(req, http, EventBus(), {}, {})]

    return run, state


def test_a_reply_unloads_the_other_model_before_it_calls_its_own(chat_run, monkeypatch):
    run, state = chat_run
    monkeypatch.setattr(config, 'CHAT_MAX_LOADED_MODELS', 1)
    events = asyncio.run(run())
    assert state.order == ['unload other:27b', 'chat']
    assert [m['name'] for m in state.models] == ['qwen3.5:4b']      # the helper model stays
    assert events[-1]['type'] == 'done'


def test_with_the_setting_off_a_reply_unloads_nothing(chat_run, monkeypatch):
    run, state = chat_run
    monkeypatch.setattr(config, 'CHAT_MAX_LOADED_MODELS', 0)
    asyncio.run(run())
    assert state.order == ['chat'] and len(state.models) == 2
