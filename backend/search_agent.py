"""
Multi-round search agent — replaces single-shot `_rewrite_query` + search +
fetch with an internal loop that:

  1. triage()        — one LLM call (JSON-mode) returns {needs_search, queries,
                        category}. Replaces today's three-stage rewrite chain.
  2. parallel search — 1-3 SearXNG calls fanned out at once.
  3. relevance check — cheap content-token overlap heuristic. No LLM.
  4. refine          — if relevance is low and rounds remain, ask the model
                        for ONE different query and search once more.
  5. fetch + context — same OG-image / page-text enrichment as legacy.

Falls back to legacy `_rewrite_query` on any triage failure — never a
regression vs today's behavior. Keeps the same return shape:
  {"context": str, "rewritten_query": str, "skipped": bool, "reason": str}

Reuses helpers from `quick_search`: `_content_tokens`, `_cached_search`,
`_rank_and_filter_for_chat`, `_enrich_with_pages`, `_enrich_og_images`,
`_build_context`, `proxy_image_url`, `_rewrite_query`.
"""
import asyncio
import json
import re
from typing import Any

import quick_search as _qs


_VALID_CATEGORIES = ("news", "code", "recipe", "general")
_REFINE_THRESHOLD_DEFAULT = 0.30


# ── JSON-mode Ollama call ──
async def _ask_ollama_json(
    http, ollama_url: str, prompt: str, model: str,
    max_tokens: int = 200, timeout: float = 45.0,
) -> dict | None:
    """Single Ollama call with format='json'. Returns parsed dict or None on
    any failure (network, non-JSON, empty).

    Default timeout 45s allows for cold-load on a large chat model — the
    triage stage is per-turn so worst case is one slow first turn after a
    long idle, then the model stays warm in Ollama's keep-alive.
    """
    raw = ""
    try:
        r = await asyncio.wait_for(
            http.post(f"{ollama_url}/api/generate", json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                # Reasoning-mode models (qwen3.5, deepseek-r1, etc.) emit a
                # <think> block; with format=json that block ends up in the
                # `thinking` field and `response` comes back empty. Disable
                # thinking for this short structured call — we want the JSON
                # directly, not chain-of-thought.
                "think": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": max_tokens,
                    # Cap KV-cache to what triage actually needs (prompt is
                    # ~3KB ≈ 1000 tokens, output is ≤240 tokens). Without
                    # this, Ollama allocates the model's default num_ctx
                    # (often 16-32K) which on a 4B can balloon to 15-20GB
                    # of VRAM and evict a co-resident chat model — turning
                    # ~1s triage into ~25s under contention.
                    "num_ctx": 4096,
                },
                "keep_alive": "10m",
            }, timeout=timeout),
            timeout=timeout + 2,
        )
    except Exception as e:
        # httpx errors often have empty str() — use repr so the cause is visible
        print(f"[SA]   ollama JSON call failed: {type(e).__name__}: {e!r}")
        return None
    try:
        body = r.json()
        raw = (body.get("response", "") or "").strip()
        # Fallback: some Ollama versions / model templates ignore think=False
        # and put structured output in the `thinking` field anyway. Read it
        # if response is empty.
        if not raw:
            raw = (body.get("thinking", "") or "").strip()
        if not raw:
            print(f"[SA]   ollama returned empty response and thinking (status={getattr(r, 'status_code', '?')})")
            return None
        # format=json should give us valid JSON, but some models still wrap
        # in code fences or trailing text. Strip a leading ```json fence and
        # take the first {...} block as a safety net.
        raw = re.sub(r"^```(?:json)?\s*", "", raw).rstrip("`").strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        return json.loads(raw)
    except Exception as e:
        print(f"[SA]   JSON parse failed: {type(e).__name__}: {e!r} (raw[:200]={raw[:200]!r})")
        return None


# ── Triage ──
def _build_triage_prompt(turns: list[str], latest: str) -> str:
    history_block = "\n".join(turns[-6:]) if turns else "(no prior turns — this is the first message)"
    return (
        "You are a search planner. Given the conversation and the user's LATEST "
        "message, decide whether to search the web and what to search for.\n\n"
        "Output a single JSON object with EXACTLY these fields:\n"
        '  "needs_search": boolean\n'
        '  "queries":      array of 1-3 self-contained search query strings\n'
        '  "category":     one of "news" | "code" | "recipe" | "general"\n'
        '  "reason":       short string, ≤80 chars (telemetry only)\n\n'
        "RULES:\n"
        "1. The latest message continues the conversation. Resolve pronouns "
        "(she/he/it/they/this/that), vague references (\"the one\", \"the issue\"), "
        "and definite-article anaphora (\"the X\" referring to a prior-mentioned "
        "topic) using the SPECIFIC noun from prior turns.\n"
        "2. Bare follow-ups — short messages without their own subject "
        "('whos winning?', 'any updates?', 'what next?') — inherit the topic "
        "from prior turns.\n"
        "3. Default to ONE query. Output TWO queries ONLY when the latest "
        "message explicitly compares two subjects with a comparison word "
        "('compare', 'vs', 'versus', 'difference between'). Mentioning two "
        "subjects without explicitly comparing them is still ONE query.\n"
        "4. Topic shifts — when the latest message clearly introduces a NEW "
        "subject unrelated to prior turns — ignore the prior turns and search "
        "only the new topic.\n"
        "5. Set needs_search=false for: greetings, pure arithmetic, requests "
        "to rewrite/translate/summarize/proofread attached text, and pure-"
        "opinion questions ('what do you think of X').\n"
        "6. Each query: ≤12 words, no quotes, no URLs, no 'site:', no booleans.\n\n"
        "EXAMPLES BELOW ARE TEMPLATES showing the rule pattern. The entities "
        "(sourdough, Rust, Brazil, etc.) are placeholders — DO NOT copy them "
        "into your output unless they actually appear in the user's conversation. "
        "Adapt the structure to the user's actual topic.\n\n"
        '  Pattern: pronoun anaphora\n'
        '    Prior: user asked about sourdough bread\n'
        '    Latest: "how do I store it?"\n'
        '    → {"needs_search":true,"queries":["sourdough bread storage"],'
        '"category":"recipe","reason":"pronoun it → sourdough bread"}\n\n'
        '  Pattern: bare follow-up inherits topic\n'
        '    Prior: user asked about Rust borrow checker\n'
        '    Latest: "any examples?"\n'
        '    → {"needs_search":true,"queries":["Rust borrow checker examples"],'
        '"category":"code","reason":"bare follow-up"}\n\n'
        '  Pattern: topic shift — ignore prior\n'
        '    Prior: extended discussion about Python async\n'
        '    Latest: "what is the capital of Brazil?"\n'
        '    → {"needs_search":true,"queries":["capital of Brazil"],'
        '"category":"general","reason":"topic shift"}\n\n'
        '  Pattern: skip greeting\n'
        '    Prior: (none)\n'
        '    Latest: "hi"\n'
        '    → {"needs_search":false,"queries":[],"category":"general","reason":"greeting"}\n\n'
        '  Pattern: skip operate-on-attached\n'
        '    Prior: discussing FastAPI deployment\n'
        '    Latest: "rewrite this paragraph: <paste>"\n'
        '    → {"needs_search":false,"queries":[],"category":"general","reason":"operate on attached"}\n\n'
        f"Recent conversation:\n{history_block}\n\n"
        f"Latest message: {latest}\n\n"
        "Output JSON only:"
    )


def _validate_triage(out: Any, latest: str, prior_tokens: set[str]) -> dict | None:
    """Return a sanitized triage dict or None if validation fails."""
    if not isinstance(out, dict):
        return None
    needs = out.get("needs_search")
    if not isinstance(needs, bool):
        return None
    raw_queries = out.get("queries")
    if not isinstance(raw_queries, list):
        return None
    queries: list[str] = []
    for q in raw_queries[:3]:
        if not isinstance(q, str):
            continue
        q = q.strip().strip('"').strip("'")
        if not q or len(q) > 200:
            continue
        if "http://" in q.lower() or "https://" in q.lower():
            continue
        if not _qs._content_tokens(q):
            # No real content — likely 'yes' or 'okay' hallucinated as a query
            continue
        queries.append(q)

    if needs and not queries:
        return None  # claimed needs_search but produced no usable queries
    if not needs:
        queries = []  # consistency: skip → empty

    # Follow-up validation: if latest message is anaphora / pronoun / very short,
    # at least one query must share a content token with prior turns. This is the
    # "carry topic forward" guarantee — matches today's _good() check.
    if needs and prior_tokens and _qs._needs_context(latest, prior_tokens):
        if not any(_qs._content_tokens(q) & prior_tokens for q in queries):
            print(f"[SA]   triage queries dropped prior topic: {queries!r}")
            return None

    cat = out.get("category", "general")
    if cat not in _VALID_CATEGORIES:
        cat = "general"

    reason = out.get("reason", "")
    if not isinstance(reason, str):
        reason = ""

    return {
        "needs_search": needs,
        "queries": queries,
        "category": cat,
        "reason": reason[:120],
    }


async def triage(
    http, ollama_url: str, model: str, messages: list, latest: str,
) -> dict | None:
    """Run the triage LLM call. Returns validated dict or None on any failure."""
    if not model:
        return None
    turns: list[str] = []
    for m in messages[-6:]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if content == latest:
            continue
        turns.append(f"{role}: {content[:300]}")
    prior_tokens: set[str] = set()
    for t in turns[-4:]:
        body = t.split(":", 1)[-1] if ":" in t else t
        prior_tokens |= _qs._content_tokens(body)

    prompt = _build_triage_prompt(turns, latest)
    raw = await _ask_ollama_json(http, ollama_url, prompt, model, max_tokens=240, timeout=45.0)
    if raw is None:
        return None
    return _validate_triage(raw, latest, prior_tokens)


# ── Relevance scoring (cheap heuristic) ──
def relevance_score(user_message: str, queries: list[str], top_results: list[dict]) -> float:
    """Fraction of original-message content tokens that appear in the
    concatenated titles+snippets of the top results. 0.0..1.0.

    Compares against the user's message, NOT the rewritten queries — if the
    rewrite was bad, we want to detect that the results miss what was asked.
    """
    user_tokens = _qs._content_tokens(user_message)
    if not user_tokens:
        # Nothing to match against → assume results are fine; don't trigger refine.
        return 1.0
    blob_parts: list[str] = []
    for r in top_results[:3]:
        blob_parts.append(r.get("title") or "")
        blob_parts.append(r.get("content") or r.get("snippet") or "")
    blob = " ".join(blob_parts)
    if not blob.strip():
        return 0.0
    result_tokens = _qs._content_tokens(blob)
    if not result_tokens:
        return 0.0
    overlap = user_tokens & result_tokens
    return len(overlap) / len(user_tokens)


# ── Refine ──
async def refine_query(
    http, ollama_url: str, model: str,
    original_message: str, tried_queries: list[str], weak_results: list[dict],
) -> str:
    """Ask the model for ONE different query given that prior queries returned
    off-topic results. Returns a single query string or empty on failure."""
    if not model:
        return ""
    samples = []
    for r in weak_results[:3]:
        title = (r.get("title") or "")[:120]
        snip = (r.get("content") or r.get("snippet") or "")[:200]
        samples.append(f"- {title}: {snip}")
    samples_block = "\n".join(samples) if samples else "(no results)"
    prompt = (
        "You wrote search queries that returned OFF-TOPIC results. Propose ONE "
        "different query that would better match the user's original question.\n\n"
        f"User's original question: {original_message}\n\n"
        f"Queries you tried: {tried_queries}\n\n"
        f"Off-topic results received:\n{samples_block}\n\n"
        "Output JSON: {\"query\": \"...\"}\n"
        "Rules: ≤12 words, no quotes, no URLs, must include the specific noun "
        "from the user's question. Output JSON only."
    )
    out = await _ask_ollama_json(http, ollama_url, prompt, model, max_tokens=80, timeout=30.0)
    if not isinstance(out, dict):
        return ""
    q = out.get("query", "")
    if not isinstance(q, str):
        return ""
    q = q.strip().strip('"').strip("'")
    if not q or len(q) > 200 or "http" in q.lower():
        return ""
    if not _qs._content_tokens(q):
        return ""
    return q


# ── Orchestrator ──
async def run_search_agent(
    http, ollama_url: str, triage_model: str, refine_model: str,
    events, conv_id: str, messages: list,
    *, default_model: str = "",
    max_rounds: int = 2,
    relevance_threshold: float = _REFINE_THRESHOLD_DEFAULT,
) -> dict:
    """Orchestrate skip → triage → multi-query search → relevance check →
    optional refine → page/OG enrichment → context build.

    Returns the same shape as `run_quick_search_for_chat`:
      {"context", "rewritten_query", "skipped", "reason"}

    On triage failure, falls back to legacy `_rewrite_query` path so the new
    architecture is strictly best-effort over today's behavior.
    """
    latest = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            latest = m["content"].strip()
            break
    if not latest:
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": "no user message"}

    skip, reason = _qs._should_skip(latest)
    if skip:
        await _emit(events, conv_id, "tool_done", {
            "tool": "quick_search", "icon": "search", "status": f"Skipped ({reason})",
        })
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": reason}

    await _emit(events, conv_id, "tool_start", {
        "tool": "quick_search", "icon": "search",
        "status": f"Searching: {latest[:60]}",
    })

    # ── Triage ──
    plan = await triage(http, ollama_url, triage_model, messages, latest)
    triage_ok = plan is not None
    if not triage_ok:
        # Triage failed (model down, JSON parse, or validation rejection).
        # Search the raw user message as a last resort — the relevance check
        # and refine path will still try to recover bad results.
        print(f"[SA]   triage failed; searching raw user message")
        plan = {
            "needs_search": True,
            "queries": [latest[:200]],
            "category": "general",
            "reason": "triage failed",
        }

    if not plan["needs_search"] or not plan["queries"]:
        await _emit(events, conv_id, "tool_done", {
            "tool": "quick_search", "icon": "search",
            "status": f"Skipped ({plan.get('reason') or 'not needed'})",
        })
        return {
            "context": "", "rewritten_query": "",
            "skipped": True, "reason": plan.get("reason") or "triage skip",
        }

    queries: list[str] = list(plan["queries"])
    category: str = plan["category"]

    progress_label = " | ".join(q[:40] for q in queries[:2])
    await _emit(events, conv_id, "tool_progress", {
        "tool": "quick_search", "icon": "search",
        "status": f"→ {progress_label[:120]}",
    })

    # ── Round 1: parallel search ──
    rounds_used = 1
    raw_lists = await asyncio.gather(
        *[_qs._cached_search(http, q) for q in queries],
        return_exceptions=True,
    )
    raw = _merge_unique([r for r in raw_lists if isinstance(r, list)])

    if not raw:
        await _emit(events, conv_id, "tool_done", {
            "tool": "quick_search", "icon": "search", "status": "No results found",
        })
        return {"context": "", "rewritten_query": queries[0],
                "skipped": False, "reason": "no results"}

    primary = queries[0]
    top = _qs._rank_and_filter_for_chat(raw, primary)
    score = relevance_score(latest, queries, top)
    print(f"[SA]   triage_ok={triage_ok} round={rounds_used} q={queries!r} relevance={score:.2f}")

    # ── Round 2: refine if relevance is low ──
    if (
        score < relevance_threshold
        and rounds_used < max_rounds
        and len(_qs._content_tokens(latest)) >= 3
    ):
        refined = await refine_query(http, ollama_url, refine_model, latest, queries, top)
        if refined and refined not in queries:
            await _emit(events, conv_id, "tool_progress", {
                "tool": "quick_search", "icon": "search",
                "status": f"Round 2: refining → {refined[:60]}",
            })
            try:
                more = await _qs._cached_search(http, refined)
            except Exception:
                more = []
            if more:
                raw = _merge_unique([raw, more])
                queries = queries + [refined]
                primary = refined  # the refined query is what found the answer
                top = _qs._rank_and_filter_for_chat(raw, primary)
                rounds_used = 2
                new_score = relevance_score(latest, queries, top)
                print(f"[SA]   round={rounds_used} refined={refined!r} relevance={new_score:.2f}")

    # ── Page-fetch + OG-image enrichment (parallel) ──
    page_text, _ = await asyncio.gather(
        _qs._enrich_with_pages(http, top, top_n=3),
        _qs._enrich_og_images(http, top, max_fetch=4),
        return_exceptions=False,
    )

    allowed: set[str] = set()
    for r in top:
        thumb = r.get("thumbnail")
        if thumb and len(allowed) < 3:
            allowed.add(_qs.proxy_image_url(thumb))

    # Feed the same results into the frontend's QUICK SEARCH carousel via SSE,
    # tagged with `source: "quick_search"` so the listener routes them to
    # `quickResults` (separate from the `research` tool's carousel). This
    # replaces the frontend's parallel `/api/quick-search` POST — the chat
    # and the carousel now see the same triage-rewritten results.
    carousel = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or r.get("snippet") or "")[:300],
            "thumbnail": r.get("thumbnail", ""),
            "engine": r.get("engine", ""),
            "type": r.get("type", "web"),
        }
        for r in top
    ]
    await _emit(events, conv_id, "search_results", {
        "query": primary,
        "results": carousel,
        "source": "quick_search",
    })

    ctx = _qs._build_context(top, primary, page_text, allowed)

    await _emit(events, conv_id, "tool_done", {
        "tool": "quick_search", "icon": "search",
        "status": f"Found {len(top)} result{'s' if len(top) != 1 else ''}"
                  f"{' (refined)' if rounds_used > 1 else ''}",
    })

    print(f"[SA]   {len(top)} results, category={category}, rounds={rounds_used}")
    return {"context": ctx, "rewritten_query": primary, "skipped": False, "reason": ""}


# ── Helpers ──
def _merge_unique(lists: list[list]) -> list:
    """Merge multiple result lists, dedup by URL, preserving first-seen order."""
    seen: set[str] = set()
    out: list = []
    for lst in lists:
        if not lst:
            continue
        for r in lst:
            url = r.get("url") if isinstance(r, dict) else None
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(r)
    return out


async def _emit(events, conv_id: str, evt: str, data: dict) -> None:
    if not (events and conv_id):
        return
    try:
        await events.emit(conv_id, evt, data)
    except Exception:
        pass
