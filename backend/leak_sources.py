"""Direct leak / FOIA / government / court source adapters for the Conspiracy Bot
and the Investigative Dossier research template.

Each adapter is an ``async def <name>(http, query, limit) -> list[dict]`` returning
normalized result dicts::

    {"title": str, "url": str, "content": str, "source": str, "kind": str,
     "engine": str, "type": "web", "score": int}

``kind`` is the source class used to bucket the dossier: ``leak`` (leaked documents),
``gov`` (government records), ``court`` (court filings), ``foia`` (FOIA archives), or
``archive`` (general archives). The extra ``engine``/``type``/``score`` keys keep the
dicts shape-compatible with the SearXNG findings already flowing through
``research.run_conspiracy_research`` (page fetch, tiering, dossier assembly).

Design rules:
- **Fail open.** Every adapter returns ``[]`` on any exception, non-200, or malformed
  body. One dead source can never sink an investigation.
- **Bounded.** Each adapter has its own request timeout; the dispatcher wraps the whole
  wave in an overall deadline.
- **No import cycle.** ``research.py`` imports this module, so this module must NOT import
  ``research`` at load time. The thin module-level wrappers below do call-time imports of
  ``research`` helpers — that also makes them monkeypatchable in offline unit tests.

Live-probe findings (2026-07): DOJ press-release JSON, CourtListener v4 search, and
Archive.org advancedsearch respond directly and unauthenticated. FBI Vault (403), the CIA
Reading Room (302→home), ICIJ Offshore Leaks (202 bot challenge), and MuckRock (Cloudflare)
block scripted access, so those are served through SearXNG ``site:`` scoping instead.
"""

import asyncio
import json
import urllib.parse

import config


# ── Thin, monkeypatchable wrappers around research helpers (call-time import
#    keeps this module free of an import cycle with research.py) ──

class _MiniResp:
    """Minimal response shim (.status_code / .json() / .text) over a bounded fetch."""

    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._body)


async def web_get(http, url: str, *, timeout: float = 15.0,
                  headers: dict | None = None, max_bytes: int = 1_500_000):
    """Bounded fetch for the fixed-host JSON adapters via research.fetch_bytes_safely
    (size cap + per-hop redirect safety) instead of the uncapped research.web_get.
    Non-200s are logged — a quietly-403ing CourtListener would otherwise be
    indistinguishable from "no results"."""
    from research import fetch_bytes_safely
    status, _hdrs, _final, body = await fetch_bytes_safely(
        http, url, timeout=timeout, headers=headers, max_bytes=max_bytes)
    if status != 200:
        print(f"[LEAK] {url.split('?', 1)[0]} returned HTTP {status}")
    return _MiniResp(status, body)


async def searxng_search(http, searxng_url: str, query: str, count: int = 10,
                         *, fallback_state: dict | None = None) -> list:
    from research import _search_searxng
    return await _search_searxng(http, searxng_url, query, count,
                                 fallback_state=fallback_state)


async def wikileaks_search(http, searxng_url: str, query: str, count: int = 12,
                           *, fallback_state: dict | None = None) -> list:
    from research import _search_wikileaks
    return await _search_wikileaks(http, searxng_url, query, count,
                                   fallback_state=fallback_state)


def clean_html(text: str) -> str:
    from research import _clean_html_text
    return _clean_html_text(text or "")


# ── Depth → per-adapter result budget ──
_LIMIT_BY_DEPTH = {1: 3, 2: 3, 3: 4, 4: 6, 5: 8}
_DEFAULT_TIMEOUT = 15.0


def _limit_for_depth(depth: int) -> int:
    try:
        return _LIMIT_BY_DEPTH.get(int(depth), 5)
    except Exception:
        return 5


def _norm_url_key(url: str) -> str:
    """Loose dedupe key: drop fragment and trailing slash, lowercase."""
    if not url:
        return ""
    u = url.split("#", 1)[0].strip()
    return u.rstrip("/").lower()


def _result(title: str, url: str, snippet: str, *, source: str, kind: str, score: int = 0) -> dict:
    return {
        "title": (title or "").strip() or "(untitled)",
        "url": url,
        "content": (snippet or "").strip()[:500],
        "source": source,
        "kind": kind,
        "engine": source.lower().replace(" ", "_"),
        "type": "web",
        "score": score,
    }


# ── Direct API adapters (fixed public hosts — plain bounded web_get) ──

async def search_doj(http, query: str, limit: int) -> list[dict]:
    """DOJ press releases via the public JSON API (keyword search, no auth)."""
    try:
        params = urllib.parse.urlencode({"keyword": query, "pagesize": max(1, min(limit, 25))})
        r = await web_get(
            http, f"https://www.justice.gov/api/v1/press_releases.json?{params}",
            timeout=_DEFAULT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for item in (data.get("results") or [])[:limit]:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            snippet = clean_html(item.get("teaser") or item.get("body") or "")
            out.append(_result(
                item.get("title") or "DOJ Press Release", url, snippet,
                source="DOJ", kind="gov",
            ))
        return out
    except Exception:
        return []


async def search_courtlistener(http, query: str, limit: int) -> list[dict]:
    """Federal court dockets via CourtListener v4 REST search (optional token)."""
    try:
        params = urllib.parse.urlencode({"q": query, "type": "r"})
        headers = {"User-Agent": "Mozilla/5.0"}
        token = config.COURTLISTENER_TOKEN
        if token:
            headers["Authorization"] = f"Token {token}"
        r = await web_get(
            http, f"https://www.courtlistener.com/api/rest/v4/search/?{params}",
            timeout=_DEFAULT_TIMEOUT, headers=headers,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for item in (data.get("results") or [])[:limit]:
            path = item.get("docket_absolute_url") or item.get("absolute_url") or ""
            if not path:
                continue
            url = path if path.startswith("http") else f"https://www.courtlistener.com{path}"
            court = item.get("court") or item.get("court_citation_string") or ""
            filed = item.get("dateFiled") or ""
            docket = item.get("docketNumber") or ""
            bits = [b for b in (court, f"Docket {docket}" if docket else "", f"Filed {filed}" if filed else "") if b]
            out.append(_result(
                item.get("caseName") or "Court Docket", url, " · ".join(bits),
                source="CourtListener", kind="court",
            ))
        return out
    except Exception:
        return []


async def search_archive_org(http, query: str, limit: int) -> list[dict]:
    """Internet Archive items via the advancedsearch JSON API."""
    try:
        base = "https://archive.org/advancedsearch.php"
        params = (
            f"q={urllib.parse.quote(query)}"
            "&fl[]=identifier&fl[]=title&fl[]=description"
            f"&rows={max(1, min(limit, 25))}&output=json"
        )
        r = await web_get(
            http, f"{base}?{params}",
            timeout=_DEFAULT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        docs = ((data.get("response") or {}).get("docs")) or []
        out = []
        for doc in docs[:limit]:
            ident = doc.get("identifier")
            if not ident:
                continue
            desc = doc.get("description") or ""
            if isinstance(desc, list):
                desc = " ".join(str(d) for d in desc)
            out.append(_result(
                doc.get("title") or ident, f"https://archive.org/details/{ident}",
                clean_html(desc), source="Archive.org", kind="archive",
            ))
        return out
    except Exception:
        return []


async def search_wikileaks(http, query: str, limit: int, *, searxng_url: str = "",
                           fallback_state: dict | None = None) -> list[dict]:
    """WikiLeaks documents via the existing research helper (SearXNG-backstopped)."""
    try:
        raw = await wikileaks_search(http, searxng_url, query, limit,
                                     fallback_state=fallback_state)
        out = []
        for item in (raw or [])[:limit]:
            url = item.get("url") or ""
            if not url:
                continue
            out.append(_result(
                item.get("title") or "WikiLeaks Document", url,
                item.get("content") or "", source="WikiLeaks", kind="leak",
                score=item.get("score") or 0,
            ))
        return out
    except Exception:
        return []


# ── SearXNG-scoped adapters (hosts that block scripted access) ──

async def _searxng_site(http, query: str, limit: int, *, searxng_url: str,
                        site: str, source: str, kind: str,
                        fallback_state: dict | None = None) -> list[dict]:
    if not searxng_url:
        return []
    try:
        raw = await searxng_search(http, searxng_url, f"{query} site:{site}", max(limit, 6),
                                   fallback_state=fallback_state)
        out = []
        for item in (raw or [])[:limit]:
            url = item.get("url") or ""
            if not url:
                continue
            out.append(_result(
                item.get("title") or source, url, item.get("content") or "",
                source=source, kind=kind, score=item.get("score") or 0,
            ))
        return out
    except Exception:
        return []


# site → (source label, kind)
_SCOPED_SOURCES = [
    ("vault.fbi.gov",           "FBI Vault",                   "gov"),
    ("cia.gov/readingroom",     "CIA Reading Room",            "gov"),
    ("offshoreleaks.icij.org",  "ICIJ Offshore Leaks",         "leak"),
    ("cryptome.org",            "Cryptome",                    "leak"),
    ("muckrock.com",            "MuckRock",                    "foia"),
    ("nsarchive.gwu.edu",       "National Security Archive",   "foia"),
    ("governmentattic.org",     "GovernmentAttic",             "foia"),
]


async def gather_leak_sources(http, query: str, *, depth: int = 4,
                              searxng_url: str = "", deadline: float = 25.0,
                              fallback_state: dict | None = None) -> list[dict]:
    """Run every leak/FOIA/gov/court adapter concurrently and return a deduped,
    normalized result list. Fails open: a broken or slow adapter contributes
    nothing rather than raising; an overall-deadline overrun returns whatever
    finished in time. `fallback_state` is the caller's shared Google-fallback
    budget ({"remaining": N}) — without it, 8 SearXNG-backed adapters hitting a
    dead SearXNG would each fire an unbudgeted Google scrape.
    """
    limit = _limit_for_depth(depth)

    coros = [
        search_doj(http, query, limit),
        search_courtlistener(http, query, limit),
        search_archive_org(http, query, limit),
        search_wikileaks(http, query, limit, searxng_url=searxng_url,
                         fallback_state=fallback_state),
    ]
    for site, source, kind in _SCOPED_SOURCES:
        coros.append(_searxng_site(
            http, query, limit, searxng_url=searxng_url,
            site=site, source=source, kind=kind,
            fallback_state=fallback_state,
        ))

    # Cap adapter concurrency: this wave runs alongside the caller's own
    # SearXNG batches, so 11 simultaneous adapters would 429 the instance.
    sem = asyncio.Semaphore(3)

    async def _capped(coro):
        async with sem:
            return await coro

    # asyncio.wait (not wait_for) so adapters that finished before the deadline
    # still contribute — a slow straggler is cancelled, not the whole wave.
    tasks = [asyncio.ensure_future(_capped(c)) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, timeout=deadline)
    except Exception:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return []
    for t in pending:
        t.cancel()
    if pending:
        # Reap cancelled stragglers so their sockets close deterministically
        # (and "Task was destroyed but it is pending!" never logs).
        await asyncio.gather(*pending, return_exceptions=True)

    # Iterate in submission order (direct API adapters before SearXNG-scoped
    # ones) so URL dedup is deterministic: a direct-archive hit wins over a
    # site:-scoped duplicate of the same document.
    out: list[dict] = []
    seen: set[str] = set()
    for t in tasks:
        if t not in done:
            continue
        try:
            res = t.result()
        except Exception:
            continue
        if not isinstance(res, list):
            continue
        for item in res:
            key = _norm_url_key(item.get("url", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
