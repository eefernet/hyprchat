"""
Quick search — shared helper for chat injection (`agents/chat.py`) and the
standalone `/api/quick-search` endpoint (`main.py`).

Pipeline (chat path):
  skip-gate → query-rewrite (WORKSPACE_MODEL) → SearXNG (safesearch=0) →
  rank/dedup → selective page fetch → build context with proxied image URLs.

Standalone API path skips rewrite/page-fetch and returns carousel-shaped data
(with OG-image enrichment for missing thumbnails).
"""
import asyncio
import re
import time
import urllib.parse
from datetime import datetime

import config
from research import _search_searxng, _fetch_page, _rank_urls, _ask_ollama


# ── 10-min TTL cache, keyed by query string ──
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 600


# ── Skip-gate: only skip when search is clearly pointless ──
# Bias is strongly toward searching — a useless search wastes ~2s of latency,
# but a wrong skip means the model hallucinates instead of getting help.
_GREETING_RE = re.compile(
    r"^(hi+|hello|hey+|yo|sup|thanks?|thank you|ty|ok(ay)?|cool|nice|great|"
    r"got it|sure|yes|no+|yeah|nope|lol|haha|hm+|nvm|never mind)[!.?\s]*$",
    re.IGNORECASE,
)
# Pure-arithmetic only — no letters, short, no question words.
# Avoids skipping things like "what's the integral of x^2" or "largest prime under 10^18".
_PURE_ARITH_RE = re.compile(r"^[\d\s+\-*/().=^%]+\??$")
# Operate-on-attached-text: "rewrite/translate/summarize THIS/THE [content]".
# Narrow form — won't match "rewrite my API to use websockets" (which may need docs).
_OP_ON_ATTACHED_RE = re.compile(
    r"^(rewrite|translate|summari[sz]e|paraphrase|reword|proofread)\s+"
    r"(this|that|the (following|text|paragraph|message)|it)\b",
    re.IGNORECASE,
)


def _should_skip(query: str) -> tuple[bool, str]:
    q = (query or "").strip()
    if not q or not re.search(r"[a-z0-9]", q, re.I):
        return True, "empty"
    if _GREETING_RE.match(q):
        return True, "greeting"
    if len(q) < 3:
        return True, "too short"
    # Pure arithmetic (no letters at all) — model can compute these directly.
    if len(q) < 80 and not re.search(r"[a-zA-Z]", q) and _PURE_ARITH_RE.match(q):
        return True, "arithmetic"
    if _OP_ON_ATTACHED_RE.match(q):
        return True, "operate on attached text"
    return False, ""


# ── Query rewrite via small/fast workspace model ──
async def _try_rewrite_call(http, ollama_url: str, prompt: str, model: str, timeout: float) -> str:
    """Single rewrite attempt. Returns sanitized output or empty string on any failure."""
    try:
        out = await asyncio.wait_for(
            _ask_ollama(http, ollama_url, prompt, model=model, max_tokens=40),
            timeout=timeout,
        )
    except Exception:
        return ""
    if not out:
        return ""
    # `_ask_ollama` returns "[AI synthesis failed: ...]" on error — treat as empty
    if out.startswith("["):
        return ""
    out = re.sub(r"<think>[\s\S]*?</think>", "", out, flags=re.IGNORECASE).strip()
    if not out:
        return ""
    out = out.splitlines()[0].strip().strip('"').strip("'").rstrip(".")
    if (
        not out
        or len(out) > 200
        or out.lower().startswith(("here", "the query", "search:", "query:", "rewritten", "i ", "i'", "as "))
        or "http" in out.lower()
    ):
        return ""
    return out


async def _rewrite_query(
    http, ollama_url: str, workspace_model: str, default_model: str,
    messages: list, latest: str,
) -> str:
    """Rewrite latest user message into a focused search query using last few turns.
    Tries WORKSPACE_MODEL first (fast), falls back to DEFAULT_MODEL if that fails
    (e.g. workspace model not installed). Returns raw `latest` on total failure.
    """
    fallback = latest[:500]
    turns: list[str] = []
    for m in messages[-6:]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if content == latest:
            continue
        turns.append(f"{role}: {content[:300]}")
    if not turns:
        # No conversation history — nothing to expand. Skip the rewrite call.
        return fallback
    prompt = (
        "You are a search query generator. Rewrite the user's latest message "
        "into a complete, search-engine-friendly query.\n\n"
        "CRITICAL RULE: If the latest message contains a pronoun "
        "(she, he, it, they, this, that, these, those) or a vague reference "
        "(the one, that thing, the version, the issue), you MUST replace it "
        "with the specific noun from the prior turns. Pronouns are NEVER "
        "acceptable in the output.\n\n"
        "Examples:\n"
        "  Conversation: user asked 'who is taylor swift?'\n"
        "  Latest: 'what does she look like?'\n"
        "  Output: Taylor Swift photos appearance\n\n"
        "  Conversation: user asked 'what is React 19?'\n"
        "  Latest: 'what about for v18?'\n"
        "  Output: React v18 features release\n\n"
        "  Conversation: discussion about Rust borrow checker\n"
        "  Latest: 'how do I fix it?'\n"
        "  Output: Rust borrow checker error fix\n\n"
        "Output ONE line only, max 12 words, no quotes, no preamble, "
        "no explanation, no 'Output:' prefix.\n\n"
        "Recent turns:\n" + "\n".join(turns[-4:]) + "\n\n"
        f"Latest: {latest}\n"
        "Rewritten query:"
    )

    # If the latest message has a pronoun and the rewriter doesn't expand it,
    # the rewrite is useless — retry with the bigger model.
    has_pronoun = bool(re.search(
        r"\b(she|he|it|they|them|her|him|its|their|this|that|these|those)\b",
        latest, re.IGNORECASE,
    ))

    def _good(rewritten: str) -> bool:
        if not rewritten:
            return False
        # Same as input → useless rewrite.
        if rewritten.strip().lower() == latest.strip().lower():
            return False
        # Pronoun didn't get resolved.
        if has_pronoun and re.search(
            r"\b(she|he|it|they|them|her|him|its|their)\b",
            rewritten, re.IGNORECASE,
        ):
            return False
        return True

    out = await _try_rewrite_call(http, ollama_url, prompt, workspace_model, timeout=8.0)
    if _good(out):
        print(f"[QS]   rewrite via {workspace_model!r}: {latest[:60]!r} → {out!r}")
        return out

    # Workspace model failed or didn't resolve pronouns — try the default model.
    if default_model and default_model != workspace_model:
        why = "didn't expand pronouns" if (out and has_pronoun) else "returned empty"
        print(f"[QS]   rewrite via {workspace_model!r} {why}, retrying with {default_model!r}")
        out = await _try_rewrite_call(http, ollama_url, prompt, default_model, timeout=15.0)
        if _good(out):
            print(f"[QS]   rewrite via {default_model!r}: {latest[:60]!r} → {out!r}")
            return out

    print(f"[QS]   rewrite failed, using raw query: {latest[:80]!r}")
    return fallback


# ── Filtering / ranking / dedup ──
def _registrable_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        host = host.split(":", 1)[0].removeprefix("www.")
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return url


def _dedupe_by_domain(results: list, max_per_domain: int = 2) -> list:
    out = []
    counts: dict[str, int] = {}
    for r in results:
        d = _registrable_domain(r.get("url", ""))
        if counts.get(d, 0) >= max_per_domain:
            continue
        counts[d] = counts.get(d, 0) + 1
        out.append(r)
    return out


def _rank_and_filter_for_chat(results: list) -> list:
    """For model context: drop YouTube/image, rank by quality, dedup, keep top 6."""
    text_only = [r for r in results if r.get("type", "web") not in ("youtube", "image")]
    ranked_urls = _rank_urls(text_only)
    by_url = {r.get("url"): r for r in text_only if r.get("url")}
    ranked = [by_url[u] for u in ranked_urls if u in by_url]
    seen = {r.get("url") for r in ranked}
    leftover = [r for r in text_only if r.get("url") not in seen]
    return _dedupe_by_domain(ranked + leftover, max_per_domain=2)[:6]


# ── Selective page fetch when snippets are too thin to answer from ──
_MENU_RE = re.compile(
    r"\b(menu|navigation|sign in|subscribe|cookie|accept all)\b.*"
    r"\b(menu|navigation|sign in|subscribe|cookie|accept all)\b",
    re.IGNORECASE,
)


def _looks_thin(snippet: str) -> bool:
    s = (snippet or "").strip()
    if len(s) < 120:
        return True
    return bool(_MENU_RE.search(s))


async def _enrich_with_pages(http, results: list, top_n: int = 3) -> dict[str, str]:
    targets = [
        r["url"] for r in results[:top_n]
        if r.get("url") and _looks_thin(r.get("content") or r.get("snippet", ""))
    ]
    if not targets:
        return {}
    try:
        fetched = await asyncio.wait_for(
            asyncio.gather(*[_fetch_page(http, u) for u in targets], return_exceptions=True),
            timeout=4.0,
        )
    except asyncio.TimeoutError:
        return {}
    out: dict[str, str] = {}
    for f in fetched:
        if isinstance(f, dict) and f.get("url") and f.get("content"):
            out[f["url"]] = f["content"][:1500]
    return out


# ── Image proxy URL builder ──
def proxy_image_url(raw_url: str) -> str:
    """Wrap a third-party image URL in our /api/img-proxy endpoint."""
    return f"/api/img-proxy?u={urllib.parse.quote(raw_url, safe='')}"


# ── Context builder ──
def _build_context(results: list, query: str, page_text: dict[str, str], allowed_image_urls: set[str]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"=== WEB SEARCH (today: {today}) ===", f"Query: {query}", ""]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "")[:200]
        url = r.get("url") or ""
        domain = _registrable_domain(url)
        snippet = (r.get("content") or r.get("snippet") or "")[:500]
        body = page_text.get(url, snippet)
        lines.append(f"{i}. **{title}** — {domain}")
        lines.append(f"   URL: {url}")
        lines.append(f"   {body[:1500]}")
        thumb = r.get("thumbnail") or ""
        if thumb:
            proxied = proxy_image_url(thumb)
            if proxied in allowed_image_urls:
                lines.append(f"   [image: {proxied}]")
        lines.append("")
    has_images = bool(allowed_image_urls)
    lines += ["INSTRUCTIONS:",
              "- Answer using these results. Cite the URLs you actually used.",
              "- If the results don't contain the answer, say so plainly — don't guess."]
    if has_images:
        lines += [
            "- IMAGES: One of the [image: ...] URLs above MUST be embedded near the top",
            "  of your answer when the question is about a person, place, thing, product,",
            '  animal, vehicle, news event, or asks "who is X", "what is X", "what does',
            '  X look like", "show me X". Use this exact markdown:',
            "      ![short alt](image_url_from_above)",
            "  Use ONLY a URL from the [image: ...] tags above — do not invent URLs.",
            "  SKIP the image for code, math, abstract concepts, or pure-text explanations.",
        ]
    return "\n".join(lines)


# ── Cached SearXNG (safesearch=0 per project config) ──
async def _cached_search(http, query: str, count: int = 10) -> list:
    now = time.time()
    cached = _CACHE.get(query)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    results = await _search_searxng(
        http, config.SEARXNG_URL, query, count=count, safesearch="0",
    )
    if results:
        _CACHE[query] = (now, results)
    return results


# ── OG image enrichment for the carousel (preserves main.py:2729-2775 behavior) ──
_OG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    r'<meta[^>]+name=["\']twitter:image:src["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image:src["\']',
    r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image:secure_url["\']',
    r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
]
_OG_SKIP = ["youtube.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
            "linkedin.com", "tiktok.com", ".pdf"]


async def _fetch_og_image(http, page_url: str) -> str:
    if any(s in page_url.lower() for s in _OG_SKIP):
        return ""
    try:
        resp = await http.get(
            page_url, timeout=6, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )
        html = resp.text[:30000]
        for pattern in _OG_PATTERNS:
            m = re.search(pattern, html, re.IGNORECASE)
            if not m:
                continue
            img = m.group(1).strip()
            if img.startswith("//"):
                img = "https:" + img
            elif img.startswith("/"):
                parsed = urllib.parse.urlparse(page_url)
                img = f"{parsed.scheme}://{parsed.netloc}{img}"
            if img.startswith("http"):
                return img
        return ""
    except Exception:
        return ""


async def _enrich_og_images(http, results: list, max_fetch: int = 6) -> None:
    needs = [(i, r["url"]) for i, r in enumerate(results)
             if r.get("type") == "web" and not r.get("thumbnail") and r.get("url")]
    if not needs:
        return
    tasks = [_fetch_og_image(http, u) for _, u in needs[:max_fetch]]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)
    for (idx, _), img in zip(needs[:max_fetch], fetched):
        if isinstance(img, str) and img:
            results[idx]["thumbnail"] = img


# ============================================================
# Public entry points
# ============================================================

async def run_quick_search_for_chat(
    http, ollama_url: str, workspace_model: str, events, conv_id: str, messages: list,
    *, default_model: str = "",
) -> dict:
    """Used by `agents/chat.py` to inject fresh search context.

    Returns: {"context": str, "rewritten_query": str, "skipped": bool, "reason": str}
    """
    latest = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            latest = m["content"].strip()
            break
    if not latest:
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": "no user message"}

    skip, reason = _should_skip(latest)
    if skip:
        if events and conv_id:
            try:
                await events.emit(conv_id, "tool_done", {
                    "tool": "quick_search", "icon": "search",
                    "status": f"Skipped ({reason})",
                })
            except Exception:
                pass
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": reason}

    if events and conv_id:
        try:
            await events.emit(conv_id, "tool_start", {
                "tool": "quick_search",
                "status": f"Searching: {latest[:60]}",
                "icon": "search",
            })
        except Exception:
            pass

    rewritten = await _rewrite_query(
        http, ollama_url, workspace_model, default_model or workspace_model, messages, latest,
    )

    # Always show the actual search query in the UI — whether rewritten or raw.
    if events and conv_id:
        try:
            label = (
                f"→ {rewritten[:80]}" if rewritten.strip() != latest.strip()
                else f"(no rewrite) {rewritten[:80]}"
            )
            await events.emit(conv_id, "tool_progress", {
                "tool": "quick_search", "icon": "search", "status": label,
            })
        except Exception:
            pass

    raw = await _cached_search(http, rewritten)
    if not raw:
        if events and conv_id:
            try:
                await events.emit(conv_id, "tool_done", {
                    "tool": "quick_search", "icon": "search",
                    "status": "No results found",
                })
            except Exception:
                pass
        return {"context": "", "rewritten_query": rewritten, "skipped": False, "reason": "no results"}

    top = _rank_and_filter_for_chat(raw)

    # Page-fetch (for thin snippets) and OG-image enrichment run in parallel —
    # they hit the same top URLs but extract different signals. Without OG
    # enrichment, web-type SearXNG results often have no thumbnail and the
    # model has no images to embed.
    page_text, _ = await asyncio.gather(
        _enrich_with_pages(http, top, top_n=3),
        _enrich_og_images(http, top, max_fetch=4),
        return_exceptions=False,
    )

    # Top 2-3 image candidates (proxied) the model is allowed to embed
    allowed: set[str] = set()
    for r in top:
        thumb = r.get("thumbnail")
        if thumb and len(allowed) < 3:
            allowed.add(proxy_image_url(thumb))
    print(f"[QS]   image candidates: {len(allowed)} (of {sum(1 for r in top if r.get('thumbnail'))} thumbs)")

    ctx = _build_context(top, rewritten, page_text, allowed)

    if events and conv_id:
        try:
            await events.emit(conv_id, "tool_done", {
                "tool": "quick_search", "icon": "search",
                "status": f"Found {len(top)} result{'s' if len(top) != 1 else ''}",
            })
        except Exception:
            pass

    print(f"[QS]   {len(top)} results for {rewritten!r} (was {latest[:60]!r})")
    return {"context": ctx, "rewritten_query": rewritten, "skipped": False, "reason": ""}


async def run_quick_search_for_api(http, query: str, count: int = 6) -> dict:
    """Used by `/api/quick-search` for the frontend carousel.
    Skips query rewrite (no conversation context); preserves OG-image enrichment.

    Returns: {"results": [...], "query": query}
    """
    raw = await _cached_search(http, query, count=max(count + 4, 10))
    if not raw:
        return {"results": [], "query": query}

    deduped = _dedupe_by_domain(raw, max_per_domain=2)[:count]
    out = []
    for r in deduped:
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "")[:300],
            "thumbnail": r.get("thumbnail", ""),
            "engine": r.get("engine", ""),
            "type": r.get("type", "web"),
        })

    await _enrich_og_images(http, out)
    return {"results": out, "query": query}
