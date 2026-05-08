"""
Quick search — shared helper for chat injection (`agents/chat.py`) and the
standalone `/api/quick-search` endpoint (`main.py`).

Pipeline (chat path): dispatches to `search_agent.run_search_agent`, which
runs skip-gate → triage (LLM, JSON-mode) → parallel SearXNG → relevance
check → optional refine → page/OG-image enrichment → context build.

This module hosts the shared helpers the agent reuses (skip-gate regexes,
content-token extraction, follow-up detection, SearXNG cache, ranking +
domain bias, page fetch, OG-image enrichment, context builder, image
proxy URL builder). The agent itself lives in `search_agent.py`.

Standalone API path (`run_quick_search_for_api`) skips triage and page-fetch
and returns carousel-shaped data (with OG-image enrichment).
"""
import asyncio
import re
import time
import urllib.parse
from datetime import datetime

import config
from research import _search_searxng, _fetch_page, _rank_urls


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


# Pronouns / vague references that always need replacement with prior-turn nouns
_PRONOUN_RE = re.compile(
    r"\b(she|he|it|they|them|her|him|his|its|their|this|that|these|those)\b",
    re.IGNORECASE,
)
_VAGUE_REF_RE = re.compile(
    r"\b(the (one|version|issue|thing|topic|story|news|results?)|"
    r"that (one|stuff|thing|kind))\b",
    re.IGNORECASE,
)
# Common follow-up openers — bare questions that lean entirely on prior context
_FOLLOWUP_STARTERS_RE = re.compile(
    r"^\s*(any|more|whats?\s+about|hows?\s+about|whats?\s+new|whats?\s+next|"
    r"tell\s+me\s+more|continue|next|whos?|whose|wheres?)\b",
    re.IGNORECASE,
)

# Stopwords used for content-token extraction. Intentionally aggressive: we want
# only nouns/proper-nouns/distinctive verbs to count as "topic anchors".
_STOPWORDS = frozenset({
    "the","a","an","is","are","was","were","be","been","being","am",
    "do","does","did","done","doing",
    "have","has","had","having",
    "will","would","could","should","can","may","might","must","shall",
    "this","that","these","those","it","its","they","them","their",
    "i","me","my","mine","you","your","yours","we","us","our","ours",
    "he","she","him","her","his","hers",
    "and","or","but","so","if","of","to","in","on","at","by","for","from","with",
    "as","about","into","over","under","than","then","now","not","no","yes",
    "ok","okay","just","still","yet","also","too","very","really","quite",
    "what","whats","who","whos","when","where","wheres","why","how","hows","which","whose",
    "any","more","next","else","other","another","like",
    "tell","show","explain","describe","said","says","say",
})


def _content_tokens(text: str) -> set[str]:
    """Lowercased non-stopword content tokens from `text`.

    Includes 3+ char words (with naive singularization so `elections` matches
    `election`) plus 2-5 char ALL-CAPS acronyms (UK, US, EU, NASA) which would
    otherwise fall under the length floor and get dropped.
    """
    tokens: set[str] = set()
    for t in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text.lower()):
        if t in _STOPWORDS:
            continue
        if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
            t = t[:-1]
        tokens.add(t)
    for t in re.findall(r"\b[A-Z]{2,5}\b", text):
        tokens.add(t.lower())
    return tokens


def _needs_context(latest: str, prior_tokens: set[str] | None = None) -> bool:
    """True when `latest` is a follow-up that won't search well on its own.

    Triggers on pronouns, vague references ("the one"), follow-up openers
    ("whos winning?", "any updates?"), short messages (≤6 words), or
    definite-article anaphora ("the reform party" when "reform" was discussed
    earlier — the latest message is referring back to a prior topic).
    """
    if _PRONOUN_RE.search(latest) or _VAGUE_REF_RE.search(latest):
        return True
    if _FOLLOWUP_STARTERS_RE.match(latest):
        return True
    if len(re.findall(r"\b\w+\b", latest)) <= 6:
        return True
    # Anaphoric "the X" / "that X" / "this X" where X was discussed earlier.
    # Catches long messages like "what is the reform party and how does it
    # compare to..." where the noun is anchored in prior turns rather than
    # being a fresh topic introduction.
    if prior_tokens:
        for noun in re.findall(
            r"\b(?:the|that|this)\s+([a-zA-Z][a-zA-Z-]{2,})\b",
            latest, re.IGNORECASE,
        ):
            n = noun.lower()
            if n.endswith("s") and not n.endswith("ss") and len(n) > 3:
                n = n[:-1]
            if n in prior_tokens:
                return True
    return False


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


# ── Query classification → domain-aware ranking ──
# Each category maps to (a) keywords that detect it from the search query
# and (b) domains we want to demote when results in that category come back.
# We *demote*, not delete — sometimes a Stack Overflow link IS what someone
# wants for a political question (e.g. data analysis of polling) — they just
# shouldn't outrank actual news outlets.
_NEWS_RE = re.compile(
    r"\b(election|vote|votes|voter|poll|polls|polling|"
    r"president|prime\s+minister|\bpm\b|government|parliament|"
    r"congress|senate|cabinet|minister|"
    r"war|attack|protest|economy|inflation|recession|"
    r"news|breaking|recent|today|yesterday|"
    r"labour|conservative|democrat|republican|tory|tories|reform|liberal|"
    r"green\s+party|farage|starmer|biden|trump|harris|"
    r"\b20[2-3]\d\b|"
    r"died|killed|elected|resigned|appointed|sworn\s+in)\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(
    r"\b(function|method|variable|exception|stack\s+trace|traceback|"
    r"compile|debug|syntax\s+error|regex|pip\b|npm\b|cargo|docker|kubernetes|k8s|"
    r"python|javascript|typescript|rust|golang|kotlin|swift|ruby|"
    r"react|vue|angular|node\.?js|django|flask|rails|spring|fastapi|"
    r"github|gitlab|repo|commit|branch|merge|pull\s+request|"
    r"sql|postgres|mysql|sqlite|mongodb|redis|"
    r"endpoint|http\b|json|xml|yaml|"
    r"linux|ubuntu|bash|zsh|terminal|"
    r"\.py\b|\.js\b|\.ts\b|\.rs\b|\.cpp|\.java\b)\b",
    re.IGNORECASE,
)
_RECIPE_RE = re.compile(
    r"\b(recipe|cook|cooking|bake|baking|ingredient|calorie|"
    r"breakfast|lunch|dinner|dessert|salad|soup|sauce|"
    r"vegetarian|vegan|gluten[\s-]free|keto|paleo)\b",
    re.IGNORECASE,
)

_DOWNRANK_DOMAINS: dict[str, frozenset[str]] = {
    "news": frozenset({
        "stackoverflow.com", "stackexchange.com", "askubuntu.com",
        "serverfault.com", "superuser.com", "geeksforgeeks.org",
        "tutorialspoint.com", "w3schools.com", "github.com", "gitlab.com",
        "leetcode.com", "hackerrank.com", "freecodecamp.org",
    }),
    "code": frozenset({
        "cnn.com", "bbc.com", "bbc.co.uk", "foxnews.com", "msnbc.com",
        "nbcnews.com", "abcnews.go.com", "cbsnews.com",
        "nytimes.com", "washingtonpost.com", "bloomberg.com",
        "theguardian.com", "telegraph.co.uk", "dailymail.co.uk",
        "allrecipes.com", "foodnetwork.com", "epicurious.com",
    }),
    "recipe": frozenset({
        "stackoverflow.com", "github.com", "gitlab.com",
        "cnn.com", "bbc.com", "reuters.com", "nytimes.com",
    }),
    "general": frozenset(),
}


def _classify_query(q: str) -> str:
    """Rough query category for domain-aware ranking."""
    if _NEWS_RE.search(q):
        return "news"
    if _CODE_RE.search(q):
        return "code"
    if _RECIPE_RE.search(q):
        return "recipe"
    return "general"


def _apply_domain_bias(results: list, category: str) -> list:
    """Push downranked-for-category domains to the bottom; keep order otherwise."""
    bad = _DOWNRANK_DOMAINS.get(category) or frozenset()
    if not bad:
        return results
    keep, defer = [], []
    for r in results:
        d = _registrable_domain(r.get("url", ""))
        (defer if d in bad else keep).append(r)
    return keep + defer


def _rank_and_filter_for_chat(results: list, query: str = "") -> list:
    """For model context: drop YouTube/image, rank by quality, dedup, apply
    category bias against off-fit domains, keep top 6.
    """
    text_only = [r for r in results if r.get("type", "web") not in ("youtube", "image")]
    ranked_urls = _rank_urls(text_only)
    by_url = {r.get("url"): r for r in text_only if r.get("url")}
    ranked = [by_url[u] for u in ranked_urls if u in by_url]
    seen = {r.get("url") for r in ranked}
    leftover = [r for r in text_only if r.get("url") not in seen]
    deduped = _dedupe_by_domain(ranked + leftover, max_per_domain=2)
    biased = _apply_domain_bias(deduped, _classify_query(query))
    return biased[:6]


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
    *, default_model: str = "", chat_model: str = "",
) -> dict:
    """Used by `agents/chat.py` to inject fresh search context.

    Dispatches to `search_agent.run_search_agent` for the full multi-round
    pipeline (skip-gate → triage → parallel SearXNG → relevance check →
    optional refine → fetch → context build).

    Triage model selection: explicit `QUICK_SEARCH_TRIAGE_MODEL` override,
    otherwise the small workspace model (fast — ~1-2s per call vs 10-30s on a
    27B chat model — and on multi-GPU / sufficient-VRAM setups it stays
    co-resident with the chat model so there's no swap penalty), otherwise
    the chat model, otherwise the default.

    Returns: {"context": str, "rewritten_query": str, "skipped": bool, "reason": str}
    """
    triage_model = (
        getattr(config, "QUICK_SEARCH_TRIAGE_MODEL", "")
        or workspace_model
        or chat_model
        or default_model
    )
    # Lazy import to avoid circular dependency — search_agent imports helpers
    # from this module.
    from search_agent import run_search_agent
    return await run_search_agent(
        http, ollama_url, triage_model, triage_model,
        events, conv_id, messages,
        default_model=default_model or workspace_model,
    )


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
