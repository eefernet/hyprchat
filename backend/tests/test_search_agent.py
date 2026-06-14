"""
Unit tests for `backend/search_agent.py` — the multi-round search agent.

These are pure-logic tests with mocked Ollama and mocked SearXNG cache. No
network. Run with:
    pytest backend/tests/test_search_agent.py -v
"""
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

# Ensure backend/ is importable when pytest runs from repo root or backend/.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import search_agent  # noqa: E402
import quick_search  # noqa: E402  (we patch helpers on this module)
import config  # noqa: E402


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


class _FakeEvents:
    def __init__(self):
        self.items = []

    async def emit(self, conv_id, evt, data):
        self.items.append((conv_id, evt, data))


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


def _many_web_results(n: int, *, prefix: str = "Result") -> list[dict]:
    return [
        {
            "title": f"{prefix} {i}",
            "url": f"https://source{i}.test/article",
            "content": f"snippet about latest broad search result {i}",
            "engine": "test",
            "score": 10,
            "type": "web",
        }
        for i in range(n)
    ]


def test_run_search_agent_skip_gate():
    """Greetings short-circuit before any LLM call."""
    http = _FakeHTTP([])  # no responses queued — would error if triage ran
    messages = [{"role": "user", "content": "hi"}]
    out = _run(search_agent.run_search_agent(
        http, "http://ollama", "test-model",
        events=None, conv_id=None, messages=messages,
    ))
    assert out["skipped"] is True
    assert out["reason"] == "greeting"
    assert http.calls == []  # zero LLM calls


def test_run_search_agent_operate_on_attached_skips():
    """Operate-on-attached messages hit the skip gate → no SearXNG."""
    plan = json.dumps({
        "needs_search": False, "queries": [], "category": "general", "reason": "operate on attached",
    })
    http = _FakeHTTP([_ollama_response(plan)])
    messages = [{"role": "user", "content": "rewrite this paragraph for me: foo bar baz"}]

    with patch.object(quick_search, "_cached_search", new=AsyncMock()) as mock_search:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is True
        assert mock_search.await_count == 0  # skip gate fired, no search ran


def test_run_search_agent_high_relevance_no_refine():
    """Balanced mode plans deterministically and does not call refine."""
    http = _FakeHTTP([])

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
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert mock_search.await_count >= 1
        assert http.calls == []               # no LLM planning call
        assert mock_refine.await_count == 0   # no refinement


def test_run_search_agent_news_recency_passes_month_time_range():
    """News plans with recency cues pass time_range='month' into SearXNG."""
    http = _FakeHTTP([])
    fake_results = _searxng_results(
        ("Latest UK general election updates", "https://bbc.co.uk/latest"),
    )
    messages = [{"role": "user", "content": "latest UK general election updates"}]

    with patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=fake_results)) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="should-not-be-called")):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert mock_search.await_count >= 2
        assert {c.kwargs["time_range"] for c in mock_search.await_args_list} == {"month"}
        assert {c.kwargs["categories"] for c in mock_search.await_args_list} == {"news"}
        assert http.calls == []


def test_run_search_agent_passes_configured_searxng_engines():
    http = _FakeHTTP([])
    fake_results = _searxng_results(
        ("Latest UK general election updates", "https://bbc.co.uk/latest"),
    )
    messages = [{"role": "user", "content": "latest UK general election updates"}]

    with patch.object(config, "QUICK_SEARCH_SEARXNG_NEWS_ENGINES", "brave,reuters"), \
         patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=fake_results)) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))

    assert out["skipped"] is False
    assert {c.kwargs["engines"] for c in mock_search.await_args_list} == {"brave,reuters"}
    assert "SearXNG engines: brave,reuters" in out["context"]


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        return fixed.astimezone(tz) if tz else fixed


def test_run_search_agent_today_uses_day_freshness_and_resolved_date():
    http = _FakeHTTP([])
    events = _FakeEvents()
    messages = [{"role": "user", "content": "what was the california elections like today?"}]
    fake_results = [
        {
            "title": "California elections update",
            "url": "https://apnews.com/california-election",
            "content": "California election coverage from today",
            "engine": "test",
            "score": 80,
            "type": "web",
            "published_date": "2026-06-05",
        }
    ]

    with patch.object(search_agent, "datetime", _FixedDateTime), \
         patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=fake_results)) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=events, conv_id="conv-today", messages=messages,
        ))

    search_events = [d for _, evt, d in events.items if evt == "search_results"]
    assert out["skipped"] is False
    assert {c.kwargs["time_range"] for c in mock_search.await_args_list} == {"day", None}
    assert {c.kwargs["categories"] for c in mock_search.await_args_list} == {"news", "general"}
    assert search_events[-1]["resolved_date"] == "2026-06-05"
    assert search_events[-1]["freshness_mode"] == "day"
    assert "Resolved date: 2026-06-05" in out["context"]
    assert "FRESHNESS WARNING" not in out["context"]


def test_run_search_agent_today_event_query_uses_general_web_fallback():
    http = _FakeHTTP([])
    messages = [{"role": "user", "content": "tell me about wwdc that just took place today"}]
    captured: list[tuple[str, str | None, str]] = []

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        captured.append((query, time_range, categories))
        if categories == "general" and time_range is None and query == "wwdc 2026":
            return [
                {
                    "title": "WWDC 2026 announcements live updates",
                    "url": "https://example.com/wwdc-2026-live",
                    "content": "Apple WWDC 2026 announcements from today's keynote",
                    "engine": "test",
                    "score": 80,
                    "type": "web",
                }
            ]
        return []

    with patch.object(search_agent, "datetime", _FixedDateTime), \
         patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id="conv-wwdc", messages=messages,
        ))

    assert out["skipped"] is False
    assert out["reason"] == ""
    assert any(q == "wwdc 2026" and tr == "day" and cat == "news" for q, tr, cat in captured)
    assert any(q == "wwdc 2026" and tr is None and cat == "general" for q, tr, cat in captured)
    assert all("took place" not in q for q, _, _ in captured)
    assert "WWDC 2026 announcements live updates" in out["context"]
    assert "FRESHNESS WARNING" not in out["context"]


def test_run_search_agent_today_warns_when_sources_are_not_same_day():
    http = _FakeHTTP([])
    messages = [{"role": "user", "content": "what was the california elections like today?"}]
    stale_results = [
        {
            "title": "California election background",
            "url": "https://example.com/california-election",
            "content": "Older California election coverage",
            "engine": "test",
            "score": 80,
            "type": "web",
            "published_date": "2026-06-04",
        }
    ]

    with patch.object(search_agent, "datetime", _FixedDateTime), \
         patch.object(quick_search, "_cached_search", new=AsyncMock(return_value=stale_results)), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))

    assert "FRESHNESS WARNING" in out["context"]
    assert "do not prove activity on 2026-06-05" in out["context"]


def test_run_search_agent_returns_balanced_target_chat_results():
    http = _FakeHTTP([])
    events = _FakeEvents()
    messages = [{"role": "user", "content": "latest broad search topic"}]

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        assert 10 <= count <= quick_search.CHAT_SEARCH_COUNT_BROAD
        assert time_range == "month"
        assert categories == "news"
        return _many_web_results(40)

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="should-not-be-called")):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=events, conv_id="conv-1", messages=messages,
        ))

    search_events = [d for _, evt, d in events.items if evt == "search_results"]
    done_events = [d for _, evt, d in events.items if evt == "tool_done"]
    assert out["skipped"] is False
    assert len(search_events[-1]["results"]) == quick_search.CHAT_TARGET_RESULTS
    assert search_events[-1]["freshness_mode"] == "month"
    assert "score_reason" in search_events[-1]["results"][0]
    assert done_events[-1]["status"] == f"Found {quick_search.CHAT_TARGET_RESULTS} results"
    assert f"{quick_search.CHAT_TARGET_RESULTS}. **" in out["context"]
    assert f"{quick_search.CHAT_TARGET_RESULTS + 1}. **" not in out["context"]


def test_embed_dedup_backfills_to_35_when_candidates_exist():
    results = _many_web_results(40, prefix="Candidate")
    dim = 50
    q = [1.0] + [0.0] * (dim - 1)
    high_dup = [1.0] + [0.0] * (dim - 1)
    low_unique_mag = math.sqrt(1.0 - 0.2 ** 2)
    embeddings = [q]
    for i in range(40):
        if i < 6:
            embeddings.append(high_dup)
        else:
            v = [0.2] + [0.0] * (dim - 1)
            v[i - 5] = low_unique_mag
            embeddings.append(v)

    async def fake_embed_batch(http, ollama_url, texts):
        assert len(texts) == 41
        return embeddings

    with patch.object(quick_search, "_ollama_embed_batch", new=fake_embed_batch):
        out = _run(quick_search._embed_score_and_dedup(
            None, "http://ollama", "query", results,
            limit=quick_search.CHAT_MAX_RESULTS, backfill=True,
        ))

    urls = [r["url"] for r in out]
    assert len(out) == quick_search.CHAT_MAX_RESULTS
    assert urls[:1] == [results[0]["url"]]
    assert all(results[i]["url"] not in urls for i in range(1, 6))


def test_run_search_agent_page_enrichment_only_uses_top_configured_results():
    http = _FakeHTTP([])
    messages = [{"role": "user", "content": "latest broad search topic"}]
    captured = {}

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        return _many_web_results(40)

    async def fake_enrich_with_pages(http, results, top_n=3):
        captured["result_count"] = len(results)
        captured["top_n"] = top_n
        return {}

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=fake_enrich_with_pages), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="should-not-be-called")):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))

    assert out["skipped"] is False
    assert captured == {
        "result_count": quick_search.CHAT_TARGET_RESULTS,
        "top_n": quick_search.CHAT_PAGE_FETCH_COUNT,
    }


def test_run_search_agent_low_relevance_refines():
    """Quality mode can run one refinement round after weak deterministic results."""
    http = _FakeHTTP([])

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

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        cached_calls["n"] += 1
        return on_topic_after_refine if query == "UK Reform Party 2026 election" else off_topic

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(config, "QUICK_SEARCH_MODE", "quality"), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="UK Reform Party 2026 election")) as mock_refine:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert mock_refine.await_count == 1   # refined exactly once
        assert cached_calls["n"] >= 2          # initial fanout + refined round


def test_run_search_agent_max_rounds_caps():
    """Even with persistently low relevance, refinement runs at most once."""
    http = _FakeHTTP([])

    off_topic = _searxng_results(("totally unrelated", "https://x.com/a"))
    messages = [{"role": "user", "content": "tell me about UK Reform Party 2026 elections politics"}]

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        return off_topic  # always off topic

    with patch.object(quick_search, "_cached_search", new=fake_cached_search) as mock_search, \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)), \
         patch.object(config, "QUICK_SEARCH_MODE", "quality"), \
         patch.object(search_agent, "refine_query", new=AsyncMock(return_value="another query")) as mock_refine:
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
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
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))
        assert out["skipped"] is False
        assert out["reason"] == "no results"


def test_run_search_agent_search_failure_injects_unavailable_context():
    http = _FakeHTTP([])
    messages = [{"role": "user", "content": "latest UK general election updates"}]

    async def failing_search(http, query, count=10, time_range=None, categories="general", engines=None):
        raise RuntimeError("searxng down")

    with patch.object(quick_search, "_cached_search", new=failing_search):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
        ))

    assert out["skipped"] is False
    assert out["reason"] == "no results"
    assert "WEB SEARCH UNAVAILABLE" in out["context"]
    assert "cannot verify it from fresh sources" in out["context"]


def test_run_search_agent_default_path_does_not_call_triage():
    """Default Quick Search uses deterministic planning instead of LLM triage."""
    http = _FakeHTTP([_ollama_response("garbage that is not json")])
    user_msg = "tell me about UK Reform Party recent gains"
    messages = [{"role": "user", "content": user_msg}]

    fake_results = _searxng_results(("UK Reform results", "https://bbc.co.uk/x"))
    captured_queries: list[str] = []

    async def fake_cached_search(http, query, count=10, time_range=None, categories="general", engines=None):
        captured_queries.append(query)
        return fake_results

    with patch.object(quick_search, "_cached_search", new=fake_cached_search), \
         patch.object(quick_search, "_enrich_with_pages", new=AsyncMock(return_value={})), \
         patch.object(quick_search, "_enrich_og_images", new=AsyncMock(return_value=None)):
        out = _run(search_agent.run_search_agent(
            http, "http://ollama", "test-model",
            events=None, conv_id=None, messages=messages,
            default_model="test-model",
        ))
        assert out["skipped"] is False
        assert http.calls == []
        assert captured_queries[0] == "UK Reform Party recent gains"
        assert out["rewritten_query"] == user_msg
