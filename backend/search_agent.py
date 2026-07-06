"""
Quick Search answer-grounding agent.

Each chat turn builds a deterministic SearchPlan immediately and (in
balanced/quality modes) runs a small-LLM query planner in parallel with the
first SearXNG wave — the deterministic results are the guaranteed floor; the
LLM plan adds queries / reroutes category only when it lands within
QUICK_SEARCH_PLANNER_TIMEOUT and validates. Then: heuristic freshness/source
ranking, optional embedding rerank, a literal-query fallback plus an LLM
refine round when relevance is low, selective page reads, and prompt context
injection. Speed mode stays fully deterministic.
"""
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import config
import quick_search as _qs


_VALID_CATEGORIES = ("news", "code", "recipe", "game", "general")
_REFINE_THRESHOLD_DEFAULT = 0.30

@dataclass
class SearchModeConfig:
    mode: str
    min_results: int
    target_results: int
    max_results: int
    page_reads: int
    max_queries: int
    allow_refine: bool
    embed_rerank: bool


@dataclass
class SearchPlan:
    canonical_question: str
    queries: list[str]
    category: str
    anchor_terms: list[str]
    freshness_mode: str
    resolved_date: str | None
    source_mode: str
    search_provider: str
    scraper_provider: str
    reranker_type: str
    searxng_engines: str
    target_results: int
    min_results: int
    max_results: int
    page_reads: int
    time_range: str | None
    generated_at: str
    embed_rerank: bool


@dataclass
class SearchFrame:
    category: str | None
    anchor_terms: list[str]
    local_entities: list[str]
    confidence: float = 0.0


def _configured_search_provider() -> str:
    provider = (getattr(config, "QUICK_SEARCH_PROVIDER", "searxng") or "searxng").strip().lower()
    return provider if provider == "searxng" else "searxng"


def _configured_scraper_provider() -> str:
    scraper = (getattr(config, "QUICK_SEARCH_SCRAPER", "local") or "local").strip().lower()
    return scraper if scraper == "local" else "local"


def _configured_reranker_type() -> str:
    reranker = (getattr(config, "QUICK_SEARCH_RERANKER", "none") or "none").strip().lower()
    if reranker in ("ollama", "embedding", "embeddings"):
        return "ollama"
    if bool(getattr(config, "QUICK_SEARCH_EMBED_RERANK", False)):
        return "ollama"
    return "none"


def _mode_config() -> SearchModeConfig:
    mode = (getattr(config, "QUICK_SEARCH_MODE", "balanced") or "balanced").strip().lower()
    if mode not in ("speed", "balanced", "quality"):
        mode = "balanced"
    min_results = max(1, min(_qs.CHAT_MAX_RESULTS, int(getattr(config, "QUICK_SEARCH_MIN_RESULTS", 10))))
    configured_target = max(min_results, min(
        _qs.CHAT_MAX_RESULTS,
        int(getattr(config, "QUICK_SEARCH_TARGET_RESULTS", _qs.CHAT_TARGET_RESULTS)),
    ))
    reranker_type = _configured_reranker_type()
    embed_rerank = reranker_type == "ollama"
    if mode == "speed":
        speed_max = min(15, _qs.CHAT_MAX_RESULTS)
        speed_min = min(min_results, speed_max)
        target = max(speed_min, min(speed_max, configured_target))
        return SearchModeConfig(mode, speed_min, target, speed_max, 3, 2, False, False)
    if mode == "quality":
        return SearchModeConfig(
            mode, min_results, _qs.CHAT_MAX_RESULTS, _qs.CHAT_MAX_RESULTS,
            12, 5, True, embed_rerank,
        )
    return SearchModeConfig(
        mode, min_results, configured_target, _qs.CHAT_MAX_RESULTS,
        _qs.CHAT_PAGE_FETCH_COUNT, 4, True, embed_rerank,
    )


def _display_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _latest_user_message(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            return m["content"].strip()
    return ""


def _strip_online_prefix(text: str) -> tuple[str, bool]:
    stripped = (text or "").strip()
    if stripped.lower().startswith("/online"):
        return stripped[len("/online"):].strip(), True
    return stripped, False


def _freshness_from_text(text: str, now: datetime) -> tuple[str, str | None, str | None, str]:
    low = text.lower()
    if re.search(r"\btoday|tonight|this morning|this afternoon|this evening\b", low):
        return "day", now.date().isoformat(), "day", _display_date(now.date())
    if re.search(r"\byesterday\b|\blast\s+night\b", low):
        d = now.date() - timedelta(days=1)
        return "day", d.isoformat(), "day", _display_date(d)
    if re.search(r"\bthis\s+week\b", low):
        return "week", None, "week", ""
    if re.search(r"\bthis\s+month\b", low):
        return "month", None, "month", ""
    if re.search(r"\b(latest|current|currently|breaking|recent|recently|updates?|happening|going on)\b", low):
        return "month", None, "month", ""
    if re.search(r"\b(?:right\s+now|as\s+of\s+now)\b", low):
        return "month", None, "month", ""
    if str(now.year) in low or str(now.year - 1) in low:
        return "month", None, "month", ""
    return "none", None, None, ""


_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(what(?:'s| is| was| were)?|who(?:'s| is)?|where(?:'s| is)?|"
    r"when(?:'s| is)?|why(?:'s| is)?|how(?:'s| is| was)?|"
    r"tell me about|give me|show me|find|search for|look up)\s+",
    re.IGNORECASE,
)
_EVENT_FILLER_RE = re.compile(
    r"\b(?:that\s+)?(?:just\s+)?(?:took|takes|taking)\s+place\b|"
    r"\b(?:happened|happening|going\s+on)\b|"
    r"\b(?:just|earlier|later|currently|current|latest|recent|recently)\b",
    re.IGNORECASE,
)
_RELATIVE_DAY_RE = re.compile(
    r"\b(today|tonight|this morning|this afternoon|this evening|yesterday|last\s+night)\b",
    re.IGNORECASE,
)
_ATTACHMENT_PLACEHOLDER_RE = re.compile(r"\s*\[Attached[^\]]+\]\s*", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_SEARCH_CONTEXT_RE = re.compile(
    r"\n+===\s*(?:QUICK WEB SEARCH|WEB SEARCH|SEARCH)\b[\s\S]*$",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(
    r"\b(?:historical|historically|history|real\s+life|real-world|irl|actual\s+history)\b",
    re.IGNORECASE,
)
_GAME_CONTEXT_RE = re.compile(
    r"\b(?:plays?|playing|game|gaming|gameplay|video\s+game|strategy\s+game|"
    r"grand\s+strategy|campaign|save\s+file|steam|xbox|playstation|nintendo|"
    r"wiki\.gg|fandom|modded?|dlc|patch)\b",
    re.IGNORECASE,
)
_GAME_STATE_RE = re.compile(
    r"\b(?:play|playing|campaign|save|nation|nations|country|countries|war|"
    r"invade|invasion|ally|allies|alliance|favor|favors|favour|favours|"
    r"curry|province|quest|level|boss|build|class|character|turn|map|mission|"
    r"achievement|start|strategy|meta|patch|dlc|mod|server|join)\b",
    re.IGNORECASE,
)
_CODE_CONTEXT_RE = re.compile(
    r"\b(?:project|repo|repository|codebase|library|framework|package|api|"
    r"backend|frontend|database|server|app|component|function|class|module)\b",
    re.IGNORECASE,
)
_TRUE_RECENCY_RE = re.compile(
    r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening|week)|yesterday|"
    r"latest|breaking|recent(?:ly)?|news|updates?|release[sd]?|launch(?:ed)?|"
    r"announc(?:ed|ement)|new\s+version|patch\s+notes?|dev\s+diar(?:y|ies)|"
    r"current\s+patch|cve|vulnerabilit(?:y|ies)|20[2-3]\d)\b",
    re.IGNORECASE,
)
_DOMAIN_FRESH_RE: dict[str, re.Pattern] = {
    "game": re.compile(
        r"\b(?:latest|new|recent(?:ly)?|patch(?:\s+notes?)?|dlc|update|updates|"
        r"dev\s+diar(?:y|ies)|version|release[sd]?|meta|tier\s+list|current\s+patch)\b",
        re.IGNORECASE,
    ),
    "code": re.compile(
        r"\b(?:latest|new|recent(?:ly)?|release[sd]?|version|changelog|"
        r"deprecat(?:ed|ion)|cve|vulnerabilit(?:y|ies)|security\s+advisory|"
        r"breaking\s+change|update|updates?)\b",
        re.IGNORECASE,
    ),
    "recipe": re.compile(r"\b(?:latest|recent(?:ly)?|new|trend|viral)\b", re.IGNORECASE),
}
_LOCAL_ACTOR_RE = re.compile(
    r"\b(?:should|can|could|would|will|does|do|did|is|was)\s+([A-Z][a-z]{2,30})\s+"
    r"(?=(?:try|play|use|pick|choose|switch|start|join|make|build|run|buy|watch|read|learn)\b)"
)
_TITLE_PHRASE_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'.-]+|[A-Z]{2,6}\d?|[IVX]{2,5})"
    r"(?:\s+(?:[A-Z][A-Za-z0-9'.-]+|[A-Z]{2,6}\d?|[IVX]{2,5}|[0-9]+)){0,4}\b"
)
_ANCHOR_ALIAS_PAIRS = (
    ("Europa Universalis IV", "EU4"),
    ("Hearts of Iron IV", "HOI4"),
    ("Crusader Kings III", "CK3"),
    ("Victoria 3", "VIC3"),
    ("Civilization VI", "Civ 6"),
    ("Civilization V", "Civ 5"),
    ("Baldur's Gate 3", "BG3"),
)
_ANCHOR_ALIAS_LOOKUP: dict[str, str] = {}
for _full, _alias in _ANCHOR_ALIAS_PAIRS:
    _ANCHOR_ALIAS_LOOKUP[_full.lower()] = _alias
    _ANCHOR_ALIAS_LOOKUP[_alias.lower()] = _full
_ANCHOR_DROP = frozenset({
    "the", "this", "that", "user", "assistant", "current", "attached", "image",
    "quick", "search", "web", "sources", "source", "context", "memory",
})


def _strip_search_noise(text: str) -> str:
    q = _SEARCH_CONTEXT_RE.sub(" ", text or "")
    q = _MARKDOWN_IMAGE_RE.sub(" ", q)
    q = _ATTACHMENT_PLACEHOLDER_RE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _dedupe_preserve(items: list[str], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = re.sub(r"\s+", " ", (item or "").strip(" .,:;!?\"'()[]{}")).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _alias_terms(term: str) -> list[str]:
    key = (term or "").strip().lower()
    if not key:
        return []
    alias = _ANCHOR_ALIAS_LOOKUP.get(key)
    if alias:
        return [term, alias]
    return [term]


def _title_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for match in _TITLE_PHRASE_RE.finditer(text or ""):
        phrase = match.group(0).strip()
        words = phrase.split()
        if not words:
            continue
        low_words = [w.strip(".,:;!?()[]{}\"'").lower() for w in words]
        if all(w in _ANCHOR_DROP for w in low_words):
            continue
        if len(words) == 1 and low_words[0] in _ANCHOR_DROP:
            continue
        if len(words) == 1 and phrase[0].isupper() and phrase[1:].islower():
            # Single title-case words are often local names; keep acronyms and
            # multi-word subjects as safer anchors.
            continue
        phrases.extend(_alias_terms(phrase))
    for key, counterpart in _ANCHOR_ALIAS_LOOKUP.items():
        if len(key) > 8:
            continue
        if re.search(rf"\b{re.escape(key)}\b", text or "", re.IGNORECASE):
            phrases.extend(_alias_terms(key.upper()))
            phrases.extend(_alias_terms(counterpart))
    return _dedupe_preserve(phrases)


def _extract_context_anchors(text: str, category: str | None) -> list[str]:
    cleaned = _strip_search_noise(text)
    anchors = _title_phrases(cleaned)
    if category == "code":
        for token in re.findall(
            r"\b(?:React|Vue|Angular|FastAPI|Django|Flask|SQLite|Postgres|"
            r"PostgreSQL|MySQL|Redis|Docker|Kubernetes|Node\.?js|TypeScript|"
            r"JavaScript|Python|Rust|Go|Golang|HyprChat)\b",
            cleaned,
            flags=re.IGNORECASE,
        ):
            anchors.extend(_alias_terms(token))
    return _dedupe_preserve(anchors)


def _extract_local_entities(text: str, anchors: list[str] | None = None) -> list[str]:
    anchor_lows = {a.lower() for a in (anchors or [])}
    local: list[str] = []
    for match in _LOCAL_ACTOR_RE.finditer(text or ""):
        name = match.group(1).strip()
        if name.lower() in anchor_lows:
            continue
        local.append(name)
    return _dedupe_preserve(local, limit=4)


def _context_category(prior: str, hint: str) -> tuple[str | None, float]:
    context = " ".join([prior, hint])
    if _qs._is_game_query(context) or _GAME_CONTEXT_RE.search(context):
        return "game", 0.72
    if _qs._is_code_query(context) or _CODE_CONTEXT_RE.search(context):
        return "code", 0.64
    if _qs._RECIPE_RE.search(context):
        return "recipe", 0.60
    return None, 0.0


def _infer_search_frame(latest: str, turns: list[str], context_hint: str = "") -> SearchFrame:
    latest_clean = _strip_search_noise(latest)
    prior = " ".join(_strip_search_noise(t) for t in turns[-4:])
    hint = _strip_search_noise(context_hint)
    context_cat, context_conf = _context_category(prior, hint)
    context_anchors = _extract_context_anchors(" ".join([prior, hint]), context_cat)
    latest_anchors = _extract_context_anchors(latest_clean, None)
    explicit_game = _qs._is_game_query(latest_clean)

    if _HISTORICAL_RE.search(latest_clean) and not explicit_game:
        local = _extract_local_entities(latest_clean, context_anchors + latest_anchors)
        return SearchFrame("general", [], local, 0.80)
    if (
        context_cat != "game"
        and not explicit_game
        and _qs._is_sports_query(latest_clean)
    ):
        anchors = _dedupe_preserve(latest_anchors)
        return SearchFrame("news", anchors, _extract_local_entities(latest_clean, anchors), 0.80)
    if _qs._is_code_query(latest_clean):
        anchors = _dedupe_preserve(latest_anchors + context_anchors)
        return SearchFrame("code", anchors, _extract_local_entities(latest_clean, anchors), 0.88)
    if _qs._RECIPE_RE.search(latest_clean):
        anchors = _dedupe_preserve(latest_anchors + context_anchors)
        return SearchFrame("recipe", anchors, _extract_local_entities(latest_clean, anchors), 0.86)
    if explicit_game:
        anchors = _dedupe_preserve(latest_anchors + context_anchors)
        return SearchFrame("game", anchors, _extract_local_entities(latest_clean, anchors), 0.88)
    if context_cat == "game" and (_GAME_STATE_RE.search(latest_clean) or len(_qs._content_tokens(latest_clean)) <= 7):
        anchors = _dedupe_preserve(context_anchors + latest_anchors)
        return SearchFrame("game", anchors, _extract_local_entities(latest_clean, anchors), context_conf)
    if context_cat == "code" and (_CODE_CONTEXT_RE.search(latest_clean) or len(_qs._content_tokens(latest_clean)) <= 7):
        anchors = _dedupe_preserve(context_anchors + latest_anchors)
        return SearchFrame("code", anchors, _extract_local_entities(latest_clean, anchors), context_conf)
    if context_cat == "recipe" and len(_qs._content_tokens(latest_clean)) <= 7:
        anchors = _dedupe_preserve(context_anchors + latest_anchors)
        return SearchFrame("recipe", anchors, _extract_local_entities(latest_clean, anchors), context_conf)

    local = _extract_local_entities(latest_clean, context_anchors + latest_anchors)
    return SearchFrame(None, [], local, 0.0)


def _clean_query_phrase(text: str) -> str:
    q = _strip_search_noise(text)
    q = re.sub(r"https?://\S+", " ", q)
    q = _QUESTION_PREFIX_RE.sub("", q)
    q = re.sub(r"\b(please|for me)\b", " ", q, flags=re.I)
    q = re.sub(r"[?!.,;:]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    words = q.split()
    if len(words) > 12:
        # Over budget: drop stopwords first so the subject at the tail of a
        # long question survives, instead of blind head truncation.
        content = [w for w in words if w.lower() not in _qs._STOPWORDS]
        words = content[:12] if len(content) >= 4 else words[:12]
        q = " ".join(words)
    return q or (text or "").strip()[:120]


def _clean_fresh_subject_phrase(text: str) -> str:
    q = _clean_query_phrase(text)
    q = _EVENT_FILLER_RE.sub(" ", q)
    q = _RELATIVE_DAY_RE.sub(" ", q)
    q = re.sub(r"\b(news|updates?)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    return q or _clean_query_phrase(text)


def _year_from_resolved_label(resolved_label: str) -> str:
    m = re.search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", resolved_label or "")
    return m.group(1) if m else str(datetime.now().year)


def _query_variants(base: str, category: str, freshness_mode: str, resolved_label: str, max_queries: int) -> list[str]:
    base = _clean_query_phrase(base)
    subject = _clean_fresh_subject_phrase(base) if freshness_mode == "day" else base
    variants = [subject]
    if freshness_mode == "day":
        year = _year_from_resolved_label(resolved_label)
        dated = _clean_query_phrase(f"{subject} {resolved_label}") if resolved_label else subject
        year_query = _clean_query_phrase(f"{subject} {year}") if year and year not in subject else subject
        variants = [
            year_query,
            dated,
            _clean_query_phrase(f"{subject} today"),
            _clean_query_phrase(f"{subject} news"),
        ]
    elif category == "news":
        variants = [base]
        low = f" {base.lower()} "
        for suffix in ("latest", "news", "updates"):
            if f" {suffix} " not in low:
                variants.append(_clean_query_phrase(f"{base} {suffix}"))
    elif category == "code":
        variants = [
            base,
            _clean_query_phrase(f"{base} documentation"),
            _clean_query_phrase(f"{base} examples"),
        ]
    elif category == "recipe":
        variants = [base, _clean_query_phrase(f"{base} recipe"), _clean_query_phrase(f"{base} technique")]
    else:
        # General: the cleaned message plus a keyword-only variant (content
        # words in original order) — "X overview"/"X sources" filler suffixes
        # skewed engines toward meta/aggregator pages.
        variants = [base]
        keyword_q = " ".join(
            [w for w in base.split() if w.lower() not in _qs._STOPWORDS][:10]
        )
        if keyword_q and keyword_q.lower() != base.lower():
            variants.append(keyword_q)
    out: list[str] = []
    seen: set[str] = set()
    for q in variants:
        q = q.strip()
        key = q.lower()
        if not q or key in seen or not _qs._content_tokens(q):
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= max_queries:
            break
    return out or [base]


def _remove_terms(text: str, terms: list[str]) -> str:
    q = text or ""
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        q = re.sub(rf"\b{re.escape(term)}\b", " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def _subject_for_frame(text: str, frame: SearchFrame) -> str:
    q = _strip_search_noise(text)
    q = _remove_terms(q, frame.local_entities)
    q = _remove_terms(q, frame.anchor_terms)
    q = re.sub(r"https?://\S+", " ", q)
    q = re.sub(
        r"\b(?:should|can|could|would|will|do|does|did|is|are|was|were)\s+"
        r"(?:i|we|you|he|she|they|it)?\b",
        " ",
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(r"\bhow\s+(?:do|can|could|would|should)\s+(?:i|we|you|he|she|they)?\b", " ", q, flags=re.I)
    q = re.sub(r"\b(?:currently|current|already|just|really|please|for me)\b", " ", q, flags=re.I)
    q = re.sub(r"\b(?:won'?t|will not|refusing|refuse)\b", " refuses ", q, flags=re.I)
    q = re.sub(r"\b\d+\+?", " ", q)
    q = re.sub(r"[?!.,;:]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    words = [
        w for w in q.split()
        if w.lower() not in {
            "the", "a", "an", "to", "with", "them", "me", "my", "your", "our",
            "i", "we", "you", "he", "she", "they", "it", "and", "or", "have",
            "has", "had", "how", "do", "does", "did", "in", "on", "at",
        }
    ]
    if len(words) > 10:
        words = words[:10]
    return " ".join(words) or _clean_query_phrase(_remove_terms(text, frame.local_entities))


def _contains_any_term(text: str, terms: list[str]) -> bool:
    low = (text or "").lower()
    return any(term and re.search(rf"\b{re.escape(term.lower())}\b", low) for term in terms)


def _frame_query_variants(
    base: str,
    frame: SearchFrame,
    category: str,
    freshness_mode: str,
    resolved_label: str,
    max_queries: int,
) -> list[str]:
    if not frame.anchor_terms and not frame.local_entities:
        return _query_variants(base, category, freshness_mode, resolved_label, max_queries)

    subject = _clean_fresh_subject_phrase(base) if freshness_mode == "day" else _subject_for_frame(base, frame)
    if not frame.anchor_terms:
        return _query_variants(_remove_terms(base, frame.local_entities), category, freshness_mode, resolved_label, max_queries)

    anchors = _dedupe_preserve(frame.anchor_terms, limit=4)
    first_anchor = anchors[0]
    anchored_subject = subject if _contains_any_term(subject, anchors) else f"{first_anchor} {subject}".strip()
    variants = [anchored_subject]
    if freshness_mode == "day":
        year = _year_from_resolved_label(resolved_label)
        variants.extend([
            _clean_query_phrase(f"{anchored_subject} {resolved_label}") if resolved_label else anchored_subject,
            _clean_query_phrase(f"{anchored_subject} {year}") if year and year not in anchored_subject else anchored_subject,
            _clean_query_phrase(f"{anchored_subject} today"),
        ])
    elif category == "game":
        variants.extend([
            _clean_query_phrase(f"{anchors[min(1, len(anchors) - 1)]} {subject} strategy"),
            _clean_query_phrase(f"{first_anchor} {subject} guide"),
            _clean_query_phrase(f"{anchors[min(1, len(anchors) - 1)]} wiki {subject}"),
        ])
    elif category == "code":
        variants.extend([
            _clean_query_phrase(f"{anchored_subject} documentation"),
            _clean_query_phrase(f"{anchored_subject} examples"),
            _clean_query_phrase(f"{anchored_subject} issue"),
        ])
    elif category == "recipe":
        variants.extend([
            _clean_query_phrase(f"{anchored_subject} recipe"),
            _clean_query_phrase(f"{anchored_subject} technique"),
        ])
    elif category == "news":
        variants.extend([
            _clean_query_phrase(f"{anchored_subject} latest"),
            _clean_query_phrase(f"{anchored_subject} news"),
            _clean_query_phrase(f"{anchored_subject} updates"),
        ])
    else:
        keyword_q = " ".join(
            [w for w in anchored_subject.split() if w.lower() not in _qs._STOPWORDS][:10]
        )
        if keyword_q and keyword_q.lower() != anchored_subject.lower():
            variants.append(keyword_q)

    out: list[str] = []
    seen: set[str] = set()
    for q in variants:
        q = _clean_query_phrase(q)
        q = _remove_terms(q, frame.local_entities)
        key = q.lower()
        if not q or key in seen or not _qs._content_tokens(q):
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= max_queries:
            break
    return out or [subject]


def _context_topic(turns: list[str]) -> str:
    """Compact topic phrase from prior turns for follow-up query fusion.

    Prefers the most recent user turn; falls back to the most recent
    assistant turn when no user turn yields content (so "tell me more about
    that" after an assistant-introduced topic still resolves). Returns
    title-phrase anchors when present, else the first few content-bearing
    words — fusing a whole prior message into the query buries the actual
    follow-up question past the query-length cap.
    """
    def _compact(body: str) -> str:
        cleaned = _clean_query_phrase(body)
        if not _qs._content_tokens(cleaned):
            return ""
        phrases = _title_phrases(cleaned)
        if phrases:
            return " ".join(phrases[:2])
        words = [w for w in cleaned.split() if w.lower() not in _qs._STOPWORDS]
        return " ".join(words[:6])

    for prefix in ("user:", "assistant:"):
        for turn in reversed(turns):
            if not turn.startswith(prefix):
                continue
            topic = _compact(turn.split(":", 1)[-1])
            if topic:
                return topic
    return ""


def _adjust_freshness_for_frame(
    category: str,
    text: str,
    freshness_mode: str,
    resolved_date: str | None,
    time_range: str | None,
    resolved_label: str,
) -> tuple[str, str | None, str | None, str]:
    if freshness_mode == "none":
        return freshness_mode, resolved_date, time_range, resolved_label
    if category == "news":
        return freshness_mode, resolved_date, time_range, resolved_label
    if freshness_mode == "day" and _TRUE_RECENCY_RE.search(text):
        return freshness_mode, resolved_date, time_range, resolved_label
    domain_fresh = _DOMAIN_FRESH_RE.get(category)
    if domain_fresh and domain_fresh.search(text):
        return freshness_mode, resolved_date, time_range, resolved_label
    if category in {"game", "code", "recipe"}:
        return "none", None, None, ""
    if _TRUE_RECENCY_RE.search(text):
        return freshness_mode, resolved_date, time_range, resolved_label
    return "none", None, None, ""


def _deterministic_plan(messages: list, latest: str, mode_cfg: SearchModeConfig, context_hint: str = "") -> SearchPlan:
    now = datetime.now().astimezone()
    latest, forced_online = _strip_online_prefix(latest)
    latest = _strip_search_noise(latest)
    search_provider = _configured_search_provider()
    scraper_provider = _configured_scraper_provider()
    reranker_type = _configured_reranker_type() if mode_cfg.embed_rerank else "none"
    turns, prior_tokens = _build_prior_tokens(messages, latest)
    base = latest
    if prior_tokens and _qs._needs_context(latest, prior_tokens):
        topic = _context_topic(turns)
        if topic and not (_qs._content_tokens(latest) & _qs._content_tokens(topic)):
            base = f"{topic} {latest}"

    frame = _infer_search_frame(latest, turns, context_hint)
    if frame.category and frame.anchor_terms and not _contains_any_term(base, frame.anchor_terms):
        base = f"{frame.anchor_terms[0]} {base}"
    freshness_mode, resolved_date, time_range, resolved_label = _freshness_from_text(base, now)
    category = frame.category or _qs._classify_query(base)
    if freshness_mode != "none" and category == "general":
        category = "news"
    freshness_mode, resolved_date, time_range, resolved_label = _adjust_freshness_for_frame(
        category, base, freshness_mode, resolved_date, time_range, resolved_label,
    )

    canonical = latest
    if resolved_label:
        canonical = re.sub(
            r"\btoday|tonight|this morning|this afternoon|this evening|yesterday\b",
            f"on {resolved_label}",
            canonical,
            flags=re.I,
        )
    if base != latest and _qs._content_tokens(base):
        canonical = f"{canonical} (context: {_clean_query_phrase(base)})"

    queries = _frame_query_variants(
        base, frame, category, freshness_mode, resolved_label, mode_cfg.max_queries,
    )
    if forced_online and latest and latest not in queries:
        queries.insert(0, _clean_query_phrase(latest))
        queries = queries[:mode_cfg.max_queries]

    searxng_engines = _searxng_engines_for_category(category)
    source_bits = [search_provider]
    if forced_online:
        source_bits.append("/online")
    source_bits.append(f"scraper={scraper_provider}")
    source_bits.append(f"reranker={reranker_type}")
    if searxng_engines:
        source_bits.append(f"engines={searxng_engines}")

    return SearchPlan(
        canonical_question=canonical[:400],
        queries=queries,
        category=category,
        anchor_terms=frame.anchor_terms,
        freshness_mode=freshness_mode,
        resolved_date=resolved_date,
        source_mode=";".join(source_bits),
        search_provider=search_provider,
        scraper_provider=scraper_provider,
        reranker_type=reranker_type,
        searxng_engines=searxng_engines,
        target_results=mode_cfg.target_results,
        min_results=mode_cfg.min_results,
        max_results=mode_cfg.max_results,
        page_reads=mode_cfg.page_reads,
        time_range=time_range,
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        embed_rerank=mode_cfg.embed_rerank,
    )


def _searxng_categories(category: str, freshness_mode: str) -> str:
    if category == "news" and freshness_mode != "none":
        return "news"
    return "general"


def _searxng_engines_for_category(category: str) -> str:
    specific = {
        "news": getattr(config, "QUICK_SEARCH_SEARXNG_NEWS_ENGINES", ""),
        "code": getattr(config, "QUICK_SEARCH_SEARXNG_CODE_ENGINES", ""),
        "recipe": getattr(config, "QUICK_SEARCH_SEARXNG_RECIPE_ENGINES", ""),
        "game": getattr(config, "QUICK_SEARCH_SEARXNG_GAME_ENGINES", ""),
    }.get(category, "")
    return (specific or getattr(config, "QUICK_SEARCH_SEARXNG_ENGINES", "") or "").strip()


def _search_count_for_plan(plan: SearchPlan, query_count: int) -> int:
    spread = max(1, query_count)
    per_query = int((plan.target_results / spread) + plan.min_results)
    if plan.freshness_mode == "day":
        per_query += 4
    return max(10, min(_qs.CHAT_SEARCH_COUNT_BROAD, per_query))


async def _search_provider(http, plan: SearchPlan, queries: list[str]) -> tuple[list, int]:
    count = _search_count_for_plan(plan, len(queries))
    categories = _searxng_categories(plan.category, plan.freshness_mode)
    engines = plan.searxng_engines or None
    attempts: list[tuple[str, int, str | None, str, str | None]] = [
        (q, count, plan.time_range, categories, engines)
        for q in queries
    ]
    if plan.freshness_mode == "day":
        # SearXNG's news category and per-engine day filters can miss
        # same-day tech/event pages until they receive date metadata. Keep the
        # strict news/day pass, but add a small general-web fallback so pages
        # like live blogs, official event pages, and tech coverage can surface.
        fallback_count = max(8, min(count, plan.min_results))
        general_engines = (getattr(config, "QUICK_SEARCH_SEARXNG_ENGINES", "") or "").strip() or None
        for q in queries[:2]:
            attempts.append((q, fallback_count, "day", "general", general_engines))
            attempts.append((q, fallback_count, None, "general", general_engines))

    raw_lists = await asyncio.gather(
        *[
            _qs._cached_search(
                http, q, count=attempt_count, time_range=time_range,
                categories=attempt_categories, engines=attempt_engines,
            )
            for q, attempt_count, time_range, attempt_categories, attempt_engines in attempts
        ],
        return_exceptions=True,
    )
    errors = sum(1 for r in raw_lists if isinstance(r, Exception))
    usable: list[list] = []
    for attempt, result in zip(attempts, raw_lists):
        q, _, time_range, attempt_categories, _ = attempt
        if not isinstance(result, list):
            continue
        tagged = []
        for item in result:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault("query_origin", q)
            copied.setdefault("search_time_range", time_range or "")
            copied.setdefault("search_categories", attempt_categories)
            tagged.append(copied)
        usable.append(tagged)
    return _merge_unique(usable), errors


async def _select_top_results(http, ollama_url: str, raw: list, primary: str, plan: SearchPlan) -> list:
    candidates = _qs._rank_for_search_plan(
        raw, primary, plan,
        category=plan.category,
        limit=plan.max_results,
    )
    if not candidates:
        return []
    if plan.embed_rerank:
        try:
            reranked = await asyncio.wait_for(
                _qs._embed_score_and_dedup(
                    http, ollama_url, primary, candidates,
                    limit=plan.target_results, backfill=True,
                ),
                timeout=float(getattr(config, "QUICK_SEARCH_EMBED_TIMEOUT", 1.5)),
            )
            if reranked:
                return reranked[:plan.target_results]
        except Exception as e:
            print(f"[SA]   embed rerank skipped: {type(e).__name__}: {e!r}")
    return candidates[:plan.target_results]


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


def _build_prior_tokens(messages: list, latest: str) -> tuple[list[str], set[str]]:
    """Extract recent conversation turns and the union of their content tokens.

    Used by triage (for the prompt + follow-up validation) and by the
    orchestrator (for follow-up-aware relevance scoring).
    """
    turns: list[str] = []
    for m in messages[-6:]:
        role = m.get("role", "")
        content = _strip_search_noise((m.get("content") or "").strip())
        if role not in ("user", "assistant") or not content:
            continue
        if content == latest:
            continue
        turns.append(f"{role}: {content[:300]}")
    prior_tokens: set[str] = set()
    for t in turns[-4:]:
        body = t.split(":", 1)[-1] if ":" in t else t
        prior_tokens |= _qs._content_tokens(body)
    return turns, prior_tokens


# ── Relevance scoring (cheap heuristic) ──
def relevance_score(
    user_message: str,
    queries: list[str],
    top_results: list[dict],
    prior_tokens: set[str] | None = None,
) -> float:
    """Fraction of original-message content tokens that appear in the
    concatenated titles+snippets of the top results. 0.0..1.0.

    Compares against the user's message, NOT the rewritten queries — if the
    rewrite was bad, we want to detect that the results miss what was asked.

    For follow-ups ("any updates?", "tell me more"), the user message itself
    has no content tokens. When `prior_tokens` is supplied AND the message
    looks like a follow-up, fold the prior topic in so we can still detect
    off-topic results. Without this, refine never fires for follow-ups.
    """
    user_tokens = _qs._content_tokens(user_message)
    if prior_tokens and _qs._needs_context(user_message, prior_tokens):
        user_tokens = user_tokens | prior_tokens
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


# ── Hybrid LLM query planner ──
# Runs in parallel with the deterministic first search wave. The deterministic
# plan is the guaranteed floor; the LLM plan only adds queries / reroutes the
# category when it returns valid output within QUICK_SEARCH_PLANNER_TIMEOUT.
_PLANNER_CATEGORY_MAP = {
    "news": "news", "sports": "news", "code": "code",
    "recipe": "recipe", "game": "game", "general": "general",
}
_PLANNER_FRESHNESS = ("day", "week", "month", "none")


def _planner_enabled(mode_cfg: SearchModeConfig) -> bool:
    if mode_cfg.mode == "speed":
        return False
    planner = (getattr(config, "QUICK_SEARCH_PLANNER", "llm") or "llm").strip().lower()
    return planner == "llm"


async def llm_plan(
    http, ollama_url: str, model: str,
    latest: str, turns: list[str], context_hint: str, now: datetime,
    *, max_queries: int = 3,
) -> dict | None:
    """Ask a small local model for a search plan: standalone question, 1-3
    queries, category, freshness. Returns a validated partial dict or None.

    Every field is optional — callers keep the deterministic value for
    anything missing or invalid, so garbage output can only ever no-op.
    """
    if not model:
        return None
    convo = "\n".join(turns[-4:])
    hint = (context_hint or "").strip()[:600]
    prompt = (
        f"Current date/time: {now.strftime('%A, %B %d, %Y %H:%M %Z')}\n"
        + (f"Conversation so far:\n{convo}\n" if convo else "")
        + (f"Background notes about the user:\n{hint}\n" if hint else "")
        + f"\nLatest user message: {latest}\n\n"
        "Write web search queries that would find sources answering the "
        "latest message. Resolve pronouns and vague references using the "
        "conversation.\n"
        'Output JSON: {"question": "the standalone question being asked", '
        '"queries": ["query 1", "query 2"], "category": "...", '
        '"freshness": "..."}\n'
        f"Rules: 1-{max_queries} queries; each ≤12 words, self-contained, "
        "no quotes or URLs. category: one of news, sports, code, recipe, "
        "game, general. freshness: day (today/yesterday events), week, "
        "month (recent developments), or none (evergreen facts). "
        "Output JSON only."
    )
    out = await _ask_ollama_json(http, ollama_url, prompt, model, max_tokens=240, timeout=30.0)
    if not isinstance(out, dict):
        return None
    plan: dict = {}
    question = out.get("question")
    if isinstance(question, str) and question.strip():
        plan["question"] = question.strip()[:400]
    queries: list[str] = []
    raw_queries = out.get("queries")
    if isinstance(raw_queries, list):
        seen: set[str] = set()
        for item in raw_queries:
            if not isinstance(item, str):
                continue
            cleaned = _clean_query_phrase(item)
            key = cleaned.lower()
            if (
                not cleaned or key in seen or "http" in key
                or not _qs._content_tokens(cleaned)
            ):
                continue
            seen.add(key)
            queries.append(cleaned)
            if len(queries) >= max_queries:
                break
    if queries:
        plan["queries"] = queries
    cat = out.get("category")
    if isinstance(cat, str) and cat.strip().lower() in _PLANNER_CATEGORY_MAP:
        plan["category"] = _PLANNER_CATEGORY_MAP[cat.strip().lower()]
    fresh = out.get("freshness")
    if isinstance(fresh, str) and fresh.strip().lower() in _PLANNER_FRESHNESS:
        plan["freshness"] = fresh.strip().lower()
    return plan or None


def _merge_llm_plan(plan: SearchPlan, llm_out: dict) -> list[str]:
    """Fold validated planner output into the deterministic plan in place.

    Returns the planner queries not already present in `plan.queries`.
    Freshness is only ever raised from "none" to week/month — explicit
    day/week cues are reliably regex-detectable, so the LLM adds value on
    implicit recency ("who is the UK PM") without being allowed to flip an
    explicit date question.
    """
    if llm_out.get("question"):
        plan.canonical_question = llm_out["question"]
    if llm_out.get("category") and llm_out["category"] != plan.category:
        plan.category = llm_out["category"]
        plan.searxng_engines = _searxng_engines_for_category(plan.category)
    if plan.freshness_mode == "none" and llm_out.get("freshness") in ("week", "month"):
        plan.freshness_mode = llm_out["freshness"]
        plan.time_range = llm_out["freshness"]
    existing = {q.lower() for q in plan.queries}
    return [q for q in llm_out.get("queries", []) if q.lower() not in existing]


# ── Orchestrator ──
async def run_search_agent(
    http, ollama_url: str, planner_model: str,
    events, conv_id: str, messages: list,
    *, default_model: str = "",
    context_hint: str = "",
    max_rounds: int = 2,
    relevance_threshold: float = _REFINE_THRESHOLD_DEFAULT,
) -> dict:
    """Orchestrate skip → deterministic plan + parallel LLM planner →
    multi-query search → heuristic rank → literal fallback / LLM refine on
    low relevance → page/OG enrichment → context.

    Returns the same shape as `run_quick_search_for_chat`:
      {"context", "rewritten_query", "skipped", "reason"}
    """
    latest = _latest_user_message(messages)
    if not latest:
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": "no user message"}

    clean_latest, _ = _strip_online_prefix(latest)
    clean_latest = _strip_search_noise(clean_latest)
    skip, reason = _qs._should_skip(clean_latest)
    if skip:
        await _emit(events, conv_id, "tool_done", {
            "tool": "quick_search", "icon": "search", "status": f"Skipped ({reason})",
        })
        return {"context": "", "rewritten_query": "", "skipped": True, "reason": reason}

    mode_cfg = _mode_config()
    plan = _deterministic_plan(messages, latest, mode_cfg, context_hint=context_hint)
    turns, prior_tokens = _build_prior_tokens(messages, clean_latest)

    # Start the LLM planner now so it overlaps the first search wave.
    planner_task = None
    if _planner_enabled(mode_cfg) and planner_model:
        planner_task = asyncio.create_task(asyncio.wait_for(
            llm_plan(
                http, ollama_url, planner_model,
                clean_latest, turns, context_hint,
                datetime.now().astimezone(),
                max_queries=min(3, mode_cfg.max_queries),
            ),
            timeout=float(getattr(config, "QUICK_SEARCH_PLANNER_TIMEOUT", 6.0)),
        ))

    await _emit(events, conv_id, "tool_start", {
        "tool": "quick_search", "icon": "search",
        "status": f"Searching ({mode_cfg.mode}): {plan.canonical_question[:60]}",
    })

    # Filter out queries already searched earlier in this conversation.
    queries = _qs._filter_novel_queries(conv_id, list(plan.queries))
    plan.queries = queries

    progress_label = " | ".join(q[:40] for q in queries[:2])
    await _emit(events, conv_id, "tool_progress", {
        "tool": "quick_search", "icon": "search",
        "status": f"→ {progress_label[:120]}",
    })

    rounds_used = 1
    raw, search_errors = await _search_provider(http, plan, queries)

    # Fold in the LLM plan if it landed: adopt question/category/freshness,
    # then run a second wave for the genuinely new queries.
    planner_outcome = "deterministic"
    if planner_task is not None:
        llm_out = None
        try:
            llm_out = await planner_task
            planner_outcome = "used" if llm_out else "invalid"
        except asyncio.TimeoutError:
            planner_outcome = "timeout"
        except Exception as e:
            planner_outcome = f"error:{type(e).__name__}"
        if llm_out:
            new_queries = _merge_llm_plan(plan, llm_out)
            if new_queries:
                new_queries = _qs._filter_novel_queries(conv_id, new_queries)
            if new_queries:
                await _emit(events, conv_id, "tool_progress", {
                    "tool": "quick_search", "icon": "search",
                    "status": f"Planner → {' | '.join(q[:40] for q in new_queries[:2])[:120]}",
                })
                wave2, wave2_errors = await _search_provider(http, plan, new_queries)
                search_errors += wave2_errors
                if wave2:
                    raw = _merge_unique([wave2, raw])
                queries = new_queries + queries
                plan.queries = queries
    plan.source_mode = f"{plan.source_mode};planner={planner_outcome}"

    if not raw:
        reason_text = "search unavailable or no results"
        ctx = _qs._build_unavailable_context(plan.canonical_question, plan, reason_text)
        await _emit(events, conv_id, "tool_done", {
            "tool": "quick_search", "icon": "search", "status": "No usable search results",
        })
        return {"context": ctx, "rewritten_query": plan.canonical_question,
                "skipped": False, "reason": "no results"}

    primary = plan.canonical_question
    top = await _select_top_results(http, ollama_url, raw, primary, plan)
    score = relevance_score(clean_latest, queries, top, prior_tokens)
    print(
        f"[SA]   plan mode={mode_cfg.mode} round={rounds_used} "
        f"planner={planner_outcome} freshness={plan.freshness_mode} "
        f"time_range={plan.time_range} q={queries!r} "
        f"relevance={score:.2f} errors={search_errors}"
    )

    # ── Free deterministic fallback: retry the raw user message ──
    # Catches the case where anchoring/fusion/subject-stripping mangled the
    # query. No LLM cost, so it runs before spending the refine round.
    if score < relevance_threshold and len(_qs._content_tokens(clean_latest)) >= 3:
        literal = _clean_query_phrase(clean_latest)
        if literal and literal.lower() not in {q.lower() for q in queries}:
            try:
                more = await _qs._cached_search(
                    http, literal,
                    count=_search_count_for_plan(plan, len(queries) + 1),
                    time_range=plan.time_range,
                    categories=_searxng_categories(plan.category, plan.freshness_mode),
                )
            except Exception:
                more = []
            if more:
                more = [{**r, "query_origin": literal} for r in more if isinstance(r, dict)]
                raw = _merge_unique([raw, more])
                queries = queries + [literal]
                plan.queries = queries
                top = await _select_top_results(http, ollama_url, raw, primary, plan)
                score = relevance_score(clean_latest, queries, top, prior_tokens)
                print(f"[SA]   literal fallback {literal!r} relevance={score:.2f}")

    # ── Optional LLM refinement when results still look off-topic ──
    # QUICK_SEARCH_PLANNER=deterministic is the kill switch for ALL planner-
    # model LLM calls, refine included.
    if (
        mode_cfg.allow_refine
        and _planner_enabled(mode_cfg)
        and score < relevance_threshold
        and rounds_used < max_rounds
        and len(_qs._content_tokens(clean_latest)) >= 3
    ):
        refined = await refine_query(http, ollama_url, planner_model, clean_latest, queries, top)
        if refined and refined not in queries:
            await _emit(events, conv_id, "tool_progress", {
                "tool": "quick_search", "icon": "search",
                "status": f"Round 2: refining → {refined[:60]}",
            })
            try:
                more = await _qs._cached_search(
                    http, refined,
                    count=_search_count_for_plan(plan, len(queries) + 1),
                    time_range=plan.time_range,
                    categories=_searxng_categories(plan.category, plan.freshness_mode),
                )
            except Exception:
                more = []
            if more:
                more = [{**r, "query_origin": refined} for r in more if isinstance(r, dict)]
                raw = _merge_unique([raw, more])
                queries = queries + [refined]
                plan.queries = queries
                top = await _select_top_results(http, ollama_url, raw, primary, plan)
                rounds_used = 2
                score = relevance_score(clean_latest, queries, top, prior_tokens)
                print(f"[SA]   round={rounds_used} refined={refined!r} relevance={score:.2f}")

    # ── Page-fetch + OG-image enrichment (parallel) ──
    page_text, _ = await asyncio.gather(
        _qs._enrich_with_pages(http, top, top_n=plan.page_reads),
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
            "published_date": r.get("published_date", ""),
            "freshness": r.get("freshness", ""),
            "source_tier": r.get("source_tier", ""),
            "score": r.get("score", 0),
            "score_reason": r.get("score_reason", ""),
            "query_origin": r.get("query_origin", ""),
        }
        for r in top
    ]
    await _emit(events, conv_id, "search_results", {
        "query": primary,
        "results": carousel,
        "source": "quick_search",
        "freshness_mode": plan.freshness_mode,
        "resolved_date": plan.resolved_date,
        "queries": plan.queries,
    })

    low_relevance = score < relevance_threshold and len(_qs._content_tokens(clean_latest)) >= 3
    ctx = _qs._build_context(top, primary, page_text, allowed, plan, low_relevance=low_relevance)

    await _emit(events, conv_id, "tool_done", {
        "tool": "quick_search", "icon": "search",
        "status": f"Found {len(top)} result{'s' if len(top) != 1 else ''}"
                  f"{' (refined)' if rounds_used > 1 else ''}",
    })

    print(f"[SA]   {len(top)} results, category={plan.category}, rounds={rounds_used}")
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
