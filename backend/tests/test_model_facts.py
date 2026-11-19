"""Model abilities come from Ollama's own report, not from the model's name (model_facts.py). Offline."""
import asyncio

import httpx

import model_facts


def client(shows, tags=None, calls=None):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": tags if tags is not None else [{"name": n, "digest": "d1"} for n in shows]})
        name = __import__("json").loads(request.content)["model"]
        if calls is not None:
            calls.append(name)
        payload = shows.get(name)
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(500)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    model_facts.forget()
    return asyncio.run(coro)


def test_capabilities_and_limit_are_read_from_show():
    shows = {
        "gemma3:27b-it": {"capabilities": ["completion", "vision"], "model_info": {"gemma3.context_length": 131072}},
        "nomic-embed-text": {"capabilities": ["embedding"], "model_info": {"nomic-bert.context_length": 2048}},
    }
    facts = run(model_facts.installed(client(shows)))
    assert facts["gemma3:27b-it"] == {"capabilities": ["completion", "vision"], "context_limit": 131072}
    assert facts["nomic-embed-text"]["capabilities"] == ["embedding"]


def test_an_old_ollama_without_the_field_reports_unknown_not_none_of_them():
    facts = run(model_facts.installed(client({"old:1b": {"model_info": {}}})))
    assert facts["old:1b"] == {"capabilities": None, "context_limit": 0}


def test_a_model_that_cannot_be_read_is_left_out_and_the_rest_still_answer():
    facts = run(model_facts.installed(client({"good:1b": {"capabilities": ["completion"]}, "bad:1b": None})))
    assert list(facts) == ["good:1b"]


def test_answers_are_kept_until_the_model_changes():
    calls = []
    shows = {"m:1b": {"capabilities": ["completion", "tools"]}}

    async def twice():
        http = client(shows, calls=calls)
        await model_facts.installed(http)
        await model_facts.installed(http)                       # same digest: remembered
        await model_facts.installed(client(shows, tags=[{"name": "m:1b", "digest": "d2"}], calls=calls))   # re-created
    run(twice())
    assert calls == ["m:1b", "m:1b"]


def test_ollama_being_down_is_an_empty_answer_not_an_error():
    def refuse(request):
        raise httpx.ConnectError("down")
    assert run(model_facts.installed(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))) == {}
