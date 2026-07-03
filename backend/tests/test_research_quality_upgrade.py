"""
Pure-logic tests for the quality-first Deep Research upgrade.
No live SearXNG, Ollama, or ChromaDB required.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import research  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _seed_public_dns(host: str):
    """Mark a host as publicly-resolving in the SSRF DNS cache so the
    stream-based fetchers stay offline in tests."""
    research._DNS_CACHE[host] = (time.time(), True)


class _StreamResponse:
    def __init__(self, *, url, status_code=200, headers=None, content=b"ok"):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/plain"}
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        yield self._content


class _StreamHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def stream(self, method, url, **kwargs):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.responses.pop(0)


def test_safe_url_filtering_blocks_private_reserved_and_local_hosts():
    blocked = [
        "http://127.0.0.1/admin",
        "http://10.0.0.8",
        "http://172.16.0.10",
        "http://192.168.1.1",
        "http://169.254.1.1",
        "http://[::1]/",
        "http://example.local/page",
        "ftp://example.com/file",
    ]
    for url in blocked:
        assert not research._url_safe_for_direct_fetch(url), url
    assert research._url_safe_for_direct_fetch("https://example.com/report")


def test_fetch_page_rejects_redirect_to_private_host():
    _seed_public_dns("example.com")
    http = _StreamHTTP([
        _StreamResponse(
            url="https://example.com/start",
            status_code=302,
            headers={"location": "http://127.0.0.1/private"},
            content=b"",
        )
    ])
    assert _run(research._fetch_page(http, "https://example.com/start")) is None
    # The private redirect target must never be fetched.
    assert http.urls == ["https://example.com/start"]


def test_fetch_page_rejects_oversized_body():
    _seed_public_dns("example.com")
    big = b"x" * (research._RESEARCH_PAGE_MAX_BYTES + 1)
    http = _StreamHTTP([
        _StreamResponse(
            url="https://example.com/huge",
            headers={"content-type": "text/html"},
            content=big,
        )
    ])
    assert _run(research._fetch_page(http, "https://example.com/huge")) is None


def test_fetch_page_returns_cleaned_content_for_safe_page():
    _seed_public_dns("example.com")
    html = ("<html><body><p>" + ("public text. " * 80) + "</p></body></html>").encode()
    http = _StreamHTTP([
        _StreamResponse(
            url="https://example.com/article",
            headers={"content-type": "text/html; charset=utf-8"},
            content=html,
        )
    ])
    page = _run(research._fetch_page(http, "https://example.com/article"))
    assert page is not None
    assert page["url"].startswith("https://example.com/article")
    assert "public text." in page["content"]
    assert "<p>" not in page["content"]


def test_safe_fetch_rejects_private_redirect_before_fetching_target():
    http = _StreamHTTP([
        _StreamResponse(
            url="https://example.com/start",
            status_code=302,
            headers={"location": "http://127.0.0.1/private"},
            content=b"",
        )
    ])
    try:
        _run(research.fetch_bytes_safely(http, "https://example.com/start", resolve_dns=False))
    except ValueError as e:
        assert "Unsafe URL" in str(e)
    else:
        raise AssertionError("unsafe redirect was allowed")
    assert http.urls == ["https://example.com/start"]


def test_safe_fetch_rejects_oversized_response():
    http = _StreamHTTP([
        _StreamResponse(url="https://example.com/report", content=b"x" * 16),
    ])
    try:
        _run(research.fetch_bytes_safely(http, "https://example.com/report", max_bytes=8, resolve_dns=False))
    except ValueError as e:
        assert "too large" in str(e).lower()
    else:
        raise AssertionError("oversized response was allowed")


def test_effective_context_chars_scales_with_research_num_ctx(monkeypatch):
    import config

    budget5 = research._RESEARCH_DEPTH_BUDGETS[5]
    # Small window: evidence clamps well below the depth-5 table value.
    monkeypatch.setattr(config, "RESEARCH_NUM_CTX", 16384)
    clamped = research._effective_context_chars(budget5)
    assert 8000 <= clamped < budget5["context_chars"]
    # Large window: the depth budget table remains the upper bound.
    monkeypatch.setattr(config, "RESEARCH_NUM_CTX", 131072)
    assert research._effective_context_chars(budget5) == budget5["context_chars"]
    # Degenerate window never goes below the floor.
    monkeypatch.setattr(config, "RESEARCH_NUM_CTX", 1024)
    assert research._effective_context_chars(budget5) == 8000


def test_searxng_google_fallback_budget_caps_scrapes(monkeypatch):
    calls = []

    async def _fake_google(http, query, count):
        calls.append(query)
        return []

    class _FailHTTP:
        async def get(self, url, **kwargs):
            raise RuntimeError("searxng down")

    monkeypatch.setattr(research, "_search_google_fallback", _fake_google)
    state = {"remaining": 2}
    for i in range(4):
        _run(research._search_searxng(_FailHTTP(), "http://sx", f"query {i}", fallback_state=state))
    assert len(calls) == 2
    # Without a budget the legacy behavior is preserved.
    _run(research._search_searxng(_FailHTTP(), "http://sx", "query unbudgeted"))
    assert len(calls) == 3


def test_adaptive_followup_queries_respect_budget_and_dedupe():
    existing = {"local ai assistants overview"}
    queries = research._normalize_followup_queries(
        [
            "local ai assistants overview",
            "https://example.com/not-a-query",
            "unrelated whale migration",
            "local AI assistants primary source data",
            "local AI assistants benchmarks 2026",
        ],
        existing,
        "local AI assistants for small business",
        remaining=2,
    )
    assert queries == [
        "local AI assistants primary source data",
        "local AI assistants benchmarks 2026",
    ]


def test_per_report_lexical_evidence_retrieval_returns_source_linked_snippets():
    report_id = "research-test-evidence"
    records = [
        {
            "id": "a",
            "text": "Cloud assistants centralize documents but can reduce local maintenance.",
            "source_id": "S1",
            "source_index": 1,
            "title": "Cloud deployment guide",
            "url": "https://vendor.example/cloud",
            "kind": "source_brief",
            "credibility_score": 52,
        },
        {
            "id": "b",
            "text": "Local Ollama deployments keep private documents on-prem and need GPU capacity planning.",
            "source_id": "S2",
            "source_index": 2,
            "title": "Local AI architecture notes",
            "url": "https://docs.example/local-ai",
            "kind": "full_page",
            "credibility_score": 82,
        },
    ]
    research._stash_report_evidence_records(report_id, records)
    out = research._lexical_retrieve_report_evidence(report_id, ["Ollama local private documents GPU"], top_k=1)
    assert out
    assert out[0]["source_id"] == "S2"
    assert out[0]["url"] == "https://docs.example/local-ai"
    assert "Ollama" in out[0]["text"]


def test_citation_validation_rejects_invented_source_ids():
    sources = [{"index": 1, "url": "https://a.example"}, {"index": 2, "url": "https://b.example"}]
    audit = research._validate_report_citations("A supported claim [S1]. An invented one [S9].", sources)
    assert audit["valid"] is False
    assert audit["invalid"] == ["S9"]
    assert "S1" in audit["used"]


def test_credibility_scoring_and_source_ranking_prefers_stronger_sources():
    official = research._score_source_credibility("https://www.nasa.gov/report", "web", 2)
    weak = research._score_source_credibility("http://clickbait-example.xyz/login", "web", 2)
    assert official["score"] > weak["score"]
    assert any("HTTPS" in f or "government" in f for f in official["factors"])
    assert any("suspicious" in f or "plain HTTP" in f for f in weak["factors"])

    sources = research._normalize_report_sources([], [], [
        {"title": "Weak", "url": "http://clickbait-example.xyz/login", "content": "thin", "score": 100},
        {"title": "Official", "url": "https://www.nasa.gov/report", "content": "official data", "score": 1},
    ], {"target_sources": 2})
    assert sources[0]["title"] == "Official"
    assert sources[0]["credibility_score"] > sources[1]["credibility_score"]


def test_report_prompt_visual_guidance_mentions_supported_renderers():
    guidance = research._VISUAL_REPORT_GUIDANCE.lower()
    for term in ("katex", "mermaid", "chart", "pygraph", "callouts"):
        assert term in guidance


def test_mocked_final_markdown_contract_allows_citations_visuals_and_equations():
    markdown = """# Report

Claim with a valid source [S1].

```mermaid
flowchart LR
A-->B
```

```pygraph
{"type":"bar","labels":["A"],"data":[1]}
```

$$E = mc^2$$
"""
    citation = research._validate_report_citations(markdown, [{"index": 1, "url": "https://example.com"}])
    renderables = research._report_renderable_audit(markdown)
    assert citation["valid"] is True
    assert renderables["has_mermaid"] is True
    assert renderables["has_pygraph"] is True
    assert renderables["has_display_equation"] is True


def test_research_markdown_cleaner_strips_thinking_leaks():
    leaked = "Okay, I need to think this through.\n</think>\n\n# Clean Report\n\nBody [S1]."
    assert research._clean_research_model_text(leaked) == "# Clean Report\n\nBody [S1]."
    assert research._streamable_report_text(leaked) == "# Clean Report\n\nBody [S1]."
    assert research._streamable_report_text("Okay, still reasoning") == ""


def test_cloud_research_completion_routes_through_provider_adapter(monkeypatch):
    calls = []

    async def fake_complete_chat(http, model_id, prompt, **kwargs):
        calls.append((model_id, prompt, kwargs))
        return "<think>hidden</think>{\"ok\": true}"

    monkeypatch.setattr(research, "is_cloud_model", lambda m: str(m).startswith("openai:"))
    monkeypatch.setattr(research, "complete_chat", fake_complete_chat)

    text = _run(research._ask_ollama(None, "http://ollama", "Prompt", model="openai:gpt-test", max_tokens=123))
    assert text == '{"ok": true}'
    assert calls[0][0] == "openai:gpt-test"
    assert calls[0][2]["num_predict"] == 123

    obj = _run(research._ask_ollama_json(
        None, "http://ollama", "Return JSON", model="openai:gpt-test",
        fallback={}, expected_type=dict,
    ))
    assert obj == {"ok": True}
    assert calls[-1][2]["format_json"] is True


def test_streamed_report_output_filters_thinking_before_live_emit(monkeypatch):
    emitted = []

    async def fake_emit_report_event(events, report_id, event_type, data):
        emitted.append((report_id, event_type, data))

    monkeypatch.setattr(research, "_emit_report_event", fake_emit_report_event)

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            import json
            yield json.dumps({"response": "Reasoning text that should stay hidden. "})
            yield json.dumps({"response": "</think>\n\n# Clean Report\n\nVisible body."})
            yield json.dumps({"done": True})

    class _FakeHTTP:
        def stream(self, *_args, **_kwargs):
            return _FakeStream()

    text = _run(research._ask_report_streamed(
        _FakeHTTP(), "http://ollama", object(), "report-live", "prompt", model="qwen3moe:test",
    ))
    token_text = "".join(e[2].get("content", "") for e in emitted if e[1] == "research_token")
    assert text == "# Clean Report\n\nVisible body."
    assert token_text == text
    assert "reasoning" not in token_text.lower()
    assert "</think>" not in token_text


def test_pdf_export_uses_react_markdown_renderer_and_pygraph_alias():
    index = (_BACKEND.parent / "frontend" / "src" / "main.jsx").read_text()
    markdown_blocks = (_BACKEND.parent / "frontend" / "src" / "components" / "markdownBlocks.jsx").read_text()
    assert "ResearchPrintPage" in index
    # PDF export wraps the report markdown through the React renderer with
    # print-mode + theme args (repinned after the renderer gained options).
    assert re.search(r"<MDWrap>\{md\(bodyMd,\{printMode:true", index)
    assert 'lang==="chart"||lang==="pygraph"' in index
    assert 'looksLikeChartConfig(code)' in index
    assert 'function sanitizeMermaidCode' in markdown_blocks
    assert 'sanitizeMermaidCode(code)' in markdown_blocks
    assert "waitForResearchRender" in index
    assert "html2pdf().set" in index


def test_deep_research_panel_state_contracts_are_guarded_in_frontend():
    index = (_BACKEND.parent / "frontend" / "src" / "main.jsx").read_text()

    # Live SSE appends token chunks, while polling replaces from durable
    # report_markdown. That keeps the two paths from duplicating the body.
    assert "setResearchLiveMarkdown(p=>p+ev.data.content)" in index
    assert "if(d.report_markdown)setResearchLiveMarkdown(cleanResearchMarkdown(d.report_markdown||\"\"))" in index
    assert "cleanResearchMarkdown(researchLiveMarkdown||report?.report_markdown||\"\")" in index
    assert "isMoeModelName" in index
    assert "Cloud Research" in index
    assert "researchModelOptions" in index

    # All terminal states release the running flag.
    assert "if(ev.type===\"research_done\"||ev.type===\"research_error\")" in index
    assert "[\"complete\",\"failed\",\"cancelled\"].includes(String(d.status||\"\").toLowerCase())" in index
    assert "setResearchRunning(false);" in index

    # Rerun switches to the new id without overwriting an existing report row.
    assert "setActiveResearchId(d.id);setActiveResearch(d);setResearchEvents(d.events_log||[]);setResearchReports(p=>[d,...p.filter(x=>x.id!==d.id)])" in index

    # Deleting the active report clears active state and stops live UI state.
    assert "setActiveResearchId(null);setActiveResearch(null);setResearchEvents([]);setResearchLiveMarkdown(\"\");setResearchRunning(false);" in index

    # Labels stay distinct: panel is durable Deep Research, chat tool is Agent Research.
    assert "Deep Research" in index
    assert "Agent Research" in index
