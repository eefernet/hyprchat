"""
Quick search — shared helper for chat injection (`agents/chat.py`) and the
standalone `/api/quick-search` endpoint (`main.py`).

Pipeline (chat path): dispatches to `search_agent.run_search_agent`, which
runs skip-gate → triage (LLM, JSON-mode, returns standalone_query +
queries + category) → per-conversation subquery dedup → parallel SearXNG
(time_range="month" for news + recency cue) → heuristic rank + category
bias → embedding rerank/dedup against nomic-embed-text → relevance check
(prior-tokens-aware for follow-ups) → optional refine → trafilatura page
extraction + OG-image enrichment → context build.

This module hosts the shared helpers the agent reuses: skip-gate regexes,
content-token extraction, follow-up detection, bounded LRU SearXNG cache,
per-conversation subquery dedup, ranking + domain bias, embedding-batch
helper, fetch concurrency semaphore, SSRF guard, trafilatura page
extraction, OG-image enrichment, context builder, image proxy URL builder.
The agent itself lives in `search_agent.py`.

Standalone API path (`run_quick_search_for_api`) skips triage and page-fetch
and returns carousel-shaped data (with OG-image enrichment).
"""
import asyncio
import ipaddress
import math
import os
import re
import socket
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime

import config
from research import _search_searxng, _rank_urls


# ── 10-min TTL cache, keyed by (query, time_range), bounded LRU ──
_CACHE: "OrderedDict[tuple[str, str | None], tuple[float, list]]" = OrderedDict()
_CACHE_TTL = 600
_CACHE_MAX = 512

# Chat Quick Search is a broad source gatherer. Keep the final source list
# generous, but only fetch full page text for the highest-ranked few.
CHAT_MAX_RESULTS = 35
CHAT_SEARCH_COUNT_DEFAULT = 25
CHAT_SEARCH_COUNT_BROAD = 35
CHAT_RANK_POOL_SIZE = 105
CHAT_PAGE_FETCH_COUNT = 8
CHAT_CONTEXT_SNIPPET_CHARS = 280
CHAT_CONTEXT_PAGE_CHARS = 1200


# ── Concurrency cap on outbound HTTP fetches ──
# SearXNG fanout (3 queries) + page-fetch (up to 8) + OG-image-fetch (4) +
# refine-search (1) can hit bursts of outbound HTTPs against a
# single LXC instance routed through ProtonVPN. Cap page+OG fetches
# to smooth bursts. SearXNG already has internal 429 retry so we
# don't gate it here.
_FETCH_SEMA = asyncio.Semaphore(6)


# ── Per-conversation subquery dedup (Khoj pattern) ──
# When the user iterates on a topic, triage often re-emits queries already
# searched in earlier turns. Track them per-conversation so we skip the
# redundant SearXNG round-trip.
_PREV_SUBQUERIES: "OrderedDict[str, tuple[float, set[str]]]" = OrderedDict()
_PREV_SUBQUERIES_TTL = 3600  # 1 hour
_PREV_SUBQUERIES_MAX = 256


def _filter_novel_queries(conv_id: str, queries: list[str]) -> list[str]:
    """Return queries not seen in this conversation. Adds them to the set.

    Lowercase-normalized comparison. Always preserves at least one query —
    if every query is a duplicate, we still let the latest one through so
    the agent doesn't return empty-handed when the user repeats a question.
    """
    if not conv_id or not queries:
        return queries
    now = time.time()
    entry = _PREV_SUBQUERIES.get(conv_id)
    if entry and (now - entry[0]) < _PREV_SUBQUERIES_TTL:
        seen = entry[1]
    else:
        seen = set()
    novel: list[str] = []
    for q in queries:
        key = q.strip().lower()
        if key and key not in seen:
            novel.append(q)
            seen.add(key)
    if not novel:
        # All duplicates — let the first one through anyway. Caches downstream
        # will absorb the cost; the user gets fresh ranking against current
        # standalone_query.
        novel = [queries[0]]
    _PREV_SUBQUERIES[conv_id] = (now, seen)
    _PREV_SUBQUERIES.move_to_end(conv_id)
    while len(_PREV_SUBQUERIES) > _PREV_SUBQUERIES_MAX:
        _PREV_SUBQUERIES.popitem(last=False)
    return novel


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


def _rank_and_filter_for_chat(
    results: list, query: str = "", category: str | None = None,
    *, limit: int = CHAT_MAX_RESULTS,
) -> list:
    """For model context: drop YouTube/image, rank by quality, dedup, apply
    category bias against off-fit domains, keep up to `limit`.

    `category` should come from triage when available — it has full
    conversation context. Falls back to regex query-classification when
    not supplied (triage-failure path).
    """
    limit = max(1, int(limit or CHAT_MAX_RESULTS))
    text_only = [r for r in results if r.get("type", "web") not in ("youtube", "image")]
    ranked_urls = _rank_urls(text_only)
    by_url = {r.get("url"): r for r in text_only if r.get("url")}
    ranked = [by_url[u] for u in ranked_urls if u in by_url]
    seen = {r.get("url") for r in ranked}
    leftover = [r for r in text_only if r.get("url") not in seen]
    deduped = _dedupe_by_domain(ranked + leftover, max_per_domain=2)
    cat = category if category in _DOWNRANK_DOMAINS else _classify_query(query)
    biased = _apply_domain_bias(deduped, cat)
    return biased[:limit]


# ── Embedding-based rerank + dedup (Perplexica pattern) ──
# Batched embed call against Ollama's nomic-embed-text. We score every
# snippet against the standalone query (cosine sim, drop below 0.45),
# then drop near-duplicate pairs (sim > 0.85) — catches the SearXNG
# mirror problem where the same article comes back via 3 different
# domains with different titles.
#
# Falls back to the input order on any Ollama error so the pipeline
# is strictly best-effort over the legacy ranking.
_EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
_EMBED_QUERY_FLOOR = 0.45
_EMBED_DUP_THRESHOLD = 0.85


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def _ollama_embed_batch(http, ollama_url: str, texts: list[str]) -> list[list[float]] | None:
    """Single batched embed call. Returns list of embeddings (same order as
    input) or None on any failure. Tries the modern /api/embed endpoint
    first, falls back to per-prompt /api/embeddings calls if the batch
    endpoint is unavailable.
    """
    if not texts:
        return []
    try:
        r = await http.post(
            f"{ollama_url}/api/embed",
            json={"model": _EMBED_MODEL, "input": texts, "keep_alive": "10m"},
            timeout=15,
        )
        if r.status_code == 200:
            body = r.json()
            embs = body.get("embeddings")
            if isinstance(embs, list) and len(embs) == len(texts):
                return embs
    except Exception as e:
        print(f"[QS]   /api/embed failed, falling back: {type(e).__name__}: {e!r}")
    # Fallback: parallel per-prompt calls against the legacy endpoint
    try:
        results = await asyncio.gather(
            *[
                http.post(
                    f"{ollama_url}/api/embeddings",
                    json={"model": _EMBED_MODEL, "prompt": t, "keep_alive": "10m"},
                    timeout=10,
                )
                for t in texts
            ],
            return_exceptions=True,
        )
        embs: list[list[float]] = []
        for r in results:
            if isinstance(r, Exception):
                return None
            if r.status_code != 200:
                return None
            e = r.json().get("embedding")
            if not isinstance(e, list):
                return None
            embs.append(e)
        return embs
    except Exception as e:
        print(f"[QS]   /api/embeddings fallback failed: {type(e).__name__}: {e!r}")
        return None


async def _embed_score_and_dedup(
    http, ollama_url: str, query: str, results: list,
    *, limit: int | None = None, backfill: bool = False,
) -> list:
    """Score each result against `query`, drop sim < 0.45, dedup near-dups
    (sim > 0.85), sort by query-similarity desc.

    Returns the input list unchanged on any embed failure. When `limit` and
    `backfill` are set, refill from the ranked candidate pool after dedup so
    duplicate clusters do not shrink the visible source list unnecessarily.
    """
    if not results or not query:
        return results[:limit] if limit else results
    snippet_texts = [
        ((r.get("title") or "") + " " + (r.get("content") or r.get("snippet") or ""))[:600]
        for r in results
    ]
    embs = await _ollama_embed_batch(http, ollama_url, [query] + snippet_texts)
    if not embs or len(embs) != len(results) + 1:
        return results[:limit] if limit else results
    q_emb = embs[0]
    snip_embs = embs[1:]

    scored: list[tuple[float, int]] = []
    for i, e in enumerate(snip_embs):
        s = _cosine(q_emb, e)
        if s >= _EMBED_QUERY_FLOOR:
            scored.append((s, i))
    if not scored:
        # Everything scored below the floor — likely a tail-end query the
        # embed model isn't great at. Keep input order rather than zero out.
        print(f"[QS]   embed: all results below floor {_EMBED_QUERY_FLOOR}; preserving order")
        return results[:limit] if limit else results
    scored.sort(key=lambda x: -x[0])

    # Dedup: walk in score order, drop any item whose snippet embedding is
    # > _EMBED_DUP_THRESHOLD from a kept item.
    kept_indices: list[int] = []
    kept_set: set[int] = set()
    for score, idx in scored:
        is_dup = False
        for kept_i in kept_indices:
            if _cosine(snip_embs[idx], snip_embs[kept_i]) > _EMBED_DUP_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            kept_indices.append(idx)
            kept_set.add(idx)

    if backfill and limit and len(kept_indices) < limit:
        for idx in range(len(results)):
            if idx in kept_set:
                continue
            is_dup = False
            for kept_i in kept_indices:
                if _cosine(snip_embs[idx], snip_embs[kept_i]) > _EMBED_DUP_THRESHOLD:
                    is_dup = True
                    break
            if is_dup:
                continue
            kept_indices.append(idx)
            kept_set.add(idx)
            if len(kept_indices) >= limit:
                break

    out = [results[i] for i in kept_indices]
    if limit:
        out = out[:limit]
    print(f"[QS]   embed-rerank: {len(results)} → {len(out)} (top sim={scored[0][0]:.2f})")
    return out


# ── Selective page fetch when snippets are too thin to answer from ──
_MENU_RE = re.compile(
    r"\b(menu|navigation|sign in|subscribe|cookie|accept all)\b.*"
    r"\b(menu|navigation|sign in|subscribe|cookie|accept all)\b",
    re.IGNORECASE,
)

# Trafilatura is the de-facto standard Python article extractor. Optional —
# we fall back to the same regex strip _fetch_page uses if it's not installed
# or fails on a particular page.
try:
    import trafilatura  # type: ignore
    _HAS_TRAFILATURA = True
except Exception:
    _HAS_TRAFILATURA = False


def _looks_thin(snippet: str) -> bool:
    s = (snippet or "").strip()
    if len(s) < 120:
        return True
    return bool(_MENU_RE.search(s))


def _regex_strip_html(text: str) -> str:
    """Same strip logic as research._fetch_page — kept inline so we can run
    trafilatura first on the same HTML we already fetched, falling back here
    without paying for a second HTTP round-trip."""
    for tag in ["script", "style", "nav", "header", "footer", "aside", "noscript"]:
        text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<h[1-3][^>]*>(.*?)</h[1-3]>", r"\n## \1\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n• \1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    text = re.sub(r"-----BEGIN PGP [A-Z ]+-----.*?-----END PGP [A-Z ]+-----",
                  "[PGP block removed]", text, flags=re.DOTALL)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    return text


_FETCH_SKIP = ["youtube.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
               ".pdf", "linkedin.com", "tiktok.com",
               "snopes.com", "politifact.com", "factcheck.org", "leadstories.com",
               "fullfact.org", "mediabiasfactcheck.com"]


async def _fetch_clean_page(http, url: str) -> dict | None:
    """Single-fetch page extraction. Tries trafilatura on the response HTML;
    falls back to the same regex strip research._fetch_page uses, on the
    same already-fetched HTML (no double round-trip).

    Returns {"url", "content"} or None.
    """
    if any(s in url.lower() for s in _FETCH_SKIP):
        return None
    try:
        async with _FETCH_SEMA:
            r = await http.get(
                url, timeout=15, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
            )
        if r.status_code >= 400:
            return None
        ct = r.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct and "json" not in ct:
            return None
        html = r.text
    except Exception:
        return None

    if _HAS_TRAFILATURA:
        try:
            extracted = trafilatura.extract(
                html, include_comments=False, include_tables=True,
                favor_precision=True, no_fallback=False,
            )
            if extracted and len(extracted) >= 200:
                return {"url": url, "content": extracted[:6000]}
        except Exception as e:
            print(f"[QS]   trafilatura failed for {url}: {type(e).__name__}: {e!r}")

    text = _regex_strip_html(html)
    if len(text) < 200:
        return None
    return {"url": url, "content": text[:6000]}


async def _enrich_with_pages(http, results: list, top_n: int = 3) -> dict[str, str]:
    targets = [
        r["url"] for r in results[:top_n]
        if r.get("url")
        and _looks_thin(r.get("content") or r.get("snippet", ""))
        and _url_safe(r["url"])
    ]
    if not targets:
        return {}

    try:
        fetched = await asyncio.wait_for(
            asyncio.gather(*[_fetch_clean_page(http, u) for u in targets], return_exceptions=True),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        return {}
    out: dict[str, str] = {}
    for f in fetched:
        if isinstance(f, dict) and f.get("url") and f.get("content"):
            out[f["url"]] = f["content"][:1500]
    return out


# ── SSRF guard ──
# A malicious search result can redirect to a private/internal address. Resolve
# the hostname and reject if any answer is private, loopback, link-local, or
# multicast. Resolution failures also reject (safe default).
_DNS_CACHE: dict[str, tuple[float, bool]] = {}
_DNS_CACHE_TTL = 300  # 5 min — DNS rarely flips faster than that for our use


def _url_safe(url: str) -> bool:
    """True if URL hostname resolves to public, routable IPs only."""
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return False
    if not host:
        return False
    host = host.lower()

    # Reject literal private IP hostnames before DNS lookup
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_public(ip)
    except ValueError:
        pass  # not a literal IP — fall through to DNS

    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and (now - cached[0]) < _DNS_CACHE_TTL:
        return cached[1]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, socket.herror, OSError):
        _DNS_CACHE[host] = (now, False)
        return False
    safe = True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            safe = False
            break
        if not _ip_is_public(ip):
            safe = False
            break
    _DNS_CACHE[host] = (now, safe)
    return safe


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


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
        snippet = (r.get("content") or r.get("snippet") or "")[:CHAT_CONTEXT_SNIPPET_CHARS]
        body = page_text.get(url, "")
        lines.append(f"{i}. **{title}** — {domain}")
        lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   Snippet: {snippet}")
        if body:
            lines.append(f"   Page excerpt: {body[:CHAT_CONTEXT_PAGE_CHARS]}")
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
async def _cached_search(
    http, query: str, count: int = 10, time_range: str | None = None,
) -> list:
    """Cached SearXNG search. Cache key is (query, time_range) so news
    queries with time_range=month don't collide with evergreen searches
    of the same string.
    """
    now = time.time()
    key = (query, time_range)
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        if len(cached[1]) >= count:
            _CACHE.move_to_end(key)
            return cached[1]
    results = await _search_searxng(
        http, config.SEARXNG_URL, query, count=count,
        safesearch="0", time_range=time_range,
    )
    if results:
        _CACHE[key] = (now, results)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
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
    if not _url_safe(page_url):
        return ""
    try:
        async with _FETCH_SEMA:
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
