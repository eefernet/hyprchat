"""
Quick search — shared helper for chat injection (`agents/chat.py`) and the
standalone `/api/quick-search` endpoint (`main.py`).

Pipeline (chat path): dispatches to `search_agent.run_search_agent`, which
runs skip-gate → deterministic SearchPlan → per-conversation subquery dedup
→ parallel SearXNG fanout with native filters → heuristic rank/freshness/
domain diversity → optional time-bounded embedding rerank → selective page
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
from datetime import date, datetime
from email.utils import parsedate_to_datetime

import config
from research import _search_searxng, _rank_urls, fetch_bytes_safely


# ── 10-min TTL cache, keyed by (query, time_range, categories, engines), bounded LRU ──
_CACHE: "OrderedDict[tuple[str, str | None, str, str], tuple[float, list]]" = OrderedDict()
_CACHE_TTL = 600
_CACHE_MAX = 512

def _clamp_int(value, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = fallback
    return max(lo, min(hi, n))


# Chat Quick Search is a broad source gatherer. Defaults target 20-25 sources,
# with explicit min/max clamps for mode-specific overrides.
CHAT_MIN_RESULTS = _clamp_int(getattr(config, "QUICK_SEARCH_MIN_RESULTS", 10), 10, 1, 35)
CHAT_MAX_RESULTS = _clamp_int(getattr(config, "QUICK_SEARCH_MAX_RESULTS", 35), 35, CHAT_MIN_RESULTS, 50)
CHAT_TARGET_RESULTS = _clamp_int(
    getattr(config, "QUICK_SEARCH_TARGET_RESULTS", 24), 24, CHAT_MIN_RESULTS, CHAT_MAX_RESULTS,
)
CHAT_SEARCH_COUNT_DEFAULT = CHAT_TARGET_RESULTS
CHAT_SEARCH_COUNT_BROAD = CHAT_MAX_RESULTS
CHAT_RANK_POOL_SIZE = max(CHAT_MAX_RESULTS * 3, CHAT_TARGET_RESULTS * 4)
CHAT_PAGE_FETCH_COUNT = _clamp_int(
    getattr(config, "QUICK_SEARCH_PAGE_READS_BALANCED", 8), 8, 0, 16,
)
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
        if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"}:
            return ".".join(parts[-3:])
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
# Category signals are split into STRONG (near-unambiguous on their own) and
# WEAK (common English words that only count when two distinct ones co-occur).
# This is what keeps "Taylor Swift new album" out of `code` (bare `swift`) and
# "who won the Celtics game last night" out of `game` (bare `game`).
_CODE_STRONG_RE = re.compile(
    r"\b(function|method|variable|exception|stack\s+trace|traceback|"
    r"compile|debug|syntax\s+error|regex|pip\b|npm\b|cargo|docker|kubernetes|k8s|"
    r"python|javascript|typescript|golang|kotlin|"
    r"vue|angular|node\.?js|django|flask|rails|fastapi|"
    r"github|gitlab|repo|commit|pull\s+request|"
    r"sql|postgres|mysql|sqlite|mongodb|redis|"
    r"endpoint|http\b|json|xml|yaml|"
    r"linux|ubuntu|bash|zsh|terminal|"
    r"\.py\b|\.js\b|\.ts\b|\.rs\b|\.cpp|\.java\b)\b",
    re.IGNORECASE,
)
_CODE_WEAK_RE = re.compile(
    r"\b(rust|swift|ruby|spring|react|branch|merge|"
    r"library|framework|package|api|backend|frontend|database|server|"
    r"class|module|script|bug|deploy)\b",
    re.IGNORECASE,
)
_RECIPE_RE = re.compile(
    r"\b(recipe|cook|cooking|bake|baking|ingredient|calorie|"
    r"breakfast|lunch|dinner|dessert|salad|soup|sauce|"
    r"vegetarian|vegan|gluten[\s-]free|keto|paleo)\b",
    re.IGNORECASE,
)
_GAME_STRONG_RE = re.compile(
    r"\b(?:gaming|gameplay|video\s+game|steam|xbox|playstation|nintendo|"
    r"wiki\.gg|fandom|walkthrough|speedrun|strategy\s+game|"
    r"patch\s+notes?|dlc|"
    r"eu4|hoi4|ck3|civ\s*[456]|bg3|rpg|mmo|fps|rts)\b",
    re.IGNORECASE,
)
_GAME_WEAK_RE = re.compile(
    r"\b(?:game|switch|campaign|quest|level|boss|build|"
    r"mods?|nerf|buff|meta)\b",
    re.IGNORECASE,
)
# Real-world sports questions share vocabulary with video games ("game",
# "match", "season") but want news treatment, not wiki.gg/fandom bias.
_SPORTS_LEAGUE_RE = re.compile(
    r"\b(?:nba|nfl|mlb|nhl|mls|wnba|ncaa|fifa\s+world\s+cup|uefa|"
    r"premier\s+league|la\s+liga|serie\s+a|bundesliga|champions\s+league|"
    r"world\s+cup|super\s+bowl|world\s+series|stanley\s+cup|playoffs?|"
    r"standings|grand\s+slam|wimbledon|formula\s+1|f1|nascar|olympics?)\b",
    re.IGNORECASE,
)
_SPORTS_CONTEXT_RE = re.compile(
    r"\b(?:game|match|series|season|finals?)\b[^.?!]{0,40}?"
    r"\b(?:won|win|wins|winning|lost|lose|beat|scored?|scores|schedule|tonight|last\s+night)\b"
    r"|\b(?:won|win|beat|scored?|scores)\b[^.?!]{0,40}?\b(?:game|match|series)s?\b",
    re.IGNORECASE,
)


def _distinct_hits(pattern: re.Pattern, text: str) -> int:
    """Count distinct alternatives a signal regex matched (plural-folded)."""
    hits: set[str] = set()
    for m in pattern.finditer(text or ""):
        tok = m.group(0).lower()
        if tok.endswith("s") and not tok.endswith("ss") and len(tok) > 3:
            tok = tok[:-1]
        hits.add(tok)
    return len(hits)


def _is_code_query(q: str) -> bool:
    return bool(_CODE_STRONG_RE.search(q)) or _distinct_hits(_CODE_WEAK_RE, q) >= 2


def _is_game_query(q: str) -> bool:
    return bool(_GAME_STRONG_RE.search(q)) or _distinct_hits(_GAME_WEAK_RE, q) >= 2


def _is_sports_query(q: str) -> bool:
    return bool(_SPORTS_LEAGUE_RE.search(q) or _SPORTS_CONTEXT_RE.search(q))
_GAME_RESULT_RE = re.compile(
    r"\b(?:game|gaming|gameplay|steam|wiki\.gg|fandom|walkthrough|campaign|"
    r"quest|level|boss|achievement|strategy|guide|dev\s+diar(?:y|ies)|"
    r"patch|dlc|mod|meta|speedrun)\b",
    re.IGNORECASE,
)
_GAME_SOURCE_DOMAINS = frozenset({
    "wiki.gg", "fandom.com", "steamcommunity.com", "reddit.com",
    "ign.com", "gamespot.com", "gamepressure.com", "polygon.com",
    "pcgamesn.com", "rockpapershotgun.com", "thegamer.com",
    "paradoxwikis.com", "paradoxplaza.com", "forum.paradoxplaza.com",
})
_GENERAL_REFERENCE_DOMAINS = frozenset({
    "wikipedia.org", "britannica.com", "history.com", "thoughtco.com",
    "worldhistory.org", "shutterstock.com", "istockphoto.com", "gettyimages.com",
})

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
    "game": frozenset({
        "cnn.com", "bbc.com", "bbc.co.uk", "reuters.com", "nytimes.com",
        "washingtonpost.com", "bloomberg.com", "shutterstock.com",
        "istockphoto.com", "gettyimages.com",
    }),
    "general": frozenset(),
}


def _classify_query(q: str) -> str:
    """Rough query category for domain-aware ranking."""
    if _is_sports_query(q) and not _GAME_STRONG_RE.search(q):
        return "news"
    if _NEWS_RE.search(q):
        return "news"
    if _is_code_query(q):
        return "code"
    if _RECIPE_RE.search(q):
        return "recipe"
    if _is_game_query(q):
        return "game"
    return "general"


_MAJOR_SOURCE_DOMAINS = frozenset({
    "apnews.com", "reuters.com", "bbc.com", "bbc.co.uk", "npr.org", "pbs.org",
    "nytimes.com", "washingtonpost.com", "theguardian.com", "bloomberg.com",
    "wsj.com", "latimes.com", "politico.com", "axios.com", "nbcnews.com",
    "abcnews.go.com", "cbsnews.com", "cnn.com", "theatlantic.com",
})
_LOW_SOURCE_DOMAINS = frozenset({
    "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com",
    "pinterest.com", "reddit.com", "quora.com", "medium.com",
})


def _parse_result_date(value) -> date | None:
    """Parse common SearXNG/news date forms to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    low = s.lower()
    today = datetime.now().date()
    if "hour ago" in low or "hours ago" in low or "minute ago" in low or "minutes ago" in low:
        return today
    if "yesterday" in low:
        return date.fromordinal(today.toordinal() - 1)
    m = re.search(r"\b(\d{1,2})\s+days?\s+ago\b", low)
    if m:
        return date.fromordinal(today.toordinal() - min(3650, int(m.group(1))))
    for candidate in (s, s.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except Exception:
            pass
    try:
        return parsedate_to_datetime(s).date()
    except Exception:
        pass
    for pattern, fmt in (
        (r"\b(\d{4}-\d{1,2}-\d{1,2})\b", "%Y-%m-%d"),
        (r"\b(\d{4}/\d{1,2}/\d{1,2})\b", "%Y/%m/%d"),
        (r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", "%B %d, %Y"),
        (r"\b([A-Z][a-z]{2} \d{1,2}, \d{4})\b", "%b %d, %Y"),
    ):
        m = re.search(pattern, s)
        if not m:
            continue
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except Exception:
            pass
    return None


def _result_published_date(result: dict) -> date | None:
    for key in ("published_date", "publishedDate", "date", "pubDate", "updated"):
        parsed = _parse_result_date(result.get(key))
        if parsed:
            return parsed
    return None


def _source_tier_for_domain(domain: str) -> tuple[str, float]:
    if not domain:
        return "unknown", 0.45
    if domain in _LOW_SOURCE_DOMAINS:
        return "low", 0.2
    if domain.endswith(".gov") or domain.endswith(".mil") or domain in _MAJOR_SOURCE_DOMAINS:
        return "primary", 1.0
    if domain.endswith(".edu") or domain.endswith(".org"):
        return "strong", 0.78
    return "standard", 0.58


def _freshness_fit(published: date | None, freshness_mode: str, resolved_date: str | None) -> tuple[str, float]:
    if freshness_mode in ("", "none", None):
        return "evergreen", 0.5
    if not published:
        return "unknown", 0.35
    today = datetime.now().date()
    target = _parse_result_date(resolved_date) if resolved_date else today
    if freshness_mode == "day":
        if target and published == target:
            return "same_day", 1.0
        delta = abs(((target or today) - published).days)
        if delta <= 2:
            return "near_day", 0.45
        return "stale", 0.05
    age = max(0, (today - published).days)
    if freshness_mode == "week":
        if age <= 7:
            return "fresh", 1.0
        if age <= 31:
            return "recent", 0.55
        return "stale", 0.1
    if freshness_mode == "month":
        if age <= 31:
            return "fresh", 1.0
        if age <= 90:
            return "recent", 0.55
        return "stale", 0.1
    return "unknown", 0.35


def _snippet_usefulness(result: dict) -> float:
    snippet = (result.get("content") or result.get("snippet") or "").strip()
    if not snippet:
        return 0.0
    if _MENU_RE.search(snippet):
        return 0.2
    if len(snippet) >= 180:
        return 1.0
    if len(snippet) >= 80:
        return 0.65
    return 0.35


def _lexical_relevance(query: str, result: dict) -> float:
    q_tokens = _content_tokens(query)
    if not q_tokens:
        return 0.5
    blob = " ".join([
        result.get("title") or "",
        result.get("content") or result.get("snippet") or "",
        _registrable_domain(result.get("url") or ""),
    ])
    r_tokens = _content_tokens(blob)
    if not r_tokens:
        return 0.0
    overlap = q_tokens & r_tokens
    return min(1.0, len(overlap) / max(1, len(q_tokens)))


def _plan_get(plan, key: str, default=None):
    if isinstance(plan, dict):
        return plan.get(key, default)
    return getattr(plan, key, default)


def _anchor_match_score(anchor_terms: list[str], text: str) -> float:
    if not anchor_terms:
        return 0.0
    low = (text or "").lower()
    best = 0.0
    for anchor in anchor_terms:
        cleaned = re.sub(r"\s+", " ", str(anchor or "").strip()).lower()
        if not cleaned:
            continue
        if re.search(rf"\b{re.escape(cleaned)}\b", low):
            best = max(best, 1.0)
            continue
        tokens = _content_tokens(cleaned)
        if not tokens:
            continue
        overlap = tokens & _content_tokens(low)
        if overlap:
            best = max(best, min(0.75, len(overlap) / len(tokens)))
    return best


def _rank_for_search_plan(
    results: list,
    query: str,
    plan=None,
    *,
    category: str | None = None,
    limit: int = CHAT_TARGET_RESULTS,
) -> list:
    """Rank candidates for answer grounding without making embeddings mandatory."""
    limit = max(1, min(CHAT_MAX_RESULTS, int(limit or CHAT_TARGET_RESULTS)))
    text_only = [r for r in results if isinstance(r, dict) and r.get("type", "web") not in ("youtube", "image")]
    if not text_only:
        return []

    cat = category or _plan_get(plan, "category") or _classify_query(query)
    ranked_urls = _rank_urls(text_only)
    rank_pos = {u: i for i, u in enumerate(ranked_urls)}
    freshness_mode = _plan_get(plan, "freshness_mode", "none")
    resolved_date = _plan_get(plan, "resolved_date", None)
    anchor_terms = _plan_get(plan, "anchor_terms", None) or []
    strict_day = freshness_mode == "day"

    scored: list[dict] = []
    for idx, r in enumerate(text_only):
        item = dict(r)
        url = item.get("url") or ""
        domain = _registrable_domain(url)
        published = _result_published_date(item)
        freshness, freshness_score = _freshness_fit(published, freshness_mode, resolved_date)
        source_tier, source_score = _source_tier_for_domain(domain)
        game_source = cat == "game" and domain in _GAME_SOURCE_DOMAINS
        game_blob = " ".join([
            item.get("title") or "",
            item.get("content") or item.get("snippet") or "",
            domain,
        ])
        game_terms = cat == "game" and bool(_GAME_RESULT_RE.search(game_blob))
        anchor_score = _anchor_match_score(anchor_terms, game_blob)
        if game_source:
            source_tier, source_score = ("community" if domain == "reddit.com" else "game"), max(source_score, 0.84)
        lexical = _lexical_relevance(query, item)
        snippet_score = _snippet_usefulness(item)
        base_rank = 1.0 - (rank_pos.get(url, idx) / max(1, len(text_only)))
        raw_score = item.get("score", 0)
        try:
            raw_score = min(1.0, max(0.0, float(raw_score) / 100.0))
        except (TypeError, ValueError):
            raw_score = 0.0

        if strict_day:
            score = (
                lexical * 0.34
                + freshness_score * 0.34
                + source_score * 0.14
                + snippet_score * 0.10
                + base_rank * 0.06
                + raw_score * 0.02
            )
        else:
            score = (
                lexical * 0.42
                + freshness_score * 0.20
                + source_score * 0.16
                + snippet_score * 0.12
                + base_rank * 0.07
                + raw_score * 0.03
            )
        if domain in (_DOWNRANK_DOMAINS.get(cat) or frozenset()):
            score *= 0.72
        if anchor_terms:
            if anchor_score > 0:
                score *= 1.0 + min(0.35, anchor_score * 0.35)
            elif cat in {"game", "code"}:
                score *= 0.58
        if cat == "game":
            if game_source or game_terms or anchor_score > 0:
                score *= 1.18
            else:
                score *= 0.42
            if domain in _GENERAL_REFERENCE_DOMAINS and not (game_terms or anchor_score > 0):
                score *= 0.55

        item["published_date"] = published.isoformat() if published else (item.get("published_date") or "")
        item["freshness"] = freshness
        item["source_tier"] = source_tier
        item["score"] = round(score * 100, 1)
        item["score_reason"] = (
            f"lexical={lexical:.2f}; freshness={freshness}; "
            f"source={source_tier}; snippet={snippet_score:.2f}; "
            f"anchor={anchor_score:.2f}"
        )
        scored.append(item)

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    diverse = _dedupe_by_domain(scored, max_per_domain=2)
    if len(diverse) < min(limit, len(scored)):
        used_urls = {r.get("url") for r in diverse}
        counts: dict[str, int] = {}
        for r in diverse:
            d = _registrable_domain(r.get("url", ""))
            counts[d] = counts.get(d, 0) + 1
        for r in scored:
            if r.get("url") in used_urls:
                continue
            d = _registrable_domain(r.get("url", ""))
            if counts.get(d, 0) >= 3:
                continue
            diverse.append(r)
            used_urls.add(r.get("url"))
            counts[d] = counts.get(d, 0) + 1
            if len(diverse) >= limit:
                break
    return diverse[:limit]


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
            status, headers, _final_url, body = await fetch_bytes_safely(
                http,
                url, timeout=15, max_bytes=2 * 1024 * 1024,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
            )
        if status >= 400:
            return None
        ct = headers.get("content-type", "")
        if "text" not in ct and "html" not in ct and "json" not in ct:
            return None
        m = re.search(r"charset=([^;\s]+)", ct, re.I)
        enc = (m.group(1) if m else "utf-8").strip("\"'")
        try:
            html = body.decode(enc, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
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


_PAGE_FETCH_DEADLINE = 8.0


async def _enrich_with_pages(http, results: list, top_n: int = 3) -> dict[str, str]:
    top_n = max(0, int(top_n or 0))
    targets: list[str] = []
    seen: set[str] = set()

    async def _add(url: str) -> None:
        if not url or url in seen or not await _url_safe(url):
            return
        targets.append(url)
        seen.add(url)

    # Always read the first few text pages, then spend the rest of the budget
    # on thin snippets where page text is most likely to change answer quality.
    for r in results[:min(3, top_n)]:
        await _add(r.get("url") or "")
    for r in results[:max(top_n * 2, top_n)]:
        if len(targets) >= top_n:
            break
        if _looks_thin(r.get("content") or r.get("snippet", "")):
            await _add(r.get("url") or "")
    if not targets:
        return {}

    # asyncio.wait (not wait_for+gather) so one slow site can't discard the
    # page text of fetches that already completed within the deadline.
    tasks = [asyncio.ensure_future(_fetch_clean_page(http, u)) for u in targets]
    done, pending = await asyncio.wait(tasks, timeout=_PAGE_FETCH_DEADLINE)
    for t in pending:
        t.cancel()
    out: dict[str, str] = {}
    for t in done:
        try:
            f = t.result()
        except Exception:
            continue
        if isinstance(f, dict) and f.get("url") and f.get("content"):
            out[f["url"]] = f["content"][:1500]
    return out


# ── SSRF guard ──
# A malicious search result can redirect to a private/internal address. Resolve
# the hostname and reject if any answer is private, loopback, link-local, or
# multicast. Resolution failures also reject (safe default).
_DNS_CACHE: dict[str, tuple[float, bool]] = {}
_DNS_CACHE_TTL = 300  # 5 min — DNS rarely flips faster than that for our use


async def _url_safe(url: str) -> bool:
    """True if URL hostname resolves to public, routable IPs only.

    Async: getaddrinfo runs in a thread so a slow/unresponsive DNS server
    can't stall the (single-worker) event loop.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
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
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
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
def _format_now() -> str:
    try:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _has_same_day_text_evidence(text: str, target: date) -> bool:
    if not text:
        return False
    low = text.lower()
    markers = {
        target.isoformat().lower(),
        f"{target.strftime('%B')} {target.day}, {target.year}".lower(),
        f"{target.strftime('%B')} {target.day} {target.year}".lower(),
        f"{target.strftime('%b')} {target.day}, {target.year}".lower(),
        f"{target.strftime('%b')} {target.day} {target.year}".lower(),
    }
    if any(marker in low for marker in markers):
        return True
    if re.search(
        r"\b(today|this morning|this afternoon|this evening|"
        r"\d+\s+(?:minutes?|hours?)\s+ago)\b",
        low,
    ):
        return True
    return str(target.year) in low and bool(re.search(r"\blive updates?\b", low))


def _strict_day_evidence(results: list, plan=None, page_text: dict[str, str] | None = None) -> bool:
    if _plan_get(plan, "freshness_mode", "none") != "day":
        return True
    resolved = _plan_get(plan, "resolved_date", None)
    target = _parse_result_date(resolved)
    if not target:
        return True
    for r in results:
        published = _result_published_date(r)
        if published and published == target:
            return True
        url = r.get("url") or ""
        result_text = " ".join([
            r.get("title") or "",
            r.get("content") or r.get("snippet") or "",
            page_text.get(url, "") if page_text else "",
        ])
        if _has_same_day_text_evidence(result_text, target):
            return True
    return False


def _build_context(
    results: list,
    query: str,
    page_text: dict[str, str],
    allowed_image_urls: set[str],
    plan=None,
    *,
    low_relevance: bool = False,
) -> str:
    now = _format_now()
    resolved = _plan_get(plan, "resolved_date", "") or "none"
    freshness_mode = _plan_get(plan, "freshness_mode", "none") or "none"
    queries = _plan_get(plan, "queries", None) or [query]
    source_mode = _plan_get(plan, "source_mode", "searxng") or "searxng"
    search_provider = _plan_get(plan, "search_provider", "searxng") or "searxng"
    scraper_provider = _plan_get(plan, "scraper_provider", "local") or "local"
    reranker_type = _plan_get(plan, "reranker_type", "none") or "none"
    searxng_engines = _plan_get(plan, "searxng_engines", "") or ""
    lines = [
        "=== WEB SEARCH GROUNDING ===",
        f"Current date/time: {now}",
        f"Canonical question: {query}",
        f"Resolved date: {resolved}",
        f"Freshness mode: {freshness_mode}",
        f"Search backend: {source_mode}",
        f"Search provider: {search_provider}",
        f"Scraper: {scraper_provider}",
        f"Reranker: {reranker_type}",
        "Queries used: " + "; ".join(str(q) for q in queries),
        "",
    ]
    if searxng_engines:
        lines.insert(-1, f"SearXNG engines: {searxng_engines}")
    if freshness_mode == "day" and not _strict_day_evidence(results, plan, page_text):
        lines += [
            "FRESHNESS WARNING:",
            f"- These sources do not prove activity on {resolved}.",
            "- If the user asked what happened on that date, say the search only found older or undated evidence.",
            "",
        ]
    if low_relevance:
        lines += [
            "RELEVANCE WARNING:",
            "- These sources have weak topical overlap with the user's question and may be off-topic.",
            "- Only use a source that clearly addresses the question; otherwise say the search did not find a direct answer.",
            "",
        ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "")[:200]
        url = r.get("url") or ""
        domain = _registrable_domain(url)
        snippet = (r.get("content") or r.get("snippet") or "")[:CHAT_CONTEXT_SNIPPET_CHARS]
        body = page_text.get(url, "")
        lines.append(f"{i}. **{title}** — {domain}")
        lines.append(f"   URL: {url}")
        if r.get("published_date"):
            lines.append(f"   Published: {r.get('published_date')}")
        if r.get("freshness") or r.get("source_tier") or r.get("score") is not None:
            lines.append(
                "   Fit: "
                f"freshness={r.get('freshness') or 'unknown'}, "
                f"source_tier={r.get('source_tier') or 'unknown'}, "
                f"score={r.get('score', '')}"
            )
        if r.get("score_reason"):
            lines.append(f"   Score reason: {r.get('score_reason')}")
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
              "- If the results don't contain the answer, say so plainly; don't guess.",
              "- For strict date questions, only claim activity on the resolved date when a source proves that date."]
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


def _build_unavailable_context(query: str, plan=None, reason: str = "search unavailable") -> str:
    resolved = _plan_get(plan, "resolved_date", "") or "none"
    freshness_mode = _plan_get(plan, "freshness_mode", "none") or "none"
    queries = _plan_get(plan, "queries", None) or [query]
    source_mode = _plan_get(plan, "source_mode", "searxng") or "searxng"
    return "\n".join([
        "=== WEB SEARCH UNAVAILABLE ===",
        f"Current date/time: {_format_now()}",
        f"Canonical question: {query}",
        f"Resolved date: {resolved}",
        f"Freshness mode: {freshness_mode}",
        f"Search backend: {source_mode}",
        "Queries attempted: " + "; ".join(str(q) for q in queries),
        f"Reason: {reason}",
        "",
        "INSTRUCTIONS:",
        "- Web search did not provide usable sources for this turn.",
        "- If the user asked about current events, recent news, or a specific date, say you cannot verify it from fresh sources.",
        "- Do not answer current facts from memory as if they were verified.",
    ])


# ── Cached SearXNG (safesearch=0 per project config) ──
async def _cached_search(
    http, query: str, count: int = 10, time_range: str | None = None,
    categories: str = "general", engines: str | None = None,
) -> list:
    """Cached SearXNG search. Cache key includes native routing filters so
    current/news/engine-specific searches don't collide with evergreen results
    of the same query string.
    """
    now = time.time()
    key = (query, time_range, categories or "general", engines or "")
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        if len(cached[1]) >= count:
            _CACHE.move_to_end(key)
            return cached[1]
    results = await _search_searxng(
        http, config.SEARXNG_URL, query, count=count,
        categories=categories or "general", safesearch="0", time_range=time_range,
        engines=engines,
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
    if not await _url_safe(page_url):
        return ""
    try:
        async with _FETCH_SEMA:
            status, _headers, _final_url, body = await fetch_bytes_safely(
                http,
                page_url, timeout=6, max_bytes=256 * 1024,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )
        if status >= 400:
            return ""
        html = body.decode("utf-8", errors="replace")[:30000]
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
    *, default_model: str = "", chat_model: str = "", context_hint: str = "",
) -> dict:
    """Used by `agents/chat.py` to inject fresh search context.

    Dispatches to `search_agent.run_search_agent`, which runs the deterministic
    plan immediately and (in balanced/quality modes) a small-LLM query planner
    in parallel with the first search wave. The model parameter feeds that
    planner and the low-relevance refinement round.

    Returns: {"context": str, "rewritten_query": str, "skipped": bool, "reason": str}
    """
    # Lazy import to avoid circular dependency — search_agent imports helpers
    # from this module.
    import model_providers
    from search_agent import run_search_agent
    # Planner/refine calls go to Ollama; never inherit a cloud-prefixed model
    # (it would 404). Cloud is honored only via an explicit triage model.
    planner_model = (
        getattr(config, "QUICK_SEARCH_TRIAGE_MODEL", "")
        or model_providers.reject_cloud(workspace_model)
        or model_providers.reject_cloud(chat_model)
        or model_providers.reject_cloud(default_model)
    )
    return await run_search_agent(
        http, ollama_url, planner_model,
        events, conv_id, messages,
        default_model=default_model or workspace_model,
        context_hint=context_hint,
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
        published = _result_published_date(r)
        domain = _registrable_domain(r.get("url", ""))
        tier, _ = _source_tier_for_domain(domain)
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "")[:300],
            "thumbnail": r.get("thumbnail", ""),
            "engine": r.get("engine", ""),
            "type": r.get("type", "web"),
            "published_date": published.isoformat() if published else (r.get("published_date") or ""),
            "freshness": "unknown",
            "source_tier": tier,
            "score": r.get("score", 0),
            "score_reason": "standalone quick search result",
            "query_origin": query,
        })

    await _enrich_og_images(http, out)
    return {"results": out, "query": query}
