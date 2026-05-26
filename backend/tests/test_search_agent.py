"""
Unit tests for `backend/search_agent.py` — the multi-round search agent.

These are pure-logic tests with mocked Ollama and mocked SearXNG cache. No
network. Run with:
    pytest backend/tests/test_search_agent.py -v
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

# Ensure backend/ is importable when pytest runs from repo root or backend/.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import search_agent  # noqa: E402
import quick_search  # noqa: E402  (we patch helpers on this module)


def _run(coro):
    return asyncio.run(coro)


# ── Fake HTTP client for the JSON-mode call ──
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    """Captures POST calls and returns a queued response."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self._responses:
            raise RuntimeError("no more responses queued")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _FakeResponse(nxt)


def _ollama_response(text: str) -> dict:
    """Shape Ollama's /api/generate non-streaming response."""
    return {"response": text, "done": True}


# ── relevance_score ──
def test_relevance_score_strong_match():
    user_msg = "tell me about UK Reform Party 2026 elections"
    queries = ["UK Reform Party platform"]
    results = [
        {"title": "Reform UK 2026 elections", "content": "The UK Reform Party..."},
        {"title": "Election results UK", "content": "Reform Party gains..."},
    ]
    score = search_agent.relevance_score(user_msg, queries, results)
    assert score >= 0.5, f"expected strong overlap, got {score}"


def test_relevance_score_off_topic():
    # Genuinely off-topic results (different vocabulary) → score below the
    # refine threshold (0.30). Note: when the user's question and the wrong
    # entity share the literal name (e.g. "UK Reform Party" vs "US Reform
    # Party"), token-overlap can't tell them apart — that's the domain-bias
    # layer's job, not the relevance heuristic's. This test validates the
    # *mechanics* of the heuristic on cleanly-disjoint cases.
    user_msg = "tell me about UK general election results politics"
    queries = ["UK election"]
    results = [
        {"title": "Best chocolate chip cookie recipes", "content": "baking ingredients..."},
        {"title": "Python regex tutorial", "content": "matching strings with patterns..."},
    ]
    score = search_agent.relevance_score(user_msg, queries, results)
    assert score < 0.30, f"expected refine threshold trigger, got {score}"


def test_relevance_score_empty_results():
    assert search_agent.relevance_score("test query", ["test"], []) == 0.0


def test_relevance_score_no_user_content():
    # User message has no content tokens (just stopwords) — assume results fine.
    assert search_agent.relevance_score("the and is to", ["x"], [{"title": "y"}]) == 1.0


# ── _validate_triage ──
def test_validate_triage_accepts_valid():
    out = search_agent._validate_triage(
        {
            "needs_search": True,
            "queries": ["UK general election 2026 results"],
            "category": "news",
            "reason": "follow-up about UK elections",
        },
        latest="whos winning?",
        prior_tokens={"election", "uk"},
    )
    assert out is not None
    assert out["needs_search"] is True
    assert out["queries"] == ["UK general election 2026 results"]
    assert out["category"] == "news"


def test_validate_triage_rejects_non_dict():
    assert search_agent._validate_triage("not a dict", "hi", set()) is None
    assert search_agent._validate_triage(None, "hi", set()) is None


def test_validate_triage_rejects_missing_fields():
    assert search_agent._validate_triage({"queries": []}, "hi", set()) is None
    assert search_agent._validate_triage(
        {"needs_search": True, "queries": []}, "hi", set(),
    ) is None  # claims search but produced no usable queries


def test_validate_triage_drops_url_queries():
    out = search_agent._validate_triage(
        {
            "needs_search": True,
            "queries": ["good query about cats", "https://example.com/ignore"],
            "category": "general",
        },
        latest="cats",
        prior_tokens=set(),
    )
    assert out is not None
    assert out["queries"] == ["good query about cats"]


def test_validate_triage_followup_must_carry_topic():
    # Latest is a bare follow-up, prior is about UK elections, but the queries
    # don't share any prior content tokens → reject.
    out = search_agent._validate_triage(
        {
            "needs_search": True,
            "queries": ["random unrelated topic about whales"],
            "category": "general",
        },
        latest="whos winning?",
        prior_tokens={"uk", "election", "general"},
    )
    assert out is None, "follow-up that drops prior topic must be rejected"


def test_validate_triage_normalizes_bad_category():
    out = search_agent._validate_triage(
        {
            "needs_search": True,
            "queries": ["python regex tutorial"],
            "category": "weather",  # not in allowed set
        },
        latest="python regex",
        prior_tokens=set(),
    )
    assert out is not None
    assert out["category"] == "general"


def test_validate_triage_skip_path_normalized():
    out = search_agent._validate_triage(
        {"needs_search": False, "queries": ["leftover"], "category": "general"},
        latest="hi",
        prior_tokens=set(),
    )
    assert out is not None
    assert out["needs_search"] is False
    assert out["queries"] == []


# ── triage with mocked HTTP ──
def test_triage_valid_json_returns_plan():
    plan_json = json.dumps({
        "needs_search": True,
        "queries": ["UK election 2026 results"],
        "category": "news",
        "reason": "follow-up",
    })
    http = _FakeHTTP([_ollama_response(plan_json)])

    messages = [
        {"role": "user", "content": "tell me about elections in the UK"},
        {"role": "assistant", "content": "The UK holds general elections..."},
    ]
    out = _run(search_agent.triage(http, "http://ollama", "test-model", messages, "whos winning?"))
    assert out is not None
    assert out["needs_search"] is True
    assert out["queries"] == ["UK election 2026 results"]
    assert out["category"] == "news"
    # Ollama call uses format=json
    assert http.calls[0]["json"]["format"] == "json"


def test_triage_invalid_json_returns_none():
    http = _FakeHTTP([_ollama_response("this is not json at all, just words")])
    out = _run(search_agent.triage(http, "http://ollama", "test-model", [], "hi there"))
    assert out is None


def test_triage_strips_code_fence():
    fenced = "```json\n" + json.dumps({
        "needs_search": False, "queries": [], "category": "general", "reason": "greeting",
    }) + "\n```"
    http = _FakeHTTP([_ollama_response(fenced)])
    out = _run(search_agent.triage(http, "http://ollama", "test-model", [], "hello"))
    assert out is not None
    assert out["needs_search"] is False


def test_triage_returns_none_on_empty_model():
    out = _run(search_agent.triage(None, "http://ollama", "", [], "hi"))
    assert out is None


def test_triage_returns_none_on_http_error():
    http = _FakeHTTP([RuntimeError("network down")])
    out = _run(search_agent.triage(http, "http://ollama", "test-model", [], "hi"))
    assert out is None


# ── _merge_unique ──
def test_merge_unique_dedupes_by_url():
    a = [{"url": "https://a.com", "title": "A"}, {"url": "https://b.com", "title": "B"}]
    b = [{"url": "https://b.com", "title": "B-dup"}, {"url": "https://c.com", "title": "C"}]
    merged = search_agent._merge_unique([a, b])
    assert [r["url"] for r in merged] == ["https://a.com", "https://b.com", "https://c.com"]
    # First-seen wins
    assert merged[1]["title"] == "B"


# ── run_search_agent orchestration ──
def _searxng_results(*titles_urls):
    return [{"title": t, "url": u, "content": f"snippet about {t}"} for t, u in titles_urls]


def test_run_search_agent_skip_gate():
    """Greetings short-circuit before any LLM call."""
    http = _FakeHTTP([])  # no responses queued — would error if triage ran
    messages = [{"role": "user", "content": "hi"}]
    out = _run(search_agent.run_search_agent(
        http, "http://ollama", "test-model", "test-model",
        events=None, conv_id=None, messages=messages,
    ))
    assert out["skipped"] is True
    assert out["reason"] == "greeting"
    assert http.calls == []  # zero LLM calls


def test_run_search_agent_triage_skip():
    """Triage returns needs_search=false → no SearXNG, return skip."""
    plan = json.dumps({
        "needs_search": False, "queries": [], "category": "general", "reason": "operate on attached",
    })
    http = _FakeHTTP([_ollama_response(plan)])
    messages = [{"role": "user", "content": "rewrite this paragraph for me: foo bar baz"}]

    with patch.object(quick_search, "_cached_search", new=AsyncMock()) as mock_search:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is True
        assert mock_search.await_count == 0  # triage said no, no search ran


def test_run_search_agent_high_relevance_no_refine():
    """One round when relevance is high — refine_query is never called."""
    plan = json.dumps({
        "needs_search": True,
        "queries": ["UK general election 2026 results"],
        "category": "news",
        "reason": "follow-up",
    })
    http = _FakeHTTP([_ollama_response(plan)])

    fake_results = _searxng_results(
        ("UK general election 2026 results", "https://bbc.co.uk/election"),
        ("Reform UK gains in election", "https://guardian.co.uk/x"),
    )

    messages = [
        {"role": "user", "content": "tell me about the UK general election 2026 results"},
    ]

    with patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=fake_results)) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="should-not-be-called")) as mock_refine:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert mock_search.await_count == 1   # single round
        assert mock_refine.await_count == 0   # no refinement


def test_run_search_agent_low_relevance_refines():
    """Low relevance → refine_query is called once and the refined query is searched."""
    plan = json.dumps({
        "needs_search": True,
        "queries": ["election"],  # too vague — returns off-topic vocabulary
        "category": "news",
        "reason": "follow-up",
    })
    http = _FakeHTTP([_ollama_response(plan)])

    # Off-topic results share NO content tokens with the user message — that's
    # what the relevance heuristic is designed to catch.
    off_topic = _searxng_results(
        ("chocolate chip cookie recipes", "https://example.com/cookies"),
        ("python regex tutorial", "https://example.com/regex"),
    )
    on_topic_after_refine = _searxng_results(
        ("UK general election 2026 results", "https://bbc.co.uk/uk-elect"),
    )

    messages = [
        {"role": "user", "content": "tell me about UK general election 2026 results politics"},
    ]

    cached_calls = {"n": 0}

    async def fake_cached_search(http, query, count=10):
        cached_calls["n"] += 1
        return off_topic if cached_calls["n"] == 1 else on_topic_after_refine

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="UK Reform Party 2026 election")) as mock_refine:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert mock_refine.await_count == 1   # refined exactly once
        assert cached_calls["n"] == 2          # round 1 + refined round


def test_run_search_agent_max_rounds_caps():
    """Even with persistently low relevance, refinement runs at most once."""
    plan = json.dumps({
        "needs_search": True, "queries": ["bad query"],
        "category": "general", "reason": "x",
    })
    http = _FakeHTTP([_ollama_response(plan)])

    off_topic = _searxng_results(("totally unrelated", "https://x.com/a"))
    messages = [{"role": "user", "content": "tell me about UK Reform Party 2026 elections politics"}]

    async def fake_cached_search(http, query, count=10):
        return off_topic  # always off topic

    with patch.object(quick_search, "_cached_search", new=fake_cached_search) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="another query")) as mock_refine:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
            max_rounds=2,
        ))
        assert mock_refine.await_count == 1   # refined exactly once even though still bad


def test_run_search_agent_no_results():
    """SearXNG returns nothing → return early with reason='no results'."""
    plan = json.dumps({
        "needs_search": True, "queries": ["something"],
        "category": "general", "reason": "x",
    })
    http = _FakeHTTP([_ollama_response(plan)])
    messages = [{"role": "user", "content": "something obscure that has no results"}]

    with patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=[])):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert out["reason"] == "no results"


def test_run_search_agent_triage_failure_searches_raw_message():
    """If triage returns invalid JSON, the agent searches the raw user message."""
    http = _FakeHTTP([_ollama_response("garbage that is not json")])
    user_msg = "tell me about UK Reform Party recent gains"
    messages = [{"role": "user", "content": user_msg}]

    fake_results = _searxng_results(("UK Reform results", "https://bbc.co.uk/x"))
    captured_queries: list[str] = []

    async def fake_cached_search(http, query, count=10):
        captured_queries.append(query)
        return fake_results

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model", "test-model",
            events=None, conv_id=None, messages=messages,
            default_model="test-model",
        ))
        assert out["skipped"] is False
        # Triage failed → searched the raw user message verbatim
        assert captured_queries == [user_msg]
        assert out["rewritten_query"] == user_msg
