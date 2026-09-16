"""Offline acceptance cases for bounded, shared interactive web search."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import context_policy
import quick_search as qs
import research
import search_agent as agent
from search_runtime import current_run, diagnose_response


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(config, "QUICK_SEARCH_MODE", "balanced")
    monkeypatch.setattr(config, "QUICK_SEARCH_PLANNER", "deterministic")
    monkeypatch.setattr(config, "QUICK_SEARCH_RERANKER", "none")
    monkeypatch.setattr(config, "QUICK_SEARCH_EMBED_RERANK", False)
    monkeypatch.setattr(config, "QUICK_SEARCH_TIMEOUT", 0.3)
    qs._CACHE.clear()


def article(url="https://example.org/report", **kwargs):
    return {"url": url, "title": "Python asyncio cancellation", "content": "Python asyncio cancellation raises CancelledError in the task.", "type": "web", **kwargs}


def run_search(**kwargs):
    return agent.run_search_agent(None, "http://ollama", "planner", kwargs.pop("events", None), "test",
        [{"role": "user", "content": "Python asyncio cancellation"}], **kwargs)


def no_pages(monkeypatch):
    monkeypatch.setattr(qs, "_enrich_with_pages", AsyncMock(return_value={}))
    monkeypatch.setattr(qs, "_enrich_og_images", AsyncMock())


def test_partial_provider_deadline_preserves_evidence_and_joins_children(monkeypatch):
    cancelled = []
    async def search(http, query, **kwargs):
        if query == "Python asyncio cancellation":
            await asyncio.sleep(0.01)
            return [article()]
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(query)
    monkeypatch.setattr(qs, "_cached_search", search)
    no_pages(monkeypatch)
    async def check():
        start = asyncio.get_running_loop().time()
        result = await run_search()
        assert asyncio.get_running_loop().time() - start < 0.45
        assert result["status"] == "partial"
        assert "CancelledError" in result["context"]
        assert cancelled
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert current_run.get() is None
    asyncio.run(check())


def test_user_cancellation_propagates_and_stops_queries(monkeypatch):
    started, stopped = [], []
    async def search(http, query, **kwargs):
        started.append(query)
        try:
            await asyncio.sleep(5)
        finally:
            stopped.append(query)
    monkeypatch.setattr(qs, "_cached_search", search)
    async def check():
        task = asyncio.create_task(run_search())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started and sorted(started) == sorted(stopped)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(check())


def test_slow_planner_never_delays_first_wave(monkeypatch):
    monkeypatch.setattr(config, "QUICK_SEARCH_PLANNER", "llm")
    stopped = []
    async def planner(*args, **kwargs):
        try:
            await asyncio.sleep(5)
        finally:
            stopped.append(True)
    async def search(*args, **kwargs):
        await asyncio.sleep(0.01)
        return [article()]
    monkeypatch.setattr(agent, "llm_plan", planner)
    monkeypatch.setattr(qs, "_cached_search", search)
    no_pages(monkeypatch)
    async def check():
        start = asyncio.get_running_loop().time()
        result = await run_search()
        assert asyncio.get_running_loop().time() - start < 0.15
        assert result["results"] and stopped
    asyncio.run(check())


def test_deadline_during_page_reads_keeps_completed_page(monkeypatch):
    monkeypatch.setattr(qs, "_cached_search", AsyncMock(return_value=[article(), article("https://second.org/report")]))
    monkeypatch.setattr(qs, "_enrich_og_images", AsyncMock())
    monkeypatch.setattr(qs, "_url_safe", AsyncMock(return_value=True))
    stopped = []
    async def fetch(http, url):
        if "second" in url:
            try:
                await asyncio.sleep(5)
            finally:
                stopped.append(True)
        return {"url": url, "content": "Python asyncio cancellation: the answer is in the final paragraph. " * 4}
    monkeypatch.setattr(qs, "_fetch_clean_page", fetch)
    result = asyncio.run(run_search())
    assert "final paragraph" in result["context"]
    assert stopped
    assert result["results"]


@pytest.mark.parametrize("code,payload,expected", [
    (200, {"results": []}, "no_results"),
    (200, {"results": [], "unresponsive_engines": [["google", "HTTP connection error"]]}, "connection_error"),
    (200, {"results": [], "unresponsive_engines": [["google", "HTTP timeout"]]}, "timeout"),
    (200, {"results": [article()], "unresponsive_engines": [["bing", "CAPTCHA"]]}, "partial"),
    (429, {}, "rate_limited"), (503, {}, "http_error"), (403, {}, "http_error"),
    (200, [], "invalid_response"), (200, {"results": {}}, "invalid_response"),
])
def test_failure_classification(code, payload, expected):
    assert diagnose_response(code, payload)["status"] == expected


@pytest.mark.parametrize("payload", [
    {"results": [], "unresponsive_engines": [["google", "HTTP connection error"]]},
    {"results": [], "unresponsive_engines": [["google", "HTTP timeout"]]},
])
def test_outage_does_not_launch_doomed_google_fallback(monkeypatch, payload):
    fallback = AsyncMock(return_value=[])
    monkeypatch.setattr(research, "_search_google_fallback", fallback)
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json=payload)
    diagnosis = {}
    assert asyncio.run(research._search_searxng(client, "http://search", "example", diagnostics=diagnosis)) == []
    assert diagnosis["status"] in {"timeout", "connection_error"}
    fallback.assert_not_awaited()


def test_google_fallback_shared_budget(monkeypatch):
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json={"results": []})
    fallback = AsyncMock(return_value=[])
    monkeypatch.setattr(research, "_search_google_fallback", fallback)
    async def check():
        budget = {"remaining": 1}
        await asyncio.gather(*(research._search_searxng(client, "http://search", str(i), fallback_state=budget) for i in range(5)))
    asyncio.run(check())
    assert fallback.await_count == 1


def test_cache_is_provider_scoped_sliced_and_isolated(monkeypatch):
    fetch = AsyncMock(return_value=[article(), article("https://second.org/a")])
    monkeypatch.setattr(qs, "_search_searxng", fetch)
    async def check():
        first = await qs._cached_search(None, "query", count=2)
        first[0]["title"] = "mutated"
        hit = await qs._cached_search(None, "query", count=1)
        assert len(hit) == 1 and hit[0]["title"] != "mutated"
        monkeypatch.setattr(config, "SEARXNG_URL", "http://replacement")
        await qs._cached_search(None, "query", count=1)
        assert fetch.await_count == 2
    asyncio.run(check())


def test_recency_cache_expires_before_evergreen_cache(monkeypatch):
    fetch = AsyncMock(return_value=[article()])
    monkeypatch.setattr(qs, "_search_searxng", fetch)
    now = [1000.0]
    monkeypatch.setattr(qs.time, "time", lambda: now[0])
    async def check():
        for time_range in ("day", None):
            await qs._cached_search(None, "query", count=1, time_range=time_range)
        now[0] += 61
        for time_range in ("day", None):
            await qs._cached_search(None, "query", count=1, time_range=time_range)
        assert fetch.await_count == 3
    asyncio.run(check())


def test_tracking_url_merge_keeps_query_and_engine_provenance():
    results = agent._merge_unique([[article("https://example.org/report?utm_source=a#top", query_origin="one", engine="google")],
        [article(query_origin="two", engine="bing")]])
    assert len(results) == 1
    assert results[0]["query_origins"] == ["one", "two"]
    assert results[0]["engines"] == ["google", "bing"]
    assert len(agent._merge_unique([[article("https://example.org/?id=1"), article("https://example.org/?id=2")]])) == 2


def test_passages_find_late_answer_and_context_respects_utf8_budget():
    page = "Background navigation unrelated material. " * 600 + "Python asyncio cancellation requires awaiting the cancelled task to observe CancelledError."
    excerpts = {article()["url"]: qs._select_passages(page, "Python asyncio cancellation")}
    assert "awaiting the cancelled task" in excerpts[article()["url"]]
    records = [article("https://example.org/" + str(i), content="日本語の説明" * 40) for i in range(24)]
    records[0] = article()
    context = qs._build_context(records, "Python asyncio cancellation", excerpts, set(), context_budget=700)
    assert context_policy.estimate_tokens(context) <= 700
    assert "awaiting the cancelled task" in context
    assert "</web_sources>" in context


def test_historical_year_is_not_a_recency_filter():
    assert agent._freshness_from_text("United States population 2025", datetime(2026, 9, 14))[2] is None
    assert not qs._should_skip("日本の最新ニュース")[0]
    assert not qs._has_same_day_text_evidence("Live updates 2026: today", datetime(2026, 9, 14).date())


def test_research_adapter_preserves_tool_end_and_research_carousel(monkeypatch):
    monkeypatch.setattr(qs, "_cached_search", AsyncMock(return_value=[article()]))
    no_pages(monkeypatch)
    class Events:
        def __init__(self): self.items = []
        async def emit(self, cid, event, data): self.items.append((event, data))
    events = Events()
    result = asyncio.run(qs.run_quick_search_for_chat(None, "http://ollama", "", events, "test",
        [{"role": "user", "content": "Python asyncio cancellation"}], tool_name="research", force_search=True))
    assert result["results"]
    assert any(event == "tool_end" and data["tool"] == "research" for event, data in events.items)
    assert all("source" not in data for event, data in events.items if event == "search_results")
    assert not any(event == "tool_done" for event, _ in events.items)


def test_force_search_bypasses_automatic_skip_gate(monkeypatch):
    search = AsyncMock(return_value=[article()])
    monkeypatch.setattr(qs, "_cached_search", search)
    no_pages(monkeypatch)
    result = asyncio.run(agent.run_search_agent(None, "", "", None, "test", [{"role": "user", "content": "hi"}], force_search=True))
    assert not result["skipped"]
    assert search.await_count


def test_ranking_acceptance_fixtures_compare_legacy_and_hybrid(monkeypatch):
    fixtures = json.loads((Path(__file__).parent / "fixtures/search_ranking.json").read_text())
    baseline_total = improved_total = 0
    for case in fixtures:
        by_url = {r["url"]: r["vector"] for r in case["results"]}
        ranked = qs._rank_for_search_plan(case["results"], case["query"],
            {"freshness_mode": case["freshness"], "resolved_date": case.get("date")})
        async def embed(http, url, texts):
            assert texts[0] == "search_query: " + case["query"]
            assert all(t.startswith("search_document: ") for t in texts[1:])
            return [[1, 0]] + [by_url[r["url"]] for r in ranked]
        monkeypatch.setattr(qs, "_ollama_embed_batch", embed)
        monkeypatch.setattr(qs, "_EMBED_MODEL", "nomic-embed-text:latest")
        outputs = {}
        for mode in ("legacy", "hybrid"):
            monkeypatch.setattr(config, "QUICK_SEARCH_RANKING", mode)
            outputs[mode] = asyncio.run(qs._embed_score_and_dedup(None, "", case["query"], ranked, limit=len(case["expected"]), backfill=True))
        counts = {mode: len(set(r["url"] for r in output) & set(case["expected"])) for mode, output in outputs.items()}
        assert counts["hybrid"] == len(case["expected"]), case["name"]
        assert counts["hybrid"] >= counts["legacy"], case["name"]
        baseline_total += counts["legacy"]
        improved_total += counts["hybrid"]
    assert improved_total > baseline_total
    print(f"ranking fixture recall: legacy={baseline_total}/6 hybrid={improved_total}/6")


def test_prefixes_are_not_applied_to_other_embedding_models(monkeypatch):
    monkeypatch.setattr(qs, "_EMBED_MODEL", "another-embed-model")
    embed = AsyncMock(return_value=[[1, 0], [1, 0]])
    monkeypatch.setattr(qs, "_ollama_embed_batch", embed)
    asyncio.run(qs._embed_score_and_dedup(None, "", "query", [article()]))
    assert embed.call_args.args[2][0] == "query"


def test_embedding_timeout_does_not_fan_out_legacy_requests():
    client = AsyncMock()
    client.post.side_effect = httpx.ReadTimeout("busy")
    assert asyncio.run(qs._ollama_embed_batch(client, "http://ollama", ["a", "b"])) is None
    assert client.post.await_count == 1


def test_health_does_not_label_connection_outage_as_rate_limit(monkeypatch):
    pytest.importorskip("fastapi")
    from routes import health
    client = AsyncMock()
    client.get.side_effect = [httpx.Response(200), httpx.Response(200, json={
        "results": [], "unresponsive_engines": [["bing", "HTTP connection error"]]})]
    monkeypatch.setattr(health, "_http", lambda: client)
    result = asyncio.run(health._check_searxng())
    assert result["status"] == "degraded"
    assert result["search_status"] == "connection_error"
    assert result["rate_limited"] is False


def test_council_invokes_shared_search_before_starting_members(monkeypatch):
    import council
    search = AsyncMock(return_value={"context": "shared evidence"})
    monkeypatch.setattr(qs, "run_quick_search_for_chat", search)
    monkeypatch.setattr(council.db, "add_message", AsyncMock())
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json={"models": []})
    events = AsyncMock()
    async def check():
        stream = council.stream_council_chat(client, events, {"members": [], "host_model": "test"},
            [{"role": "user", "content": "query"}], "test", quick_search=True)
        await stream.__anext__()
        search.assert_awaited_once()
        assert search.call_args.kwargs["context_budget"] <= 1500
        await stream.aclose()
    asyncio.run(check())


def test_tool_dispatch_uses_shared_search(monkeypatch):
    import tools
    import coder_jobs
    from types import SimpleNamespace
    monkeypatch.setattr(coder_jobs, "route_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "build_gate_context", AsyncMock(return_value=SimpleNamespace(snapshot_partial=False, is_v2=False)))
    search = AsyncMock(return_value={"context": "shared evidence"})
    monkeypatch.setattr(qs, "run_quick_search_for_chat", search)
    result = asyncio.run(tools.exec_tool(None, AsyncMock(), "research", {"query": "a query"}, "test"))
    assert result == "shared evidence"
    assert search.call_args.kwargs["tool_name"] == "research"
    assert search.call_args.kwargs["force_search"] is True


def test_answer_formatting_does_not_pollute_news_queries():
    question = "Tell me the latest on US news. Give three short items with source links."
    plan = agent._deterministic_plan([{"role": "user", "content": question}], question, agent._mode_config())
    assert plan.queries[0] == "United States news"
    assert all("source links" not in q and "short items" not in q for q in plan.queries)
    assert plan.freshness_mode == "month"


def test_long_query_keeps_country_acronym_and_factual_followup():
    query = agent._clean_query_phrase("Please explain how the US can improve housing availability with current zoning regulations and local permitting restrictions")
    assert "US" in query
    assert "score" in agent._strip_search_noise("Who won the match? Give me the score.")


def test_week_search_uses_supported_searxng_filter():
    from urllib.parse import urlsplit, parse_qs
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json={"results": [article()]})
    asyncio.run(research._search_searxng(client, "http://search", "query", time_range="week"))
    assert parse_qs(urlsplit(client.get.call_args.args[0]).query)["time_range"] == ["month"]


def test_context_supplies_copyable_markdown_citations():
    record = article()
    context = qs._build_context([record], "Python asyncio cancellation", {}, set())
    assert f"[{record['title']}]({record['url']})" in context
    assert "Markdown link copied from its source heading" in context
