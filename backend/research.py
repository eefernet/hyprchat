"""
Research engines — deep research, conspiracy research, and search helpers.
"""
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime

import httpx

import config
from research_config import (
    REPORT_TEMPLATE_MAP,
    REPORT_TEMPLATES,
    RESEARCH_DEPTH_BUDGETS as _RESEARCH_DEPTH_BUDGETS,
    effective_context_chars as _effective_context_chars,
    research_depth_budget as _research_depth_budget,
    research_num_ctx as _research_num_ctx,
)

# model_providers transitively imports database (aiosqlite). research is
# imported by quick_search/search_agent, whose tests run without aiosqlite —
# keep this module importable there with a pass-through fallback.
try:
    from model_providers import complete_chat, is_cloud_model, stream_provider_chat, strip_leaked_cot
except ModuleNotFoundError:  # test envs without aiosqlite
    async def complete_chat(*_args, **_kwargs):
        return ""

    def is_cloud_model(_model_id):
        return False

    async def stream_provider_chat(*_args, **_kwargs):
        if False:
            yield {}

    def strip_leaked_cot(text):
        return text, ""


_WEB_FETCH_CLIENT = None
_WEB_FETCH_CLIENT_PROXY = None


def _outbound_proxy_url() -> str:
    return (getattr(config, "OUTBOUND_PROXY_URL", "") or "").strip()


def _web_fetch_client(http):
    """Return the normal client or a proxy-scoped client for public web fetches."""
    proxy = _outbound_proxy_url()
    if not proxy:
        return http
    global _WEB_FETCH_CLIENT, _WEB_FETCH_CLIENT_PROXY
    if _WEB_FETCH_CLIENT is None or _WEB_FETCH_CLIENT_PROXY != proxy:
        if _WEB_FETCH_CLIENT is not None:
            # Proxy setting changed — close the old client instead of leaking
            # its connection pool.
            try:
                asyncio.get_running_loop().create_task(_WEB_FETCH_CLIENT.aclose())
            except RuntimeError:
                pass
        _WEB_FETCH_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            verify=getattr(config, "HTTP_VERIFY_SSL", True),
            proxy=proxy,
            trust_env=False,
        )
        _WEB_FETCH_CLIENT_PROXY = proxy
    return _WEB_FETCH_CLIENT


async def close_web_fetch_client() -> None:
    global _WEB_FETCH_CLIENT, _WEB_FETCH_CLIENT_PROXY
    if _WEB_FETCH_CLIENT is not None:
        await _WEB_FETCH_CLIENT.aclose()
    _WEB_FETCH_CLIENT = None
    _WEB_FETCH_CLIENT_PROXY = None


async def web_get(http, url: str, **kwargs):
    return await _web_fetch_client(http).get(url, **kwargs)


def web_stream(http, method: str, url: str, **kwargs):
    return _web_fetch_client(http).stream(method, url, **kwargs)


# ── Search rate-limit tuning ──
_SEARCH_BATCH_SIZE = 3

# ── Research page-fetch resource caps ──
_RESEARCH_PAGE_MAX_BYTES = 2 * 1024 * 1024     # body cap for research page reads
_RESEARCH_PAGE_MAX_CLEAN_CHARS = 400_000       # decoded-text cap before regex cleaning
_SEARCH_BATCH_DELAY_DEEP = 2.0          # seconds between batches in deep research
_SEARCH_BATCH_DELAY_CONSPIRACY = 2.5    # seconds between batches in conspiracy research


async def _batched_searches(queries, search_fn, *, delay,
                            before_batch=None, prepare=None, on_batch_done=None, sink=None):
    """Run search_fn over queries in _SEARCH_BATCH_SIZE batches with a rate-limit
    sleep between batches.

    before_batch: async () -> None, awaited at each batch start (cancel checks).
    prepare: (q) -> bool claim/dedup filter; a batch where every query is
        rejected is skipped entirely (no on_batch_done, no sleep).
    on_batch_done: async (done: int) -> None, done = queries attempted so far.
    sink: list extended in place per batch so partial results survive a
        mid-run cancellation raised from before_batch.
    """
    out = sink if sink is not None else []
    total = len(queries)
    for batch_start in range(0, total, _SEARCH_BATCH_SIZE):
        if before_batch is not None:
            await before_batch()
        batch = queries[batch_start:batch_start + _SEARCH_BATCH_SIZE]
        run = [q for q in batch if prepare is None or prepare(q)]
        if prepare is not None and not run:
            continue
        results = await asyncio.gather(*[search_fn(q) for q in run], return_exceptions=True)
        for res in results:
            if isinstance(res, list):
                out.extend(res)
        if on_batch_done is not None:
            await on_batch_done(batch_start + len(batch))
        if batch_start + _SEARCH_BATCH_SIZE < total:
            await asyncio.sleep(delay)
    return out

_THINK_BLOCK_RE = re.compile(r"(?is)<think\b[^>]*>.*?</think\s*>")
_THINK_OPEN_RE = re.compile(r"(?is)<think\b[^>]*>")
_THINK_CLOSE_RE = re.compile(r"(?is)</think\s*>")
_REPORT_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,2}[ \t]+\S")


def _clean_research_model_text(text: str, *, allow_empty: bool = False) -> str:
    """Remove reasoning leaks before report text is persisted or rendered."""
    raw = text or ""
    if not raw:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    # Some local reasoning models leak the opening tag, then stream a bare
    # closer after the reasoning preamble. If a closer remains, discard the
    # leading preamble and keep the answer after the last closer.
    last_close = None
    for m in _THINK_CLOSE_RE.finditer(cleaned):
        last_close = m
    if last_close:
        cleaned = cleaned[last_close.end():]
    cleaned = _THINK_OPEN_RE.sub("", cleaned)
    cleaned = _THINK_CLOSE_RE.sub("", cleaned)
    cot_clean, leaked = strip_leaked_cot(cleaned.strip())
    if leaked:
        cleaned = cot_clean
    cleaned = (cleaned or "").strip()
    if cleaned or allow_empty:
        return cleaned
    return raw.strip()


def _streamable_report_text(raw: str, *, final: bool = False) -> str:
    """Return only content that is safe to show while final report streams.

    Before the report's first markdown heading is visible, keep buffering so a
    delayed </think> cannot expose a reasoning preamble in the live panel.
    """
    text = raw or ""
    if not text:
        return ""
    close = None
    for m in _THINK_CLOSE_RE.finditer(text):
        close = m
    if close:
        return _clean_research_model_text(text[close.end():], allow_empty=True)
    heading = _REPORT_HEADING_RE.search(text)
    if heading:
        return _clean_research_model_text(text[heading.start():], allow_empty=True)
    if final:
        return _clean_research_model_text(text, allow_empty=True)
    return ""


# ── URL safety, source credibility, and report evidence cache ──
_DNS_CACHE: dict[str, tuple[float, bool]] = {}
_DNS_CACHE_TTL = 300
_REPORT_EVIDENCE_CACHE: "OrderedDict[str, tuple[float, list[dict]]]" = OrderedDict()
_REPORT_EVIDENCE_CACHE_TTL = 3600
_REPORT_EVIDENCE_CACHE_MAX = 64

_VISUAL_REPORT_GUIDANCE = """Visual and quantitative reporting requirements:
- Use KaTeX for formulas: `$...$` for inline math and `$$...$$` for display equations.
- Use Mermaid fences for timelines, workflows, evidence maps, entity relationships, and decision trees.
- Use `chart` fences with Chart.js JSON for quantitative data. `pygraph` is an alias for the same safe chart schema; it is not Python execution.
- Use tables for comparisons and structured tradeoffs.
- Use GitHub-style callouts (`> [!NOTE]`, `> [!WARNING]`, `> [!IMPORTANT]`) for caveats, limitations, and warnings.
- When calculations are needed, rely on computed values from extracted data or backend/tool computation where possible, then render the formula/chart from those values. Do not hand-estimate chart data.
"""


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _host_blocked(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        return not _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        return False


def _resolve_host_public(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
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


def _url_safe_for_direct_fetch(url: str, *, resolve_dns: bool = False) -> bool:
    """Guard user-provided and search-result URLs before network fetches."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname:
            return False
        host = p.hostname.lower().rstrip(".")
        if _host_blocked(host):
            return False
        if resolve_dns:
            return _resolve_host_public(host)
        return True
    except Exception:
        return False


async def _url_safe_for_fetch(url: str, *, resolve_dns: bool = False) -> bool:
    if not _url_safe_for_direct_fetch(url, resolve_dns=False):
        return False
    if not resolve_dns:
        return True
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
        return await asyncio.to_thread(_resolve_host_public, host)
    except Exception:
        return False


async def fetch_bytes_safely(
    http,
    url: str,
    *,
    timeout: float = 15,
    headers: dict | None = None,
    max_bytes: int = 1024 * 1024,
    max_redirects: int = 5,
    resolve_dns: bool = True,
) -> tuple[int, dict, str, bytes]:
    """Fetch a URL with SSRF checks before every redirect target."""
    current = (url or "").strip()
    if not current:
        raise ValueError("URL is required")

    for _ in range(max_redirects + 1):
        if not await _url_safe_for_fetch(current, resolve_dns=resolve_dns):
            raise ValueError("Unsafe URL")

        async with web_stream(
            http,
            "GET",
            current,
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
        ) as r:
            location = r.headers.get("location", "")
            if 300 <= r.status_code < 400 and location:
                current = urllib.parse.urljoin(current, location)
                continue

            chunks = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if max_bytes and total > max_bytes:
                    raise ValueError("Response too large")
                chunks.append(chunk)

            final_url = str(r.url)
            if not await _url_safe_for_fetch(final_url, resolve_dns=resolve_dns):
                raise ValueError("Unsafe redirect target")
            return r.status_code, dict(r.headers), final_url, b"".join(chunks)

    raise ValueError("Too many redirects")


async def _fetch_text_safely(
    http, url: str, *, timeout: float = 15, headers: dict | None = None,
    max_bytes: int = _RESEARCH_PAGE_MAX_BYTES,
) -> tuple[int, dict, str, str] | None:
    """fetch_bytes_safely + charset decode. None on unsafe URL/redirect or oversize body."""
    try:
        status, hdrs, final_url, body = await fetch_bytes_safely(
            http, url, timeout=timeout, headers=headers, max_bytes=max_bytes,
        )
    except ValueError:
        return None
    ct = hdrs.get("content-type", "") or ""
    m = re.search(r"charset=([^;\s]+)", ct, re.I)
    enc = (m.group(1) if m else "utf-8").strip("\"'")
    try:
        text = body.decode(enc, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return status, hdrs, final_url, text[:_RESEARCH_PAGE_MAX_CLEAN_CHARS]


_SUSPICIOUS_TLDS = {
    "zip", "mov", "click", "country", "kim", "loan", "men", "party", "review",
    "science", "stream", "top", "trade", "work", "xyz",
}
_LOW_CREDIBILITY_DOMAINS = {
    "reddit.com", "quora.com", "medium.com", "dev.to", "tiktok.com",
    "facebook.com", "x.com", "twitter.com", "instagram.com", "pinterest.com",
}
_MAJOR_NEWS_DOMAINS = {
    "apnews.com", "reuters.com", "bbc.com", "bbc.co.uk", "npr.org", "pbs.org",
    "bloomberg.com", "wsj.com", "nytimes.com", "washingtonpost.com",
    "theguardian.com", "politico.com", "axios.com",
}


def _registrable_domain_from_host(host: str) -> str:
    host = (host or "").lower().split(":", 1)[0].removeprefix("www.").rstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _score_source_credibility(url: str, source_type: str = "web", tier: int | None = None) -> dict:
    """Return a 0-100 credibility score plus human-readable factors.

    This is a ranking/audit signal, not a hard filter. Low-score sources stay
    available so disputed topics can still surface contradictions.
    """
    score = 50
    factors: list[str] = []
    try:
        p = urllib.parse.urlsplit(url or "")
    except Exception:
        p = urllib.parse.SplitResult("", "", "", "", "")
    host = (p.hostname or "").lower().rstrip(".")
    domain = _registrable_domain_from_host(host)
    source_type_l = (source_type or "web").lower()

    if source_type_l in {"uploaded", "file", "direct_file"}:
        score += 20
        factors.append("user-provided file")
    if source_type_l == "github_repo":
        score += 18
        factors.append("direct GitHub repository snapshot")
    if p.scheme == "https":
        score += 8
        factors.append("HTTPS")
    elif p.scheme == "http":
        score -= 8
        factors.append("plain HTTP")
    elif not url:
        factors.append("local or uploaded context")

    if tier is None:
        try:
            tier = _source_tier(url)
        except Exception:
            tier = 2
    if tier == 0:
        score += 20
        factors.append("primary-source tier")
    elif tier == 1:
        score += 12
        factors.append("investigative or archival tier")
    elif tier == 3:
        score += 5
        factors.append("secondary fact-check tier")

    if host.endswith((".gov", ".mil")):
        score += 16
        factors.append("government domain")
    elif host.endswith(".edu"):
        score += 12
        factors.append("education domain")
    elif domain in _MAJOR_NEWS_DOMAINS:
        score += 9
        factors.append("major news domain")
    elif "docs." in host or "/docs" in (p.path or "").lower():
        score += 7
        factors.append("documentation path")

    if domain in _LOW_CREDIBILITY_DOMAINS:
        score -= 16
        factors.append("community/social platform")
    if source_type_l in {"forum", "social"}:
        score -= 10
        factors.append("forum/social source type")
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if tld in _SUSPICIOUS_TLDS:
        score -= 12
        factors.append(f"suspicious TLD .{tld}")
    if host.startswith("xn--") or ".xn--" in host:
        score -= 8
        factors.append("punycode hostname")
    if len(host) > 55 or host.count("-") >= 4:
        score -= 6
        factors.append("unusual hostname shape")
    if re.search(r"/(login|signup|cart|checkout|tag|category|author|search)(/|$)", p.path or "", re.I):
        score -= 4
        factors.append("low-evidence path")

    score = max(0, min(100, int(round(score))))
    if not factors:
        factors.append("standard web source")
    return {"score": score, "factors": factors[:8]}


def _apply_source_quality(src: dict) -> dict:
    tier = src.get("tier")
    try:
        tier = int(tier)
    except Exception:
        tier = _source_tier(src.get("url", ""))
    cred = _score_source_credibility(src.get("url", ""), src.get("type", "web"), tier)
    src["tier"] = tier
    src["tier_label"] = _source_tier_label(tier)
    src["credibility_score"] = cred["score"]
    src["credibility_factors"] = cred["factors"]
    return src


async def _search_google_fallback(http, query: str, count: int = 10) -> list:
    """Fallback: scrape Google search results when SearXNG is down."""
    try:
        params = urllib.parse.urlencode({"q": query, "num": count, "hl": "en"})
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        r = await web_get(http, f"https://www.google.com/search?{params}", timeout=12, headers=headers, follow_redirects=True)
        if r.status_code != 200:
            return []
        html = r.text
        results = []
        for m in re.finditer(r'<a[^>]+href="(/url\?q=([^"&]+)&|([^"]+))"[^>]*>(.*?)</a>', html, re.DOTALL):
            url = urllib.parse.unquote(m.group(2) or m.group(3) or "")
            if not url.startswith("http") or "google.com" in url or "accounts.google" in url:
                continue
            title_html = m.group(4) or ""
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            if not title or len(title) < 5:
                continue
            snippet = ""
            pos = m.end()
            nearby = html[pos:pos+600]
            snip_m = re.search(r'<span[^>]*>((?:(?!</span>).){20,300})</span>', nearby, re.DOTALL)
            if snip_m:
                snippet = re.sub(r'<[^>]+>', '', snip_m.group(1)).strip()
            if url not in [r["url"] for r in results]:
                results.append({
                    "title": title[:200], "url": url,
                    "content": snippet[:500],
                    "engine": "google-fallback", "score": 50,
                    "thumbnail": "", "type": "web",
                })
            if len(results) >= count:
                break
        return results
    except Exception as e:
        print(f"[SEARCH] Google fallback failed: {e}")
        return []


async def _search_searxng(
    http, searxng_url: str, query: str, count: int = 10,
    categories: str = "general", safesearch: str | None = None,
    time_range: str | None = None, engines: str | None = None,
    fallback_state: dict | None = None,
) -> list:
    """Search SearXNG and return structured results. Falls back to Google scrape if SearXNG returns nothing.

    `time_range`: SearXNG accepts day|week|month|year. Set to "month" for news
    queries with explicit time-cues so 2019 articles don't outrank current ones.
    """
    results = []
    try:
        _params = {"q": query, "format": "json", "language": "en", "categories": categories}
        if safesearch is not None:
            _params["safesearch"] = safesearch
        if time_range:
            _params["time_range"] = time_range
        if engines:
            _params["engines"] = engines
        params = urllib.parse.urlencode(_params)
        r = await http.get(f"{searxng_url}/search?{params}", timeout=12)
        if r.status_code == 429:
            await asyncio.sleep(3.0)
            r = await http.get(f"{searxng_url}/search?{params}", timeout=12)
        if r.status_code >= 400:
            return []
        data = r.json()
        for item in data.get("results", [])[:count]:
            url = item.get("url", "")
            url_lower = url.lower()
            thumbnail = item.get("thumbnail") or item.get("img_src") or ""
            r_type = "web"
            if "youtube.com/watch" in url_lower or "youtu.be/" in url_lower:
                r_type = "youtube"
                vid_id = None
                if "youtube.com/watch" in url_lower:
                    qs = url.split("?", 1)[1] if "?" in url else ""
                    for part in qs.split("&"):
                        if part.startswith("v="):
                            vid_id = part[2:].split("&")[0]; break
                elif "youtu.be/" in url_lower:
                    vid_id = url.split("youtu.be/")[1].split("?")[0].split("/")[0]
                if vid_id:
                    thumbnail = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
            elif any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                # The URL itself points at an image file. A web page that
                # merely advertises an og:image thumbnail is still a "web"
                # result — keep its type so the chat ranker doesn't drop it.
                r_type = "image"
            results.append({
                "title": item.get("title", ""), "url": url,
                "content": (item.get("content", "") or "")[:500],
                "engine": item.get("engine", ""), "score": item.get("score", 0),
                "thumbnail": thumbnail, "type": r_type,
                "published_date": (
                    item.get("publishedDate") or item.get("published_date")
                    or item.get("date") or item.get("pubDate") or ""
                ),
            })
        for box in data.get("infoboxes", []):
            results.append({
                "title": box.get("infobox", "Infobox"),
                "url": (box.get("urls", [{}])[0].get("url", "") if box.get("urls") else ""),
                "content": box.get("content", ""), "engine": "infobox", "score": 100,
            })
    except Exception as e:
        print(f"[SEARCH] SearXNG failed for {query[:60]!r}: {type(e).__name__}: {e}")
    # Fallback to Google scrape if SearXNG returned nothing. Callers can pass
    # a shared fallback_state ({"remaining": N}) to cap scrapes per run —
    # an unhealthy SearXNG at depth 5 would otherwise fire ~24 rapid Google
    # requests and get captcha'd into silently empty results.
    if not results:
        if fallback_state is not None:
            if fallback_state.get("remaining", 0) <= 0:
                print(f"[SEARCH] Google fallback budget exhausted; skipping for {query[:60]!r}")
                return results
            fallback_state["remaining"] -= 1
        results = await _search_google_fallback(http, query, count)
    return results


async def _search_wikileaks(http, searxng_url: str, query: str, count: int = 15) -> list:
    """Search WikiLeaks directly via their search API, with SearXNG fallback."""
    results = []
    try:
        params = urllib.parse.urlencode({"query": query, "include_onion": "false"})
        r = await web_get(
            http,
            f"https://search.wikileaks.org/?{params}",
            timeout=15,
            headers={"Accept": "application/json, text/html", "User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            try:
                data = r.json()
                hits = data.get("hits", {})
                items = hits.get("hits", []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
                if not items:
                    items = data.get("results", [])
                for item in items[:count]:
                    src = item.get("_source", item)
                    title = (src.get("title") or src.get("subject") or src.get("from") or
                             src.get("filename") or "WikiLeaks Document")
                    url = src.get("url") or src.get("link") or src.get("permalink") or ""
                    body = (src.get("description") or src.get("content") or src.get("body") or
                            src.get("text") or src.get("summary") or "")
                    if not url:
                        continue
                    results.append({
                        "title": f"🔓 {title}",
                        "url": url,
                        "content": body[:500],
                        "engine": "wikileaks",
                        "score": item.get("_score", 0),
                        "thumbnail": "",
                        "type": "web",
                    })
            except Exception:
                text = r.text
                for m in re.finditer(r'href="(https?://wikileaks\.org/[^"]+)"[^>]*>([^<]{5,200})<', text):
                    url, title = m.group(1), m.group(2).strip()
                    if url not in [x["url"] for x in results]:
                        results.append({
                            "title": f"🔓 {title}",
                            "url": url,
                            "content": "",
                            "engine": "wikileaks",
                            "score": 0,
                            "thumbnail": "",
                            "type": "web",
                        })
                    if len(results) >= count:
                        break
    except Exception:
        pass

    if len(results) < 8:
        try:
            wl_srx = await _search_searxng(http, searxng_url, f"{query} site:wikileaks.org", min(count, 10))
            for r in wl_srx:
                if r.get("url") and r["url"] not in [x["url"] for x in results]:
                    r["title"] = f"🔓 {r['title']}"
                    results.append(r)
        except Exception:
            pass

    return results[:count]


# WikiLeaks collection URLs
_WL_COLLECTIONS = {
    "plusd":        ("US Diplomatic Cables",       "https://wikileaks.org/plusd/"),
    "vault7":       ("CIA Vault 7 — Cyber Tools",  "https://wikileaks.org/vault7/"),
    "gifiles":      ("Stratfor Global Intel Files","https://wikileaks.org/gifiles/"),
    "dnc":          ("DNC Email Archive",          "https://wikileaks.org/dnc-emails/"),
    "podesta":      ("Podesta Email Archive",       "https://wikileaks.org/podesta-emails/"),
    "nsa":          ("NSA/GCHQ Surveillance Docs", "https://wikileaks.org/nsa-aff/"),
    "spyfiles":     ("Spy Files — Surveillance Tech","https://wikileaks.org/spyfiles/"),
    "saudi":        ("Saudi Cables",               "https://wikileaks.org/saudi-cables/"),
    "syria":        ("Syria Files",                "https://wikileaks.org/syria-files/"),
    "hbgary":       ("HBGary Email Leak",          "https://wikileaks.org/hbgary-emails/"),
    "sony":         ("Sony Email Archive",         "https://wikileaks.org/sony/emails/"),
    "tpp":          ("Trans-Pacific Partnership",  "https://wikileaks.org/tpp/"),
    "ttip":         ("TTIP Trade Docs",            "https://wikileaks.org/ttip/"),
    "collateral":   ("Collateral Murder Video",    "https://collateralmurder.wikileaks.org/"),
    "afghanistan":  ("Afghanistan War Diary",      "https://wikileaks.org/afg/"),
    "iraq":         ("Iraq War Logs",              "https://wikileaks.org/iraq/"),
    "guantanamo":   ("Guantanamo Files",           "https://wikileaks.org/gitmo/"),
}

def _wikileaks_collections_for_topic(topic_lower: str) -> list[str]:
    """Return relevant WikiLeaks collection keys for a given topic."""
    cols = []
    kw = {
        "plusd":       ["diplomat", "cable", "state department", "embassy", "foreign policy", "cia", "nsa", "saudi", "iran", "israel", "russia", "china"],
        "vault7":      ["cia", "hacking", "cyber", "malware", "exploit", "surveillance", "tool", "weeping angel", "marble", "vault 7", "vault7"],
        "gifiles":     ["stratfor", "intelligence", "corporate spy", "global intel", "bhopal", "occupy", "cartel"],
        "dnc":         ["dnc", "democrat", "clinton", "hillary", "bernie sanders", "election", "primary", "debbie wasserman"],
        "podesta":     ["podesta", "clinton", "hillary", "pizza", "comet", "spirit cooking", "election", "campaign", "email"],
        "nsa":         ["nsa", "gchq", "prism", "five eyes", "surveillance", "snowden", "xkeyscore", "spy"],
        "spyfiles":    ["surveillance", "spy", "imsi", "stingray", "finspy", "finfisher", "hack team", "hacking team", "gamma group"],
        "saudi":       ["saudi", "bin salman", "mbs", "oil", "opec", "khashoggi", "aramco", "middle east"],
        "syria":       ["syria", "assad", "aleppo", "rebel", "isis", "isil", "middle east"],
        "hbgary":      ["hbgary", "aaron barr", "anonymous", "nsa", "cia contractor", "cyber"],
        "sony":        ["sony", "hack", "nk", "north korea", "email"],
        "tpp":         ["tpp", "trade", "pacific", "corporate", "secret trade"],
        "ttip":        ["ttip", "trade", "europe", "corporate"],
        "collateral":  ["iraq", "war", "helicopter", "murder", "civilian", "military", "apache"],
        "afghanistan": ["afghanistan", "afghan", "war diary", "military", "ied", "taliban"],
        "iraq":        ["iraq", "war", "baghdad", "military", "civilian", "mosul"],
        "guantanamo":  ["guantanamo", "gitmo", "detainee", "prisoner", "torture", "enhanced"],
    }
    for col, keywords in kw.items():
        if any(k in topic_lower for k in keywords):
            cols.append(col)
    return cols


def _extract_seed_urls(*texts: str, limit: int = 12) -> list[str]:
    """Extract user-provided URLs that should be treated as first-class sources."""
    urls = []
    seen = set()
    for text in texts:
        for raw in re.findall(r"https?://[^\s<>\]\[\"'`]+", text or "", flags=re.I):
            url = raw.rstrip(").,;:!?")
            norm = _normalize_url(url)
            if norm and norm not in seen:
                seen.add(norm)
                urls.append(norm)
            if len(urls) >= limit:
                return urls
    return urls


def _github_repo_from_url(url: str) -> tuple[str, str] | None:
    try:
        p = urllib.parse.urlsplit(url)
        if p.netloc.lower() not in {"github.com", "www.github.com"}:
            return None
        parts = [part for part in p.path.strip("/").split("/") if part]
        if len(parts) < 2:
            return None
        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            return None
        return owner, repo
    except Exception:
        return None


def _github_file_score(path: str, size: int) -> int:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    score = 0
    if name in {"readme.md", "readme", "agents.md", "claude.md"}:
        score += 120
    if name in {"package.json", "pyproject.toml", "requirements.txt", "dockerfile", "compose.yaml", "docker-compose.yml"}:
        score += 105
    if name in {"vite.config.js", "vite.config.ts", "tsconfig.json", "webpack.config.js"}:
        score += 95
    if p in {"frontend/src/main.jsx", "backend/main.py", "backend/research.py", "backend/database.py", "deploy_monitor.py"}:
        score += 90
    if p.startswith(("frontend/", "backend/", "src/", "app/", "components/")):
        score += 35
    if p.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".md", ".json", ".toml", ".yml", ".yaml")):
        score += 25
    if "/node_modules/" in p or "/.git/" in p or "/dist/assets/" in p or "/__pycache__/" in p:
        score -= 200
    if size > 220000:
        score -= 35
    return score


async def _fetch_github_repo_snapshot(http, url: str) -> dict | None:
    repo = _github_repo_from_url(url)
    if not repo:
        return None
    owner, name = repo
    headers = {
        "User-Agent": "HyprChat-DeepResearch/1.0",
        "Accept": "application/vnd.github+json",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        meta_r = await web_get(http, f"https://api.github.com/repos/{owner}/{name}", headers=headers, timeout=15)
        if meta_r.status_code >= 400:
            return None
        meta = meta_r.json()
        branch = meta.get("default_branch") or "main"
        tree_r = await web_get(
            http,
            f"https://api.github.com/repos/{owner}/{name}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1",
            headers=headers,
            timeout=20,
        )
        if tree_r.status_code >= 400:
            return None
        tree = tree_r.json().get("tree") or []
        blobs = [x for x in tree if x.get("type") == "blob" and x.get("path")]
        ranked = sorted(
            blobs,
            key=lambda x: (_github_file_score(x.get("path", ""), int(x.get("size") or 0)), -len(x.get("path", ""))),
            reverse=True,
        )
        selected = [x for x in ranked if _github_file_score(x.get("path", ""), int(x.get("size") or 0)) > 0][:14]
        async def _fetch_blob_text(item: dict) -> str | None:
            path = item.get("path", "")
            file_url = f"https://api.github.com/repos/{owner}/{name}/contents/{urllib.parse.quote(path, safe='/')}"
            try:
                file_headers = {**headers, "Accept": "application/vnd.github.raw"}
                fr = await web_get(http, file_url, params={"ref": branch}, headers=file_headers, timeout=15)
                if fr.status_code >= 400:
                    return None
                return fr.text
            except Exception:
                return None

        blob_texts: list[str | None] = []
        for i in range(0, len(selected), 5):
            blob_texts.extend(await asyncio.gather(*[_fetch_blob_text(x) for x in selected[i:i + 5]]))

        file_sections = []
        total_chars = 0
        for item, text in zip(selected, blob_texts):
            if text is None or "\x00" in text:
                continue
            remaining = 70000 - total_chars
            if remaining <= 2000:
                break
            excerpt = text[:min(18000, remaining)]
            total_chars += len(excerpt)
            file_sections.append(f"## File: {item.get('path', '')}\n```text\n{excerpt}\n```")
        top_tree = "\n".join(f"- {x.get('path')} ({x.get('size', 0)} bytes)" for x in ranked[:80])
        content = (
            f"# GitHub Repository Snapshot: {owner}/{name}\n"
            f"URL: https://github.com/{owner}/{name}\n"
            f"Default branch: {branch}\n"
            f"Description: {meta.get('description') or ''}\n"
            f"Stars: {meta.get('stargazers_count', 0)}\n"
            f"Primary language: {meta.get('language') or 'unknown'}\n\n"
            f"## Repository tree excerpt\n{top_tree}\n\n"
            + "\n\n".join(file_sections)
        )
        return {
            "url": f"https://github.com/{owner}/{name}",
            "title": f"GitHub repository snapshot: {owner}/{name}",
            "content": content[:76000],
            "default_branch": branch,
            "files": [x.get("path") for x in selected],
        }
    except Exception as e:
        print(f"[RESEARCH REPORT] GitHub snapshot failed for {url}: {e}")
        return None


def _clean_html_text(text: str, *,
                     tags=("script", "style", "nav", "header", "footer", "aside", "noscript"),
                     structure: bool = True) -> str:
    """Strip boilerplate tags and normalize HTML into plain text.

    structure=True also converts h1-3/li/p/br into markdown-ish markers,
    decodes common entities, and removes PGP blocks (the _fetch_page /
    _fetch_wikileaks_page behavior); structure=False is the lighter
    _fetch_gov_doc_index variant (tag strip + whitespace collapse only).
    """
    for tag in tags:
        text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if structure:
        text = re.sub(r"<h[1-3][^>]*>(.*?)</h[1-3]>", r"\n## \1\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n• \1", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    if structure:
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&\w+;", " ", text)
        text = re.sub(r"-----BEGIN PGP [A-Z ]+-----.*?-----END PGP [A-Z ]+-----", "[PGP block removed]", text, flags=re.DOTALL)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+" if structure else r"\n\s*\n+", "\n\n", text)
    return text.strip()


async def _fetch_page(http, url: str) -> dict | None:
    """Fetch and clean a web page."""
    skip = ["youtube.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
            ".pdf", "linkedin.com", "tiktok.com",
            "snopes.com", "politifact.com", "factcheck.org", "leadstories.com",
            "fullfact.org", "mediabiasfactcheck.com"]
    if any(p in url.lower() for p in skip):
        return None
    try:
        fetched = await _fetch_text_safely(
            http, url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
        if not fetched:
            return None
        status, hdrs, final_url, text = fetched
        if status >= 400:
            return None
        ct = hdrs.get("content-type", "")
        if "text" not in ct and "json" not in ct:
            return None
        text = _clean_html_text(text)
        if len(text) < 200:
            return None
        return {"url": _normalize_url(final_url), "content": text[:6000]}
    except Exception:
        return None


async def _fetch_gov_doc_index(http, url: str) -> dict | None:
    """Fetch government document index pages (including PDF links) for conspiracy research."""
    try:
        fetched = await _fetch_text_safely(
            http, url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"})
        if not fetched:
            return None
        status, hdrs, final_url, text = fetched
        if status >= 400:
            return None
        ct = hdrs.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return None
        pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', text, re.IGNORECASE)
        doc_links = re.findall(r'href=["\']([^"\']*(?:document|file|exhibit|report)[^"\']*)["\']', text, re.IGNORECASE)
        text = _clean_html_text(text, tags=("script", "style", "nav", "header", "footer"), structure=False)
        result = {"url": _normalize_url(final_url), "content": text[:5000], "pdf_links": [], "doc_links": []}
        for lnk in pdf_links[:20]:
            result["pdf_links"].append(urllib.parse.urljoin(final_url, lnk))
        for lnk in doc_links[:10]:
            result["doc_links"].append(urllib.parse.urljoin(final_url, lnk))
        return result
    except Exception:
        return None


# ── Source tier scoring for evidence-first prioritization ──
_TIER1_PRIMARY = [
    "wikileaks.org", "archives.gov", "cia.gov/readingroom", "vault.fbi.gov",
    "courtlistener.com", "documentcloud.org", "muckrock.com", "pacer.gov",
    "sec.gov/edgar", "cryptome.org", "ddosecrets.com", "theblackvault.com",
    "foia.state.gov",
]
_TIER2_INVESTIGATIVE = [
    "theintercept.com", "bellingcat.com", "propublica.org", "archive.org",
    "substack.com", "thegrayzone.com", "mintpressnews.com",
]
_TIER4_FACTCHECK = [
    "snopes.com", "politifact.com", "factcheck.org", "leadstories.com",
    "fullfact.org", "reuters.com/fact-check", "apnews.com/fact-check",
    "mediabiasfactcheck.com", "usatoday.com/fact-check",
    "washingtonpost.com/fact-checker",
]


def _source_tier(url: str) -> int:
    """Score a URL by source tier: 0=primary evidence, 1=investigative, 2=general, 3=fact-checker."""
    ul = url.lower()
    if any(d in ul for d in _TIER1_PRIMARY):
        return 0
    if any(d in ul for d in _TIER2_INVESTIGATIVE):
        return 1
    if any(d in ul for d in _TIER4_FACTCHECK):
        return 3
    return 2


def _source_tier_label(tier) -> str:
    try:
        tier_i = int(tier)
    except Exception:
        tier_i = 2
    return {
        0: "primary evidence",
        1: "investigative / archival",
        2: "general web / community",
        3: "fact-checker / secondary review",
    }.get(tier_i, "general web / community")


async def _fetch_wikileaks_page(http, url: str) -> dict | None:
    """Fetch a WikiLeaks page, extracting article text and document/PDF links."""
    lower = url.lower()
    if any(lower.endswith(ext) for ext in (".zip", ".tar", ".gz", ".rar", ".7z")):
        return {"url": url, "content": f"[Archive file — direct download: {url}]"}
    if ".pdf" in lower:
        return {"url": url, "content": f"[PDF document — direct download: {url}]"}
    try:
        fetched = await _fetch_text_safely(
            http, url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"})
        if not fetched:
            return None
        status, hdrs, final_url, text = fetched
        if status != 200:
            return None
        ct = hdrs.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return None

        wl_links = re.findall(r'href=["\']((https?://(?:www\.)?wikileaks\.org)?(/[^"\'#?][^"\']*?))["\']', text, re.IGNORECASE)
        pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', text, re.IGNORECASE)

        doc_links = []
        for match in wl_links[:30]:
            full = match[0] if match[0].startswith("http") else urllib.parse.urljoin(final_url, match[2])
            if full != url and full not in doc_links:
                doc_links.append(full)
        pdf_full = []
        for lnk in pdf_links[:15]:
            pdf_full.append(urllib.parse.urljoin(final_url, lnk))

        text = _clean_html_text(text)

        if len(text) < 100:
            return None

        result: dict = {"url": _normalize_url(final_url), "content": text[:6000]}
        if doc_links:
            result["doc_links"] = doc_links
        if pdf_full:
            result["pdf_links"] = pdf_full
            result["content"] += "\n\n**PDF documents found:**\n" + "\n".join(f"• {p}" for p in pdf_full[:10])
        return result
    except Exception:
        return None


def _extract_entities(text: str, topic_words: set) -> set:
    """Extract key entities from text."""
    entities = set()
    caps = re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\b", text)
    for term in caps:
        if term.lower() not in topic_words and len(term) > 5:
            entities.add(term)
    quoted = re.findall(r'"([^"]{4,40})"', text)
    for term in quoted:
        if "<" not in term:
            entities.add(term)
    skip_acr = {"THE","AND","FOR","NOT","BUT","ARE","WAS","HAS","ITS","THIS","THAT","WITH","FROM","HTML","HTTP","URL","API"}
    for acr in re.findall(r"\b([A-Z]{2,6})\b", text):
        if acr not in skip_acr and acr.lower() not in topic_words:
            entities.add(acr)
    return entities


def _rank_urls(findings: list, exclude: set = None) -> list:
    """Rank URLs by source quality."""
    exclude = exclude or set()
    scores = {}
    quality = {"wikipedia.org":10,"arxiv.org":9,"github.com":8,"stackoverflow.com":8,
               "nature.com":9,".gov":8,".edu":8,"reuters.com":8,"bbc.com":7,
               "arstechnica.com":7,"docs.":8,"medium.com":5,"dev.to":6}
    for f in findings:
        url = f.get("url", "")
        if not url or url in exclude:
            continue
        score = f.get("score", 0) or 0
        tier = _source_tier(url)
        credibility = _score_source_credibility(url, f.get("type", "web"), tier).get("score", 50)
        score += credibility / 12
        score += {0: 10, 1: 7, 2: 2, 3: 4}.get(tier, 2)
        for domain, bonus in quality.items():
            if domain in url.lower():
                score += bonus
                break
        if len(f.get("content", "")) > 200:
            score += 3
        skip = ["youtube.com","twitter.com","facebook.com",".pdf","linkedin.com"]
        if any(p in url.lower() for p in skip):
            score -= 100
        if url not in scores or score > scores[url]:
            scores[url] = score
    return sorted([u for u in scores if scores[u] > 0], key=lambda u: scores[u], reverse=True)


async def _ask_ollama(http, ollama_url: str, prompt: str, model: str = None, default_model: str = "qwen3.5:27b", max_tokens: int = 4096) -> str:
    """Call Ollama for AI synthesis."""
    _num_ctx = _research_num_ctx()
    model_id = model or default_model
    if is_cloud_model(model_id):
        text = await complete_chat(
            http, model_id, prompt,
            temperature=0.3,
            num_predict=max_tokens,
            timeout=300,
        )
        return _clean_research_model_text(text)
    try:
        r = await http.post(f"{ollama_url}/api/generate", json={
            "model": model_id,
            "prompt": prompt, "stream": False,
            "think": False,
            "options": {"temperature": 0.3, "num_predict": max_tokens, "num_ctx": _num_ctx},
        }, timeout=180)
        data = r.json()
        text = (data.get("response", "") or "").strip()
        return _clean_research_model_text(text)
    except Exception as e:
        return f"[AI synthesis failed: {e}]"


async def _ask_ollama_json(
    http, ollama_url: str, prompt: str, model: str = None,
    default_model: str = "qwen3.5:27b", max_tokens: int = 4096,
    fallback=None, expected_type=None,
):
    """Call Ollama in JSON mode and make one compact repair attempt."""
    _num_ctx = _research_num_ctx()
    model_id = model or default_model

    async def call_once(call_prompt: str) -> str:
        if is_cloud_model(model_id):
            return _clean_research_model_text(await complete_chat(
                http, model_id, call_prompt,
                temperature=0.1,
                num_predict=max_tokens,
                format_json=True,
                timeout=300,
            ))
        r = await http.post(f"{ollama_url}/api/generate", json={
            "model": model_id,
            "prompt": call_prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": 0.1,
                "num_predict": max_tokens,
                "num_ctx": _num_ctx,
            },
        }, timeout=180)
        data = r.json()
        return _clean_research_model_text(data.get("response", "") or "")

    def expected(obj) -> bool:
        if obj is None:
            return False
        if expected_type is not None and not isinstance(obj, expected_type):
            return False
        return True

    raw = ""
    try:
        raw = await call_once(prompt)
        parsed = _safe_json_obj(raw, None)
        if expected(parsed):
            return parsed

        if expected_type is list:
            shape = "array"
        elif expected_type is dict:
            shape = "object"
        elif isinstance(expected_type, tuple) and list in expected_type and dict in expected_type:
            shape = "array or object"
        else:
            shape = "JSON value"
        repair_prompt = f"""Repair this model output into valid JSON.

Return only one JSON {shape}. Do not include markdown, comments, or prose.
If the output cannot be repaired, return an empty JSON {shape}.

Original request excerpt:
{prompt[:2400]}

Invalid output:
{(raw or "[empty]")[:5000]}"""
        repaired = await call_once(repair_prompt)
        parsed = _safe_json_obj(repaired, None)
        if expected(parsed):
            return parsed
    except Exception as e:
        print(f"[RESEARCH REPORT] JSON Ollama call failed: {e}")
    return fallback


async def _ask_ollama_streamed(
    http, ollama_url: str, events, prompt: str, conv_id: str, tool_name: str,
    model: str = None, default_model: str = "qwen3.5:27b",
    max_tokens: int = 4096, status_prefix: str = "🧠 Synthesizing",
) -> str:
    """Stream from Ollama, emitting periodic status events so the user sees live progress."""
    _num_ctx = _research_num_ctx()
    accumulated = ""
    last_emit_len = 0
    try:
        async with http.stream("POST", f"{ollama_url}/api/generate", json={
            "model": model or default_model,
            "prompt": prompt, "stream": True,
            "think": False,
            "options": {"temperature": 0.3, "num_predict": max_tokens, "num_ctx": _num_ctx},
        }, timeout=300) as stream:
            async for line in stream.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                accumulated += chunk.get("response", "")
                if len(accumulated) - last_emit_len >= 180:
                    last_emit_len = len(accumulated)
                    approx_toks = len(accumulated) // 4
                    await events.emit(conv_id, "tool_start", {
                        "tool": tool_name, "icon": "search",
                        "status": f"{status_prefix}... ⟨{approx_toks}↑ tkns⟩",
                    })
                if chunk.get("done"):
                    break
        text = accumulated.strip()
        clean, leaked = strip_leaked_cot(text)
        return (clean or text) if leaked else text
    except Exception as e:
        return f"[AI synthesis failed: {e}]"


def _safe_json_obj(text: str, fallback):
    """Best-effort JSON parser for model output that may include markdown fences."""
    if not text:
        return fallback
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    candidates = [raw]
    m = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return fallback


def _one_line(text: str, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _evidence_collection_name(report_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", report_id or "report").strip("-")
    name = f"evidence-{safe}"[:63].strip("-")
    return name if len(name) >= 3 else "evidence-report"


def _stash_report_evidence_records(report_id: str, records: list[dict]) -> None:
    if not report_id:
        return
    _REPORT_EVIDENCE_CACHE[report_id] = (time.time(), records or [])
    _REPORT_EVIDENCE_CACHE.move_to_end(report_id)
    while len(_REPORT_EVIDENCE_CACHE) > _REPORT_EVIDENCE_CACHE_MAX:
        _REPORT_EVIDENCE_CACHE.popitem(last=False)


def _get_report_evidence_records(report_id: str) -> list[dict]:
    entry = _REPORT_EVIDENCE_CACHE.get(report_id)
    if not entry:
        return []
    ts, records = entry
    if time.time() - ts > _REPORT_EVIDENCE_CACHE_TTL:
        _REPORT_EVIDENCE_CACHE.pop(report_id, None)
        return []
    return records or []


def _evidence_tokens(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "there", "their",
        "about", "into", "over", "under", "what", "when", "where", "which",
        "would", "could", "should", "have", "has", "had", "are", "was", "were",
    }
    return {
        t.rstrip("s")
        for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_.+#-]{2,}", (text or "").lower())
        if t not in stop
    }


def _chunk_evidence_text(text: str, *, max_chars: int = 1800, overlap: int = 180) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            split_at = max(text.rfind(". ", start, end), text.rfind("; ", start, end), text.rfind(" ", start, end))
            if split_at > start + max_chars // 2:
                end = split_at + 1
        chunk = text[start:end].strip()
        if len(chunk) >= 80:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _build_report_evidence_records(
    report_id: str, query: str, user_context: str,
    sources: list[dict], pages: list[dict],
) -> list[dict]:
    records: list[dict] = []
    source_by_index = {}
    source_by_url = {}
    for src in sources or []:
        try:
            idx = int(src.get("index") or src.get("source_index") or 0)
        except Exception:
            idx = 0
        if idx:
            source_by_index[idx] = src
        if src.get("url"):
            source_by_url[_normalize_url(src.get("url", ""))] = src

    def add(kind: str, text: str, src: dict | None = None, title: str = "", url: str = "") -> None:
        if not text or len(text.strip()) < 80:
            return
        src = src or {}
        try:
            source_index = int(src.get("index") or src.get("source_index") or 0)
        except Exception:
            source_index = 0
        source_id = f"S{source_index}" if source_index else ("CTX" if kind in {"user_context", "kb_context"} else "S?")
        src_title = title or src.get("title") or src.get("filename") or source_id
        src_url = url or src.get("url") or ""
        credibility = src.get("credibility_score")
        if credibility is None:
            credibility = _score_source_credibility(src_url, src.get("type", kind), src.get("tier")).get("score", 50)
        for ci, chunk in enumerate(_chunk_evidence_text(text)):
            rec_id = hashlib.md5(f"{report_id}:{kind}:{source_id}:{src_url}:{ci}:{chunk[:80]}".encode()).hexdigest()
            records.append({
                "id": rec_id,
                "text": chunk,
                "kind": kind,
                "source_id": source_id,
                "source_index": source_index,
                "title": _one_line(src_title, 180),
                "url": src_url,
                "credibility_score": int(credibility or 0),
                "tier_label": src.get("tier_label") or _source_tier_label(src.get("tier", 2)),
                "query": query,
            })

    if user_context:
        add("user_context", user_context, {"type": "file", "title": "Uploaded/KB context", "credibility_score": 82})
    for src in sources or []:
        brief = " ".join([src.get("title", ""), src.get("snippet", ""), src.get("url", "")]).strip()
        add("source_brief", brief, src)
    for page in pages or []:
        src = None
        try:
            sid = int(page.get("source_index") or 0)
            src = source_by_index.get(sid)
        except Exception:
            src = None
        if not src and page.get("url"):
            src = source_by_url.get(_normalize_url(page.get("url", "")))
        add("full_page", page.get("content", ""), src, title=page.get("title", ""), url=page.get("url", ""))
    return records


async def _index_report_evidence(report_id: str, records: list[dict]) -> dict:
    """Embed report evidence into a per-report ChromaDB collection.

    The in-process cache is always populated, so retrieval still works when
    ChromaDB or the embedding model is unavailable.
    """
    records = records or []
    _stash_report_evidence_records(report_id, records)
    if not records:
        return {"chunks": 0, "embedded": 0, "fallback": True}
    try:
        import rag

        texts = [r["text"] for r in records]
        embeddings = await rag.embed_texts(texts)
        valid = [(r, e) for r, e in zip(records, embeddings) if e is not None]
        if not valid:
            return {"chunks": len(records), "embedded": 0, "fallback": True}

        def _do_upsert():
            # Sync chroma client calls; keep them off the event loop.
            collection = rag.get_chroma().get_or_create_collection(
                name=_evidence_collection_name(report_id),
                metadata={"hnsw:space": "cosine", "kind": "research_evidence"},
            )
            # Sliced: Chroma hard-caps a single upsert at ~5461 records.
            batch = rag.CHROMA_UPSERT_BATCH
            for s in range(0, len(valid), batch):
                part = valid[s:s + batch]
                collection.upsert(
                    ids=[r["id"] for r, _ in part],
                    documents=[r["text"] for r, _ in part],
                    metadatas=[{
                        "report_id": report_id,
                        "source_id": str(r.get("source_id") or ""),
                        "source_index": int(r.get("source_index") or 0),
                        "title": str(r.get("title") or "")[:300],
                        "url": str(r.get("url") or "")[:1000],
                        "kind": str(r.get("kind") or ""),
                        "credibility_score": int(r.get("credibility_score") or 0),
                        "tier_label": str(r.get("tier_label") or "")[:120],
                    } for r, _ in part],
                    embeddings=[e for _, e in part],
                )

        await asyncio.to_thread(_do_upsert)
        return {"chunks": len(records), "embedded": len(valid), "fallback": False}
    except Exception as e:
        print(f"[RESEARCH REPORT] Evidence index fallback for {report_id}: {e}")
        return {"chunks": len(records), "embedded": 0, "fallback": True, "error": str(e)[:240]}


def _lexical_retrieve_report_evidence(report_id: str, queries: list[str], top_k: int = 8) -> list[dict]:
    records = _get_report_evidence_records(report_id)
    if not records:
        return []
    q_tokens = _evidence_tokens(" ".join(queries or []))
    scored = []
    for rec in records:
        r_tokens = _evidence_tokens(" ".join([rec.get("title", ""), rec.get("text", "")]))
        if not r_tokens:
            continue
        overlap = len(q_tokens & r_tokens) if q_tokens else 1
        density = overlap / max(1, len(q_tokens)) if q_tokens else 0.2
        score = density * 0.72 + min(1.0, float(rec.get("credibility_score", 50)) / 100.0) * 0.20
        if rec.get("kind") == "full_page":
            score += 0.08
        scored.append((score, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    seen = set()
    for score, rec in scored:
        key = (rec.get("source_id"), rec.get("text", "")[:120])
        if key in seen:
            continue
        seen.add(key)
        item = dict(rec)
        item["score"] = round(score, 4)
        out.append(item)
        if len(out) >= top_k:
            break
    return out


async def _retrieve_report_evidence(report_id: str, queries: list[str], top_k: int = 8) -> list[dict]:
    queries = [q for q in (queries or []) if str(q or "").strip()]
    if not report_id or not queries:
        return []
    try:
        import rag

        query_text = "\n".join(queries)[:4000]
        emb = await rag.embed_single(query_text)
        if emb is None:
            return _lexical_retrieve_report_evidence(report_id, queries, top_k=top_k)
        def _do_query():
            # Sync chroma client calls; keep them off the event loop.
            collection = rag.get_chroma().get_collection(_evidence_collection_name(report_id))
            count = collection.count()
            if count <= 0:
                return None
            return collection.query(
                query_embeddings=[emb],
                n_results=min(max(1, top_k * 2), count),
                include=["documents", "metadatas", "distances"],
            )

        results = await asyncio.to_thread(_do_query)
        if results is None:
            return _lexical_retrieve_report_evidence(report_id, queries, top_k=top_k)
        out = []
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        seen = set()
        for doc, meta, dist in zip(docs, metas, dists):
            key = (meta.get("source_id"), doc[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "text": doc,
                "source_id": meta.get("source_id") or "",
                "source_index": meta.get("source_index") or 0,
                "title": meta.get("title") or "",
                "url": meta.get("url") or "",
                "kind": meta.get("kind") or "",
                "credibility_score": meta.get("credibility_score") or 0,
                "tier_label": meta.get("tier_label") or "",
                "score": round(1 - float(dist or 0), 4),
            })
            if len(out) >= top_k:
                break
        return out or _lexical_retrieve_report_evidence(report_id, queries, top_k=top_k)
    except Exception:
        return _lexical_retrieve_report_evidence(report_id, queries, top_k=top_k)


def _format_targeted_evidence(snippets: list[dict], max_chars: int = 14000) -> str:
    parts = []
    total = 0
    for snip in snippets or []:
        sid = snip.get("source_id") or "CTX"
        title = snip.get("title") or "Evidence"
        url = snip.get("url") or "uploaded/local"
        cred = snip.get("credibility_score", 0)
        header = f"[{sid}] {title} | credibility {cred}/100 | {url}"
        body = _one_line(snip.get("text", ""), 900)
        block = f"{header}\n{body}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def _normalize_source_ids(raw_ids, max_source_index: int) -> list[str]:
    ids = []
    for raw in raw_ids or []:
        text = str(raw).strip()
        matches = re.findall(r"\bS\s*(\d+)\b", text, re.I)
        if not matches and text.isdigit():
            matches = [text]
        for match in matches:
            try:
                idx = int(match)
            except Exception:
                continue
            if idx < 1 or idx > max_source_index:
                continue
            sid = f"S{idx}"
            if sid not in ids:
                ids.append(sid)
    return ids


def _normalize_research_findings(findings, max_source_index: int, max_findings: int = 16) -> list[dict]:
    clean = []
    if not isinstance(findings, list):
        return clean
    for item in findings[:max_findings]:
        if isinstance(item, str):
            item = {"claim": item}
        if not isinstance(item, dict):
            continue
        confidence = str(item.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        evidence_strength = str(item.get("evidence_strength") or "").strip().lower()
        if evidence_strength not in {"strong", "moderate", "thin", "anecdotal"}:
            evidence_strength = {"high": "strong", "medium": "moderate", "low": "thin"}.get(confidence, "moderate")
        clean.append({
            "finding_id": len(clean) + 1,
            "claim": _one_line(str(item.get("claim") or item.get("title") or "Finding"), 520),
            "evidence": _one_line(str(item.get("evidence") or item.get("summary") or ""), 700),
            "source_ids": _normalize_source_ids(item.get("source_ids") or item.get("sources"), max_source_index),
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "source_quality": _one_line(str(item.get("source_quality") or ""), 260),
            "caveat": _one_line(str(item.get("caveat") or item.get("limitation") or ""), 320),
            "implication": _one_line(str(item.get("implication") or ""), 420),
        })
    return clean


def _domain_from_url(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


def _source_is_community(src: dict) -> bool:
    url = (src.get("url") or "").lower()
    source_type = (src.get("type") or "").lower()
    community_markers = (
        "reddit.com", "news.ycombinator.com", "stackoverflow.com", "stackexchange.com",
        "medium.com", "dev.to", "substack.com", "github.com/issues",
        "github.com/discussions", "discourse.", "forum",
    )
    return source_type in {"forum", "social"} or any(marker in url for marker in community_markers)


def _best_evidence_sentence(text: str, topic: str = "", limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    topic_words = set(re.findall(r"[a-z0-9][a-z0-9_.+#-]{3,}", (topic or "").lower()))
    candidates = re.split(r"(?<=[.!?])\s+", text)
    if len(candidates) == 1:
        candidates = re.split(r"\s+[•\-–]\s+|\n+", text)
    best = ""
    best_score = -1
    for sent in candidates[:80]:
        sent = _one_line(sent, limit)
        if len(sent) < 35:
            continue
        words = set(re.findall(r"[a-z0-9][a-z0-9_.+#-]{3,}", sent.lower()))
        overlap = len(words & topic_words)
        score = overlap * 5 + min(len(sent), 220) / 55
        if re.search(r"\b(data|benchmark|official|report|study|docs?|issue|architecture|cost|risk|model|cloud|local|private)\b", sent, re.I):
            score += 2
        if score > best_score:
            best = sent
            best_score = score
    return best or _one_line(text, limit)


def _weak_source_finding() -> dict:
    return {
        "finding_id": 1,
        "claim": "The available source set was too weak for structured extraction.",
        "evidence": "The runner will still synthesize a report from the source briefs and available full text.",
        "source_ids": [],
        "confidence": "low",
        "evidence_strength": "thin",
        "source_quality": "insufficient",
        "caveat": "No source-backed findings were extracted.",
        "implication": "Treat conclusions as preliminary.",
    }


def _build_source_backed_findings(query: str, focus: str, sources: list[dict], pages: list[dict], max_findings: int = 16) -> list[dict]:
    """Deterministically create conservative findings from collected evidence."""
    if not sources and not pages:
        return [_weak_source_finding()]

    source_by_idx = {}
    for src in sources or []:
        try:
            idx = int(src.get("index") or 0)
        except Exception:
            idx = 0
        if idx > 0:
            source_by_idx[idx] = src

    pages_by_source = {}
    for page in pages or []:
        try:
            sid = int(page.get("source_index") or 0)
        except Exception:
            sid = 0
        content = (page.get("content") or "").strip()
        if sid <= 0 or not content:
            continue
        current = pages_by_source.get(sid)
        if not current or len(content) > len(current.get("content", "")):
            pages_by_source[sid] = page

    candidates = []
    for idx, src in source_by_idx.items():
        page = pages_by_source.get(idx)
        snippet = (src.get("snippet") or "").strip()
        title = (src.get("title") or "").strip()
        url = (src.get("url") or "").strip()
        text = (page.get("content") if page else "") or snippet or title or url
        if not (title or snippet or url or text):
            continue
        try:
            tier = int(src.get("tier", 2))
        except Exception:
            tier = 2
        candidates.append((0 if page else 1, tier, -len(text), idx, src, page, text))

    if not candidates:
        return [_weak_source_finding()]

    findings = []
    topic = f"{query} {focus or ''}".strip()
    for _, tier, _, idx, src, page, text in sorted(candidates)[:max_findings]:
        title = _one_line(src.get("title") or _domain_from_url(src.get("url", "")) or f"Source S{idx}", 140)
        domain = _domain_from_url(src.get("url", ""))
        sentence = _best_evidence_sentence(text, topic)
        has_page = bool(page and page.get("content"))
        is_community = _source_is_community(src)
        if tier == 0 and has_page and len(text) > 800:
            confidence = "high"
        elif has_page or tier in {0, 1, 3}:
            confidence = "medium"
        else:
            confidence = "low"
        if confidence == "high":
            strength = "strong"
        elif is_community and tier == 2:
            strength = "anecdotal"
        elif confidence == "medium":
            strength = "moderate"
        else:
            strength = "thin"
        if is_community:
            caveat = "Community or general-source evidence; treat as practical signal rather than primary empirical proof."
        elif not has_page:
            caveat = "Based on the collected source brief/snippet; the full page was not read during fallback extraction."
        else:
            caveat = "Deterministic extraction fallback; verify exact source wording before making strong claims."
        claim = sentence
        if title and (not claim.lower().startswith(title.lower()[:40])):
            claim = f"{title}: {claim}"
        findings.append({
            "finding_id": len(findings) + 1,
            "claim": _one_line(claim, 520),
            "evidence": _one_line(f"{title}{f' ({domain})' if domain else ''}: {sentence}", 700),
            "source_ids": [f"S{idx}"],
            "confidence": confidence,
            "evidence_strength": strength,
            "source_quality": _one_line(f"{_source_tier_label(tier)}; {src.get('type','web')}{f'; {domain}' if domain else ''}", 260),
            "caveat": caveat,
            "implication": "Use this as cited support in the report, with stronger conclusions reserved for corroborated primary or empirical evidence.",
        })
    return findings or [_weak_source_finding()]


def _audit_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_research_audit(audit) -> dict:
    if not isinstance(audit, dict) or not audit:
        return {}
    try:
        score = int(float(audit.get("coverage_score")))
    except Exception:
        return {}
    clean = dict(audit)
    clean["coverage_score"] = max(0, min(100, score))
    for key in ("strengths", "finding_issues", "weaknesses", "contradictions", "missing_evidence", "source_quality_notes"):
        clean[key] = _audit_list(clean.get(key))
    return clean


def _validate_report_citations(markdown: str, sources: list[dict]) -> dict:
    allowed = set()
    for i, src in enumerate(sources or [], start=1):
        try:
            idx = int(src.get("index") or src.get("source_index") or i)
        except Exception:
            idx = i
        if idx > 0:
            allowed.add(f"S{idx}")
    used = []
    for match in re.findall(r"\[S\s*(\d+)\]", markdown or "", flags=re.I):
        sid = f"S{int(match)}"
        if sid not in used:
            used.append(sid)
    invalid = [sid for sid in used if sid not in allowed]
    return {
        "valid": not invalid,
        "used": used,
        "invalid": invalid,
        "allowed_count": len(allowed),
    }


def _report_renderable_audit(markdown: str) -> dict:
    text = markdown or ""
    return {
        "has_mermaid": bool(re.search(r"```mermaid\s+[\s\S]+?```", text, re.I)),
        "has_chart": bool(re.search(r"```chart\s+[\s\S]+?```", text, re.I)),
        "has_pygraph": bool(re.search(r"```pygraph\s+[\s\S]+?```", text, re.I)),
        "has_display_equation": bool(re.search(r"\$\$[\s\S]+?\$\$", text)),
        "citation_refs": re.findall(r"\[S\s*\d+\]", text, flags=re.I),
    }


def _deterministic_research_audit(
    findings: list[dict], sources: list[dict], pages: list[dict],
    searches: int, budget: dict, reason: str = "Auditor returned invalid or empty JSON.",
) -> dict:
    def ratio(value: int, target: int) -> float:
        if not target:
            return 1.0 if value else 0.0
        return max(0.0, min(1.0, value / max(target, 1)))

    target_queries = int(budget.get("queries") or 0)
    target_sources = int(budget.get("target_sources") or 0)
    target_pages = int(budget.get("page_reads") or 0)
    source_count = len(sources or [])
    page_count = len(pages or [])
    finding_count = len(findings or [])
    cited_count = len([f for f in findings or [] if f.get("source_ids")])
    caveat_count = len([f for f in findings or [] if (f.get("caveat") or "").strip()])
    search_ratio = ratio(searches, target_queries)
    source_ratio = ratio(source_count, target_sources)
    page_ratio = ratio(page_count, target_pages)
    citation_ratio = cited_count / finding_count if finding_count else 0.0
    caveat_ratio = caveat_count / finding_count if finding_count else 0.0

    tiers = {
        "primary": len([s for s in sources or [] if s.get("tier") == 0]),
        "investigative": len([s for s in sources or [] if s.get("tier") == 1]),
        "general": len([s for s in sources or [] if s.get("tier") == 2]),
        "fact_checker": len([s for s in sources or [] if s.get("tier") == 3]),
    }
    if source_count:
        quality_ratio = (
            tiers["primary"] * 1.0 +
            tiers["investigative"] * 0.75 +
            tiers["fact_checker"] * 0.65 +
            tiers["general"] * 0.45
        ) / source_count
        credibility_values = [
            float(s.get("credibility_score", _score_source_credibility(s.get("url", ""), s.get("type", "web"), s.get("tier")).get("score", 50)))
            for s in sources or []
        ]
        credibility_ratio = (sum(credibility_values) / max(1, len(credibility_values))) / 100.0
        quality_ratio = quality_ratio * 0.65 + credibility_ratio * 0.35
    else:
        quality_ratio = 0.0

    score = round(100 * (
        search_ratio * 0.15 +
        source_ratio * 0.15 +
        page_ratio * 0.12 +
        citation_ratio * 0.25 +
        quality_ratio * 0.25 +
        caveat_ratio * 0.08
    ))
    if source_count == 0:
        score = min(score, 15)
    if finding_count and cited_count == 0:
        score = min(score, 25)
    if source_count and tiers["general"] == source_count:
        score = min(score, 62 if page_count and cited_count else 52)
    if source_count and source_ratio < 0.25:
        score = min(score, 55)
    score = max(0, min(100, score))

    strengths = []
    if cited_count:
        strengths.append(f"{cited_count}/{finding_count} findings include source IDs.")
    if page_count:
        strengths.append(f"{page_count} full-text page extracts were available for verification.")
    if tiers["primary"] or tiers["investigative"]:
        strengths.append(f"{tiers['primary'] + tiers['investigative']} primary/investigative sources were collected.")

    weaknesses = []
    if target_queries and search_ratio < 1:
        weaknesses.append(f"Search target was partially completed: {searches}/{target_queries} searches.")
    if target_sources and source_ratio < 1:
        weaknesses.append(f"Source target was partially completed: {source_count}/{target_sources} sources.")
    if target_pages and page_ratio < 1:
        weaknesses.append(f"Page-read target was partially completed: {page_count}/{target_pages} pages.")
    if finding_count and citation_ratio < 1:
        weaknesses.append(f"{finding_count - cited_count}/{finding_count} findings lack source IDs.")
    if source_count and tiers["general"] / source_count >= 0.75:
        weaknesses.append("Most collected sources are general/community-tier evidence.")
    if not caveat_count and finding_count:
        weaknesses.append("Findings have limited explicit caveats about uncertainty or missing evidence.")
    if reason:
        weaknesses.append(reason)

    missing_evidence = []
    if source_count and tiers["primary"] == 0:
        missing_evidence.append("No primary-source tier evidence was identifiable in the collected source set.")
    if target_pages and page_count < target_pages:
        missing_evidence.append("More full-text page reads would improve quote-level verification.")
    if not cited_count:
        missing_evidence.append("No source-backed findings were available for citation-level audit.")

    contradictions = []
    for finding in findings or []:
        caveat = finding.get("caveat") or ""
        if re.search(r"\b(contradict|conflict|disputed|uncertain|mixed)\b", caveat, re.I):
            contradictions.append(f"Finding #{finding.get('finding_id', '?')} caveat signals uncertainty: {caveat}")
    source_quality_notes = [
        f"Source tier mix: {tiers['primary']} primary, {tiers['investigative']} investigative/archival, {tiers['general']} general/community, {tiers['fact_checker']} fact-checker.",
        "Coverage score is evidence coverage: collection completion, citation coverage, page reads, source tier mix, and caveat coverage.",
    ]
    if source_count:
        avg_cred = round(sum(float(s.get("credibility_score", 50)) for s in sources or []) / source_count)
        source_quality_notes.append(f"Average credibility score: {avg_cred}/100. Low-score sources were retained for dispute/contradiction coverage, not treated as proof.")
    if source_count and tiers["general"] == source_count:
        source_quality_notes.append("All collected sources are general/community by tier, so deterministic coverage is capped despite citations.")

    return {
        "coverage_score": score,
        "audit_method": "deterministic_fallback",
        "strengths": strengths,
        "finding_issues": [],
        "weaknesses": weaknesses,
        "contradictions": contradictions,
        "missing_evidence": missing_evidence,
        "source_quality_notes": source_quality_notes,
    }


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((
            p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or p.path,
            urllib.parse.urlencode(query), "",
        ))
    except Exception:
        return url.strip()


def _clean_search_query_text(text: str) -> str:
    text = re.sub(r"https?://[^\s<>\]\[\"'`]+", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _clip_query_head(text: str, limit: int) -> str:
    text = _clean_search_query_text(text)
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped or text[:limit].strip()


def _clip_query_tail(text: str, limit: int) -> str:
    text = _clean_search_query_text(text)
    if len(text) <= limit:
        return text
    clipped = text[-limit:].lstrip()
    if " " in clipped:
        clipped = clipped.split(" ", 1)[1]
    return clipped or text[-limit:].strip()


def _compact_search_query(text: str, limit: int = 220) -> str:
    """Bound long search queries without collapsing distinct tail terms."""
    text = _clean_search_query_text(text)
    if len(text) <= limit:
        return text
    tail_limit = min(70, max(30, limit // 3))
    head_limit = max(80, limit - tail_limit - 1)
    return _clean_search_query_text(f"{_clip_query_head(text, head_limit)} {_clip_query_tail(text, tail_limit)}")[:limit]


def _compose_report_query(topic: str, suffix: str = "", limit: int = 220) -> str:
    topic = _clean_search_query_text(topic)
    suffix = _compact_search_query(suffix, 80)
    if not suffix:
        return _compact_search_query(topic, limit)
    base_limit = max(80, limit - len(suffix) - 1)
    return _compact_search_query(f"{_clip_query_head(topic, base_limit)} {suffix}", limit)


def _make_report_search_queries(
    query: str, focus: str, planned_queries: list, report_type: str, budget: dict,
) -> list[str]:
    """Build diverse report searches.

    Long user topics used to be truncated before suffixes were appended, which
    made all template/common variants dedupe to one query. Compose variants by
    reserving room for the differentiating suffix.
    """
    qset: list[str] = []
    seen: set[str] = set()

    def add_query(q: str) -> None:
        q = _compact_search_query(q)
        key = re.sub(r"\W+", " ", q.lower()).strip()
        if q and key and key not in seen:
            seen.add(key)
            qset.append(q)

    search_topic = _clean_search_query_text(query) or str(query or "")
    search_focus = _clean_search_query_text(focus)
    add_query(search_topic)
    if search_focus:
        add_query(_compose_report_query(search_topic, search_focus))
    for q in planned_queries or []:
        add_query(str(q))

    common_queries = [
        "official documentation", "primary source", "implementation details", "architecture",
        "benchmarks data", "failure modes", "limitations criticism", "best practices",
        "production deployment", "security risk", "maintenance cost", "case study",
        "community discussion", "recent 2026", "alternatives comparison", "migration guide",
        "decision criteria", "developer experience", "testing strategy", "rollback plan",
        "technical debt", "adoption trends", "performance tradeoffs", "source code organization",
    ]
    template_queries = {
        "academic": ["research paper", "literature review", "systematic review", "methodology limitations", "empirical study", "survey paper", "replication", "open dataset"],
        "decision": ["pros cons risks", "cost benefit", "alternatives comparison", "implementation risk", "decision matrix", "migration cost", "opportunity cost", "governance", "rollback strategy"],
        "market": ["market size competitors", "industry analysis", "pricing business model", "customer adoption", "funding", "analyst report", "customer reviews", "growth trend", "competitive landscape"],
        "technical": ["architecture", "implementation details", "benchmarks", "failure modes best practices", "dependency graph", "source code architecture", "build tooling", "runtime performance", "debugging", "production examples"],
        "timeline": ["timeline chronology", "documents evidence", "key actors", "controversy investigation", "original source", "archive", "public record", "interview", "event sequence"],
        "digest": ["best sources", "overview", "primary source", "expert analysis", "official docs", "high signal references", "source comparison", "must read", "FAQ"],
        "analyst": ["expert analysis", "latest developments", "data statistics", "criticism risks", "strategic implications", "operational constraints", "adoption", "roadmap", "lessons learned"],
    }.get(report_type, [])

    for suffix in template_queries:
        add_query(_compose_report_query(search_topic, suffix))
    for suffix in common_queries:
        if len(qset) >= int(budget.get("queries") or 0):
            break
        add_query(_compose_report_query(search_topic, suffix))
    return qset[:min(len(qset), int(budget.get("queries") or len(qset)))]


def _normalize_followup_queries(raw_queries, existing_queries: set[str], topic: str, remaining: int) -> list[str]:
    """Clean model-proposed adaptive queries without exceeding budget."""
    if remaining <= 0:
        return []
    out: list[str] = []
    seen = {re.sub(r"\W+", " ", q.lower()).strip() for q in existing_queries or set()}
    topic_tokens = _evidence_tokens(topic)
    for raw in raw_queries or []:
        q = _compact_search_query(str(raw or ""))
        if not q or "http://" in q.lower() or "https://" in q.lower():
            continue
        key = re.sub(r"\W+", " ", q.lower()).strip()
        if not key or key in seen:
            continue
        q_tokens = _evidence_tokens(q)
        # Keep genuine contradiction/primary-source expansions even if they
        # introduce new entities; otherwise require at least one topic anchor.
        if topic_tokens and not (q_tokens & topic_tokens) and not re.search(r"\b(contradiction|criticism|primary source|official|data|benchmark|audit|timeline)\b", q, re.I):
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= remaining:
            break
    return out


def _normalize_report_sources(
    input_sources: list[dict], direct_sources: list[dict],
    all_results: list[dict], budget: dict,
) -> list[dict]:
    seen_urls = {s.get("url") for s in direct_sources if s.get("url")}
    sources: list[dict] = []
    target_web_sources = max(0, int(budget.get("target_sources") or 0) - len(input_sources) - len(direct_sources))

    def result_priority(item):
        url = _normalize_url(item.get("url", ""))
        tier = _source_tier(url)
        cred = _score_source_credibility(url, item.get("type", "web"), tier).get("score", 50)
        raw_score = item.get("score", 0) or 0
        try:
            raw_score = float(raw_score)
        except Exception:
            raw_score = 0.0
        return (tier, -cred, -raw_score, -len(item.get("content", "") or ""))

    for item in sorted(all_results or [], key=result_priority):
        if len(sources) >= target_web_sources:
            break
        url = _normalize_url(item.get("url", ""))
        if not url or url in seen_urls:
            continue
        if not _url_safe_for_direct_fetch(url, resolve_dns=False):
            continue
        seen_urls.add(url)
        src = {
            "index": len(input_sources) + len(direct_sources) + len(sources) + 1,
            "title": item.get("title", "") or url,
            "url": url,
            "snippet": item.get("content", "")[:320],
            "thumbnail": item.get("thumbnail", ""),
            "type": item.get("type", "web"),
            "tier": _source_tier(url),
            "query": item.get("query", ""),
        }
        sources.append(_apply_source_quality(src))

    out = []
    for src in list(input_sources or []) + list(direct_sources or []) + sources:
        out.append(_apply_source_quality(dict(src)))
    for i, src in enumerate(out, start=1):
        src["index"] = i
    return out


def _normalize_adaptive_research_state(obj, max_learnings: int = 10) -> dict:
    if not isinstance(obj, dict):
        return {"learnings": [], "follow_up_queries": []}
    learnings = obj.get("learnings") or obj.get("findings") or []
    followups = obj.get("follow_up_queries") or obj.get("followups") or obj.get("queries") or []
    if isinstance(learnings, str):
        learnings = [learnings]
    if isinstance(followups, str):
        followups = [followups]
    return {
        "learnings": [_one_line(str(x), 420) for x in learnings[:max_learnings] if str(x or "").strip()],
        "follow_up_queries": [str(x).strip() for x in followups if str(x or "").strip()],
        "gaps": [_one_line(str(x), 260) for x in (obj.get("gaps") or [])[:8] if str(x or "").strip()] if isinstance(obj.get("gaps") or [], list) else [],
    }


async def _extract_adaptive_learnings(
    http, ollama_url: str, default_model: str, model: str,
    query: str, focus: str, report_type: str, round_no: int,
    evidence_summary: str, remaining_queries: int,
) -> dict:
    if remaining_queries <= 0:
        return {"learnings": [], "follow_up_queries": [], "gaps": []}
    prompt = f"""You are the adaptive middle stage of a deep-research pipeline.

Topic: {query}
Focus: {focus or "none"}
Report type: {report_type}
Adaptive round: {round_no}
Remaining search-query budget: {remaining_queries}

Evidence gathered so far:
{evidence_summary[:24000]}

Return strict JSON:
{{
  "learnings": ["compact source-backed learning, not a final conclusion"],
  "gaps": ["important missing evidence or unresolved contradiction"],
  "follow_up_queries": ["specific next search query"]
}}

Rules:
- Follow-up queries must be specific, searchable, and anchored to the topic.
- Prefer primary sources, official docs/data, benchmarks, contradictions, and missing viewpoints.
- Do not repeat a query already implied by the evidence summary.
- Return at most {remaining_queries} follow_up_queries."""
    obj = await _ask_ollama_json(
        http, ollama_url, prompt, model=model, default_model=default_model,
        max_tokens=1800, fallback={}, expected_type=dict,
    )
    return _normalize_adaptive_research_state(obj)


# High-volume streaming events that are never read back after the run —
# the full text lands in report_markdown, so persisting each token chunk
# only burns a DB write cycle per 240 chars and bloats report GET payloads.
_EPHEMERAL_REPORT_EVENTS = {"research_token"}


async def _emit_report_event(events, report_id: str, event_type: str, data: dict):
    """Emit a live research event and persist it on the report row."""
    import database as db

    event = {"type": event_type, "data": data or {}, "timestamp": time.time()}
    if event_type not in _EPHEMERAL_REPORT_EVENTS:
        try:
            await db.append_research_event(report_id, event)
        except Exception as e:
            print(f"[RESEARCH REPORT] Event persist failed: {e}")
    try:
        await events.emit(report_id, event_type, data or {})
    except Exception as e:
        print(f"[RESEARCH REPORT] Event emit failed: {e}")


async def _ask_report_streamed(
    http, ollama_url: str, events, report_id: str, prompt: str,
    model: str = None, default_model: str = "qwen3.5:27b",
    max_tokens: int = 6144,
) -> str:
    """Stream final report tokens to the dedicated research workspace."""
    import cancel_registry

    _num_ctx = _research_num_ctx()
    model_id = model or default_model
    accumulated = ""
    emitted_len = 0
    token_buf = ""

    async def emit_safe(final: bool = False) -> None:
        nonlocal emitted_len, token_buf
        safe_text = _streamable_report_text(accumulated, final=final)
        if len(safe_text) < emitted_len:
            emitted_len = 0
            token_buf = ""
        delta = safe_text[emitted_len:]
        if not delta:
            if final and token_buf:
                await _emit_report_event(events, report_id, "research_token", {"content": token_buf})
                token_buf = ""
            return
        emitted_len = len(safe_text)
        token_buf += delta
        if final or len(token_buf) >= 240:
            await _emit_report_event(events, report_id, "research_token", {"content": token_buf})
            token_buf = ""

    try:
        if is_cloud_model(model_id):
            async for ev in stream_provider_chat(
                http,
                model_id,
                [{"role": "user", "content": prompt}],
                options={"temperature": 0.25, "num_predict": max_tokens},
            ):
                if cancel_registry.is_cancelled(report_id):
                    raise cancel_registry.RunCancelled(report_id)
                if ev.get("type") == "token" and ev.get("content"):
                    accumulated += ev.get("content", "")
                    await emit_safe()
            await emit_safe(final=True)
            return _clean_research_model_text(accumulated)

        async with http.stream("POST", f"{ollama_url}/api/generate", json={
            "model": model_id,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "options": {"temperature": 0.25, "num_predict": max_tokens, "num_ctx": _num_ctx},
        }, timeout=420) as stream:
            async for line in stream.aiter_lines():
                if cancel_registry.is_cancelled(report_id):
                    raise cancel_registry.RunCancelled(report_id)
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                piece = chunk.get("response", "") or ""
                if piece:
                    accumulated += piece
                    await emit_safe()
                if chunk.get("done"):
                    break
        await emit_safe(final=True)
        return _clean_research_model_text(accumulated)
    except cancel_registry.RunCancelled:
        raise
    except Exception as e:
        raise RuntimeError(f"Report synthesis failed: {e}") from e


async def run_research_report(
    http, ollama_url: str, default_model: str, events, report_id: str,
    query: str, depth: int = 3, focus: str = "", report_type: str = "analyst",
    model: str = "", planner_model: str = "", auditor_model: str = "",
    kb_ids: list | None = None, inputs: list | None = None,
) -> dict:
    """Run the first-class Deep Research report pipeline.

    This is intentionally additive. `run_deep_research` below keeps the existing
    tool contract used by chat agents and Daedalus.
    """
    import database as db
    import rag
    import cancel_registry

    t_start = time.time()
    depth = max(1, min(5, int(depth or 3)))
    budget = _research_depth_budget(depth)
    report_type = report_type if report_type in REPORT_TEMPLATE_MAP else "analyst"
    template = REPORT_TEMPLATE_MAP[report_type]
    run_model = model or default_model
    plan_model = planner_model or run_model
    audit_model = auditor_model or run_model
    current_date = datetime.utcnow().date().isoformat()
    kb_ids = kb_ids or []
    inputs = inputs or []
    searxng_url = config.SEARXNG_URL
    cancel_registry.register(report_id)

    async def check_cancel():
        if cancel_registry.is_cancelled(report_id):
            raise cancel_registry.RunCancelled(report_id)

    async def phase(key: str, label: str, detail: str = "", pct: int | None = None):
        await check_cancel()
        await db.update_research_report(report_id, status="running", unless_status="cancelled")
        await _emit_report_event(events, report_id, "research_phase", {
            "phase": key, "label": label, "detail": detail, "pct": pct,
        })

    try:
        # A cancel can land between report creation and this runner starting
        # (registry signal hits before register()); honor the DB row so the
        # run doesn't resurrect a cancelled report back to running/complete.
        existing = await db.get_research_report(report_id)
        if existing and existing.get("status") == "cancelled":
            await _emit_report_event(events, report_id, "research_error", {
                "status": "cancelled", "error": "Cancelled by user",
            })
            return {"id": report_id, "status": "cancelled"}

        await db.update_research_report(report_id, status="running", error="", unless_status="cancelled")
        await _emit_report_event(events, report_id, "research_started", {
            "query": query, "report_type": report_type, "depth": depth,
        })

        # Phase 1: planning
        await phase("planning", "Planning research strategy", "Building queries, criteria, and outline", 5)
        plan_prompt = f"""You are the planning stage for an advanced research system.

Topic: {query}
Focus: {focus or "none"}
Report type: {template["label"]}
Expected sections: {", ".join(template["sections"])}
Current date: {current_date}
Depth: {depth}/5
Collection budget: up to {budget["queries"]} search queries, {budget["target_sources"]} sources, and {budget["page_reads"]} full-page reads.

Return strict JSON with:
{{
  "title": "short report title",
  "research_questions": ["..."],
  "search_queries": ["..."],
  "inclusion_criteria": ["..."],
  "outline": [{{"heading":"...", "goal":"..."}}],
  "known_risks": ["..."]
}}

Prefer precise search queries, primary sources, recent sources when freshness matters, diverse viewpoints, and enough query diversity to use the collection budget.
Use the current date above as authoritative; do not infer "current real-world knowledge" from model training cutoffs."""
        plan_text = await cancel_registry.await_cancellable(
            _ask_ollama(http, ollama_url, plan_prompt, model=plan_model, default_model=default_model, max_tokens=1800),
            report_id,
        )
        plan = _safe_json_obj(plan_text, {})
        if not isinstance(plan, dict):
            plan = {}
        fallback_title = query[:80].strip() or "Research Report"
        title = _one_line(plan.get("title") or fallback_title, 96)
        outline = plan.get("outline") if isinstance(plan.get("outline"), list) else [
            {"heading": s, "goal": f"Cover {s.lower()} for {template['label']}."}
            for s in template["sections"]
        ]
        await db.update_research_report(report_id, title=title, outline={
            "template": report_type,
            "sections": outline,
            "research_questions": plan.get("research_questions", []),
            "inclusion_criteria": plan.get("inclusion_criteria", []),
            "known_risks": plan.get("known_risks", []),
        })

        # Phase 2: gather context from uploaded inputs and KBs.
        await phase("context", "Loading user context", "Reading uploaded notes and knowledge bases", 12)
        input_context_parts = []
        input_sources = []
        for item in inputs[:12]:
            name = _one_line(item.get("name") or item.get("filename") or "Uploaded input", 120)
            content = (item.get("content") or item.get("text") or "").strip()
            if not content:
                continue
            content = content[:16000]
            input_sources.append({
                "index": 0, "title": f"Uploaded: {name}", "url": "",
                "snippet": _one_line(content, 240), "type": item.get("type", "file"),
                "tier": 0, "tier_label": _source_tier_label(0), "metadata": {"name": name},
            })
            input_context_parts.append(f"### Uploaded input: {name}\n{content}")
        kb_context = ""
        if kb_ids and query:
            try:
                chunks = await rag.query(kb_ids, query, top_k=8)
                if chunks:
                    kb_context = rag.format_context(chunks, max_chars=8000)
                    input_context_parts.append(f"### Knowledge base context\n{kb_context}")
            except Exception as e:
                await _emit_report_event(events, report_id, "research_audit", {
                    "level": "warning", "message": f"KB context unavailable: {e}",
                })
        user_context = "\n\n".join(input_context_parts)[:32000]
        seed_urls = _extract_seed_urls(query, focus, user_context)
        direct_sources = []
        direct_pages = []
        safe_seeds = []
        for url in seed_urls:
            if not _url_safe_for_direct_fetch(url):
                await _emit_report_event(events, report_id, "research_audit", {
                    "level": "warning", "message": f"Skipped unsafe direct URL: {url}",
                })
                continue
            safe_seeds.append(url)

        async def _fetch_seed(url: str) -> tuple[str, dict | None]:
            if _github_repo_from_url(url):
                page = await _fetch_github_repo_snapshot(http, url)
                if page:
                    return "github_repo", page
            return "direct_url", await _fetch_page(http, url)

        await check_cancel()
        seed_results = await asyncio.gather(
            *[_fetch_seed(u) for u in safe_seeds], return_exceptions=True,
        ) if safe_seeds else []
        for url, seed_result in zip(safe_seeds, seed_results):
            await check_cancel()
            if isinstance(seed_result, BaseException):
                seed_result = ("direct_url", None)
            source_type, page = seed_result
            if not page or not page.get("content"):
                await _emit_report_event(events, report_id, "research_audit", {
                    "level": "warning", "message": f"Direct URL could not be read: {url}",
                })
                continue
            direct_url = _normalize_url(page.get("url") or url)
            tier = 0 if source_type == "github_repo" else _source_tier(direct_url)
            src = {
                "index": len(input_sources) + len(direct_sources) + 1,
                "title": page.get("title") or direct_url,
                "url": direct_url,
                "snippet": _one_line(page.get("content", ""), 320),
                "type": source_type,
                "tier": tier,
                "tier_label": _source_tier_label(tier),
                "query": "user-provided URL",
                "metadata": {"seed_url": url, "files": page.get("files", []), "default_branch": page.get("default_branch")},
            }
            src = _apply_source_quality(src)
            direct_sources.append(src)
            direct_pages.append({
                "url": direct_url,
                "title": src["title"],
                "content": page.get("content", ""),
                "source_index": src["index"],
                "source_type": source_type,
            })
            await _emit_report_event(events, report_id, "research_source_found", src)
            await _emit_report_event(events, report_id, "research_source_read", {
                "url": direct_url, "title": src["title"], "source_index": src["index"],
                "chars": len(page.get("content", "")), "direct": True,
            })

        # Phase 3: search.
        await phase("search", "Searching the web", "Initial source discovery and query expansion", 20)
        planned_queries = plan.get("search_queries", [])
        if not isinstance(planned_queries, list):
            planned_queries = []
        full_qset = _make_report_search_queries(query, focus, planned_queries, report_type, budget)
        initial_query_count = max(3, int(max(1, budget["queries"]) * (0.58 if depth >= 4 else 0.68)))
        qset = full_qset[:min(len(full_qset), initial_query_count)]

        all_results = []
        searched = set()
        pages = list(direct_pages)
        fetched_urls = {_normalize_url(p.get("url", "")) for p in pages if p.get("url")}
        emitted_source_urls = {s.get("url") for s in direct_sources if s.get("url")}
        adaptive_learnings: list[str] = []
        adaptive_gaps: list[str] = []
        adaptive_followups: list[str] = []
        # Shared Google-scrape budget across the whole report run.
        google_fallback_state = {"remaining": 8}

        async def run_search_queries(queries: list[str], pct_base: int, pct_span: int, detail_prefix: str):
            if not queries:
                return

            def claim(q):
                if q in searched:
                    return False
                searched.add(q)
                return True

            async def one(q):
                time_range = "year" if re.search(r"\b(latest|recent|current|today|202[5-9]|news)\b", q, re.I) else None
                items = await _search_searxng(http, searxng_url, q, count=budget["results_per_query"], time_range=time_range, fallback_state=google_fallback_state)
                for item in items:
                    item["query"] = q
                return items

            async def progress(done):
                await _emit_report_event(events, report_id, "research_phase", {
                    "phase": "search", "label": "Searching the web",
                    "detail": f"{detail_prefix}: {min(done, len(queries))}/{len(queries)} queries complete",
                    "pct": pct_base + int(pct_span * done / max(len(queries), 1)),
                })

            await _batched_searches(queries, one, delay=_SEARCH_BATCH_DELAY_DEEP,
                                    before_batch=check_cancel, prepare=claim,
                                    on_batch_done=progress, sink=all_results)

        def remap_page_source_indices(current_sources: list[dict]) -> None:
            by_url = {_normalize_url(s.get("url", "")): s for s in current_sources if s.get("url")}
            for page in pages:
                src = by_url.get(_normalize_url(page.get("url", "")))
                if src:
                    page["source_index"] = src.get("index", 0)

        async def emit_new_sources(current_sources: list[dict]) -> None:
            for src in current_sources:
                url = src.get("url") or f"local:{src.get('title','')}"
                if url in emitted_source_urls:
                    continue
                emitted_source_urls.add(url)
                await _emit_report_event(events, report_id, "research_source_found", src)

        async def fetch_ranked_pages(current_sources: list[dict], max_new_pages: int) -> None:
            if max_new_pages <= 0:
                return
            await phase("reading", "Reading high-value sources", "Fetching full text for ranked pages", 42)
            web_results = [r for r in all_results if r.get("url")]
            top_urls = [u for u in _rank_urls(web_results, fetched_urls) if _normalize_url(u) not in fetched_urls][:max_new_pages]
            for i in range(0, len(top_urls), 4):
                await check_cancel()
                batch = top_urls[i:i + 4]
                fetched = await asyncio.gather(*[_fetch_page(http, u) for u in batch], return_exceptions=True)
                for u, page in zip(batch, fetched):
                    # Record the requested URL even on failure so adaptive
                    # rounds never re-rank/re-fetch it (redirects used to leave
                    # the original URL eligible forever).
                    req_norm = _normalize_url(u)
                    fetched_urls.add(req_norm)
                    if isinstance(page, dict) and page.get("content"):
                        norm = _normalize_url(page.get("url", ""))
                        fetched_urls.add(norm)
                        src_meta = next((s for s in current_sources if s.get("url") in (norm, req_norm)), {})
                        page["source_index"] = src_meta.get("index", 0)
                        pages.append(page)
                        await _emit_report_event(events, report_id, "research_source_read", {
                            "url": page.get("url", ""), "title": src_meta.get("title", ""),
                            "source_index": page.get("source_index"),
                            "chars": len(page.get("content", "")),
                        })

        def current_metrics(current_sources: list[dict], extra: dict | None = None) -> dict:
            m = {
                "searches": len(searched), "results": len(all_results),
                "source_count": len(current_sources), "pages_read": len(pages), "elapsed": time.time() - t_start,
                "target_queries": budget["queries"], "target_sources": budget["target_sources"],
                "target_pages": budget["page_reads"], "depth": depth,
                "adaptive_learnings": len(adaptive_learnings),
                "adaptive_followup_queries": len(adaptive_followups),
            }
            if extra:
                m.update(extra)
            return m

        await run_search_queries(qset, 20, 18, "initial")
        sources = _normalize_report_sources(input_sources, direct_sources, all_results, budget)
        remap_page_source_indices(sources)
        await emit_new_sources(sources)
        await db.update_research_report(report_id, sources=sources, metrics=current_metrics(sources))
        await db.replace_research_sources(report_id, sources)

        if not sources and not user_context:
            raise RuntimeError("No usable sources found. Check SearXNG or provide files/KB context.")

        # Phase 4: read pages, then adaptively search gaps.
        await fetch_ranked_pages(sources, max(0, budget["page_reads"] - len(pages)))
        await db.update_research_report(report_id, metrics=current_metrics(sources))

        max_adaptive_rounds = max(0, min(4, depth - 1))
        for round_no in range(1, max_adaptive_rounds + 1):
            await check_cancel()
            remaining_queries = max(0, budget["queries"] - len(searched))
            remaining_pages = max(0, budget["page_reads"] - len(pages))
            if remaining_queries <= 0 and remaining_pages <= 0:
                break
            await phase("followup", "Adaptive follow-up", f"Round {round_no}: extracting learnings and gaps", 50 + min(10, round_no * 3))
            interim_source_lines = [
                f"[S{s.get('index')}] {s.get('title','')} | credibility {s.get('credibility_score', 0)}/100 | {s.get('snippet','')}"
                for s in sources[:min(len(sources), budget["source_briefs"])]
            ]
            interim_page_lines = [
                f"[S{p.get('source_index') or '?'}] {p.get('url','')}\n{p.get('content','')[:1400]}"
                for p in pages[-min(len(pages), 8):]
            ]
            evidence_summary = (
                ("Existing learnings:\n" + "\n".join(f"- {x}" for x in adaptive_learnings[-10:]) + "\n\n" if adaptive_learnings else "") +
                "Source briefs:\n" + "\n".join(interim_source_lines) +
                "\n\nRecent full-text evidence:\n" + "\n\n".join(interim_page_lines)
            )
            adaptive_state = await cancel_registry.await_cancellable(
                _extract_adaptive_learnings(
                    http, ollama_url, default_model, run_model,
                    query, focus, template["label"], round_no,
                    evidence_summary, remaining_queries,
                ),
                report_id,
            )
            new_learnings = adaptive_state.get("learnings", [])
            new_gaps = adaptive_state.get("gaps", [])
            adaptive_learnings.extend([x for x in new_learnings if x not in adaptive_learnings])
            adaptive_gaps.extend([x for x in new_gaps if x not in adaptive_gaps])
            followups = _normalize_followup_queries(
                adaptive_state.get("follow_up_queries", []),
                searched, query, remaining_queries,
            )
            if not followups and remaining_queries > 0 and round_no == 1:
                followups = _normalize_followup_queries(
                    [
                        _compose_report_query(query, "primary source data"),
                        _compose_report_query(query, "contradictions criticism limitations"),
                        _compose_report_query(query, "official documentation benchmarks"),
                    ],
                    searched, query, min(remaining_queries, 3),
                )
            if not followups and not new_learnings:
                break
            if followups:
                adaptive_followups.extend(followups)
                await _emit_report_event(events, report_id, "research_phase", {
                    "phase": "followup",
                    "label": "Adaptive follow-up",
                    "detail": f"Round {round_no}: {len(followups)} follow-up queries",
                    "pct": 56 + min(12, round_no * 3),
                })
                await run_search_queries(followups, 56 + min(12, round_no * 3), 4, f"adaptive round {round_no}")
                sources = _normalize_report_sources(input_sources, direct_sources, all_results, budget)
                remap_page_source_indices(sources)
                await emit_new_sources(sources)
                await db.replace_research_sources(report_id, sources)
            if remaining_pages > 0:
                await fetch_ranked_pages(sources, remaining_pages)
            await db.update_research_report(report_id, sources=sources, metrics=current_metrics(sources))

        sources = _normalize_report_sources(input_sources, direct_sources, all_results, budget)
        remap_page_source_indices(sources)
        await db.replace_research_sources(report_id, sources)

        # Build bounded evidence context.
        source_briefs = []
        for src in sources[:budget["source_briefs"]]:
            sid = f"S{src['index']}"
            tier = src.get("tier", 2)
            cred = src.get("credibility_score", 0)
            factors = ", ".join(src.get("credibility_factors") or [])
            metadata = src.get("metadata") if isinstance(src.get("metadata"), dict) else {}
            files = metadata.get("files") or []
            file_line = f"Repository files sampled: {', '.join(files[:12])}\n" if files else ""
            source_briefs.append(
                f"Source ID: [{sid}]\n"
                f"Title: {src.get('title','')}\n"
                f"URL: {src.get('url','uploaded/local')}\n"
                f"Source type: {src.get('type','web')}\n"
                f"Source tier: {_source_tier_label(tier)} (T{tier})\n"
                f"Credibility: {cred}/100 ({factors or 'standard web source'})\n"
                f"{file_line}"
                f"Snippet: {src.get('snippet','')}"
            )
        page_context = []
        for p in pages[:budget["page_extracts"]]:
            sid = f"S{p.get('source_index')}" if p.get("source_index") else "S?"
            page_context.append(f"--- {sid} {p.get('url','')} ---\n{p.get('content','')[:3000]}")
        evidence_context = (
            ("USER/KB CONTEXT\n" + user_context + "\n\n" if user_context else "") +
            "SOURCE BRIEFS\n" + "\n\n".join(source_briefs) +
            "\n\nFULL TEXT EXTRACTS\n" + "\n\n".join(page_context)
        )[:_effective_context_chars(budget)]

        evidence_records = _build_report_evidence_records(report_id, query, user_context, sources, pages)
        evidence_index = await cancel_registry.await_cancellable(
            _index_report_evidence(report_id, evidence_records),
            report_id,
        )
        section_queries = [query, focus] + [
            str(q) for q in (plan.get("research_questions") or [])[:10]
        ] + [
            f"{o.get('heading','')} {o.get('goal','')}" for o in outline[:10] if isinstance(o, dict)
        ] + adaptive_followups[:8]
        targeted_evidence = await cancel_registry.await_cancellable(
            _retrieve_report_evidence(report_id, section_queries, top_k=min(18, max(8, budget["findings"]))),
            report_id,
        )
        targeted_evidence_context = _format_targeted_evidence(targeted_evidence, max_chars=16000)
        await _emit_report_event(events, report_id, "research_phase", {
            "phase": "evidence_index",
            "label": "Indexing evidence",
            "detail": f"{evidence_index.get('embedded', 0)}/{evidence_index.get('chunks', 0)} chunks embedded",
            "pct": 60,
        })

        # Phase 5: extract findings.
        await phase("extracting", "Extracting evidence", "Converting sources into claim-level findings", 62)
        allowed_source_ids = ", ".join(f"S{s.get('index')}" for s in sources[:budget["source_briefs"]])
        findings_prompt = f"""Extract the strongest findings for this report as strict JSON.

Topic: {query}
Focus: {focus or "none"}
Report type: {template["label"]}
Current date: {current_date}
Allowed source IDs: {allowed_source_ids}

Evidence:
{evidence_context}

Targeted evidence retrieved from the per-report evidence index:
{targeted_evidence_context or "[No targeted snippets retrieved; use the evidence above.]"}

Adaptive learnings and gaps:
{json.dumps({"learnings": adaptive_learnings[:20], "gaps": adaptive_gaps[:12], "follow_up_queries": adaptive_followups[:20]}, indent=2)}

Return a JSON array of 8-{budget["findings"]} objects:
[
  {{
    "finding_id": 1,
    "claim": "specific finding",
    "evidence": "short evidence summary",
    "source_ids": ["S1"],
    "confidence": "high|medium|low",
    "evidence_strength": "strong|moderate|thin|anecdotal",
    "source_quality": "primary/benchmark/official/community/blog/etc.",
    "caveat": "main limitation or empty string",
    "implication": "why it matters"
  }}
]

Rules:
- Use source IDs only as S1, S2, etc. Do not invent source IDs.
- Use "finding_id" only for findings. Do not call sources "claims".
- Treat sources with Source type "github_repo" as direct repository evidence for repo-specific architecture, dependency, and file-organization claims.
- Use the current date above as authoritative. Dates on or before that date are not future-dated.
- Prefer per-report evidence-index snippets for exact source linkage when they answer the research question.
- Consider credibility scores and factors when assigning evidence_strength; low-score sources can support dispute coverage but should not carry strong claims alone.
- Do not write "studies show" unless the cited source_ids contain peer-reviewed papers, official benchmarks, or primary empirical studies.
- Mark community discussions, blogs, GitHub issues, and vendor docs as moderate, thin, or anecdotal unless they contain direct data."""
        findings_obj = await cancel_registry.await_cancellable(
            _ask_ollama_json(
                http, ollama_url, findings_prompt, model=run_model,
                default_model=default_model,
                max_tokens=min(5200, 1900 + budget["findings"] * 150),
                fallback=[], expected_type=(list, dict),
            ),
            report_id,
        )
        if isinstance(findings_obj, dict):
            findings_obj = findings_obj.get("findings") or findings_obj.get("items") or findings_obj.get("results") or []
        findings = _normalize_research_findings(findings_obj, len(sources), budget["findings"])
        if not findings:
            findings = _build_source_backed_findings(query, focus, sources, pages, budget["findings"])
        for finding in findings[:budget["findings"]]:
            await _emit_report_event(events, report_id, "research_finding", finding)
        await db.update_research_report(report_id, findings=findings)

        # Phase 6: audit.
        await phase("audit", "Auditing evidence quality", "Checking citation coverage and uncertainty", 74)
        audit_prompt = f"""You are a citation auditor and skeptical reviewer.

Topic: {query}
Report type: {template["label"]}
Current date: {current_date}
Findings JSON:
{json.dumps(findings[:budget["findings"]], indent=2)}

Sources:
{chr(10).join(source_briefs[:budget["source_briefs"]])}

Targeted evidence retrieved from the per-report evidence index:
{targeted_evidence_context or "[No targeted snippets retrieved.]"}

Audit rules:
- Refer to extracted findings as "Finding #N".
- Refer to sources only as "[S#]"; never write "claim 22" or use a bare number for a source.
- Flag any finding that uses strong causal, benchmark, quality, latency, cost, or hallucination-reduction language without primary empirical data.
- Use credibility scores and factors to calibrate source_quality_notes and coverage language.
- Treat community discussions, blogs, GitHub repositories, and vendor docs as practical signals, not peer-reviewed evidence.
- Use the current date above as authoritative. Do not call sources future-dated when their dates are on or before the current date.
- Never mention a model training cutoff or phrases like "current real-world knowledge early 2024"; audit only against the source set and current date shown here.
- Contradictions should name the affected Finding #N and source IDs where possible.

Return strict JSON:
{{
  "coverage_score": 0-100,
  "strengths": ["..."],
  "finding_issues": [{{"finding_id": 1, "issue": "...", "source_ids": ["S1"]}}],
  "weaknesses": ["..."],
  "contradictions": ["..."],
  "missing_evidence": ["..."],
  "source_quality_notes": ["..."]
}}"""
        audit_obj = await cancel_registry.await_cancellable(
            _ask_ollama_json(
                http, ollama_url, audit_prompt, model=audit_model,
                default_model=default_model,
                max_tokens=min(4200, 2000 + budget["findings"] * 100),
                fallback=None, expected_type=dict,
            ),
            report_id,
        )
        audit = _normalize_research_audit(audit_obj)
        if not audit:
            audit = _deterministic_research_audit(
                findings, sources, pages, len(searched), budget,
                reason="Audit generated from deterministic fallback because the LLM auditor returned invalid or empty JSON.",
            )
        await _emit_report_event(events, report_id, "research_audit", audit)

        metrics = {
            "searches": len(searched),
            "results": len(all_results),
            "source_count": len(sources),
            "pages_read": len(pages),
            "elapsed": time.time() - t_start,
            "coverage_score": audit.get("coverage_score", 0),
            "depth": depth,
            "context_chars_used": len(evidence_context),
            "research_num_ctx": _research_num_ctx(),
            "target_queries": budget["queries"],
            "target_sources": budget["target_sources"],
            "target_pages": budget["page_reads"],
            "adaptive_rounds": max_adaptive_rounds,
            "adaptive_learnings": adaptive_learnings[:30],
            "adaptive_gaps": adaptive_gaps[:20],
            "adaptive_followup_queries": adaptive_followups[:30],
            "evidence_index": evidence_index,
            "targeted_evidence_count": len(targeted_evidence),
            "tiers": {
                "primary": len([s for s in sources if s.get("tier") == 0]),
                "investigative": len([s for s in sources if s.get("tier") == 1]),
                "general": len([s for s in sources if s.get("tier") == 2]),
                "fact_checker": len([s for s in sources if s.get("tier") == 3]),
            },
            "credibility": {
                "average": round(sum(float(s.get("credibility_score", 50)) for s in sources) / max(1, len(sources))),
                "low_count": len([s for s in sources if float(s.get("credibility_score", 50)) < 45]),
                "high_count": len([s for s in sources if float(s.get("credibility_score", 50)) >= 75]),
            },
        }

        # Phase 7: synthesize final report.
        await phase("synthesis", "Writing final report", "Streaming the report into the viewer", 86)
        visual_guidance = "\n".join(f"- {line}" for line in _VISUAL_REPORT_GUIDANCE.strip().splitlines())
        final_prompt = f"""Write a polished, advanced research report.

Topic: {query}
Focus: {focus or "none"}
Report type: {template["label"]}
Required sections: {", ".join(template["sections"])}
Title: {title}
Current date: {current_date}

Research plan:
{json.dumps({"questions": plan.get("research_questions", []), "criteria": plan.get("inclusion_criteria", [])}, indent=2)}

Findings:
{json.dumps(findings[:budget["findings"]], indent=2)}

Audit:
{json.dumps(audit, indent=2)}

Adaptive learnings and follow-up searches:
{json.dumps({"learnings": adaptive_learnings[:24], "gaps": adaptive_gaps[:16], "follow_up_queries": adaptive_followups[:24]}, indent=2)}

Targeted evidence retrieved from the per-report evidence index:
{targeted_evidence_context or "[No targeted snippets retrieved; use findings/source briefs only.]"}

Source credibility summary:
{json.dumps(metrics.get("credibility", {}), indent=2)}

Write in Markdown. Requirements:
- Start with "# {title}".
- Include a compact "Method" section explaining web/files/KB coverage.
- Use inline citations like [S1], [S2] after claims.
- Do not cite a source id unless it appears in the evidence.
- Use the current date above as authoritative. Do not describe source dates on or before that date as future-dated, and do not mention training cutoffs.
- Include uncertainty, contradictions, and missing-evidence caveats.
- Use "Finding #N" only when referring to extracted findings; use "[S#]" only when citing sources.
- Do not use "studies show", "proves", "significantly improves", "reduces hallucinations", "sub-second", or other strong empirical language unless the cited finding has strong empirical, benchmark, peer-reviewed, or primary-source support.
- When evidence comes mostly from community posts, blogs, GitHub repos, or vendor/project docs, write it as reported practice, implementation guidance, or anecdotal evidence.
- When a Source type "github_repo" snapshot is present, use it for repository-specific claims and do not say no direct repository review was performed.
- If the audit coverage score is under 70 or the source set is mostly general/community sources, add an "Evidence Strength" section before recommendations.
- Include credibility-calibrated language: low-score sources may show claims/disputes, but do not present them as established facts unless corroborated.
- Use tables where comparisons are clearer than prose.
{visual_guidance}
- End with "Source Notes" summarizing source quality and follow-up searches.
- Keep it rigorous and decision-useful, not a search-result dump."""
        report = await cancel_registry.await_cancellable(
            _ask_report_streamed(http, ollama_url, events, report_id, final_prompt, model=run_model, default_model=default_model),
            report_id,
        )
        citation_audit = _validate_report_citations(report, sources)
        renderable_audit = _report_renderable_audit(report)
        if not citation_audit.get("valid"):
            warning = (
                "\n\n> [!WARNING]\n"
                "> Citation audit rejected invented source IDs: "
                + ", ".join(citation_audit.get("invalid", []))
                + ". Treat those references as unsupported unless they are corrected against the source list."
            )
            report = report.rstrip() + warning + "\n"
            await _emit_report_event(events, report_id, "research_audit", {
                "level": "warning",
                "message": "Final report contained invented citations.",
                "citation_audit": citation_audit,
            })
        summary = _one_line(re.sub(r"^# .+?\n", "", report.strip(), flags=re.DOTALL), 320)
        if not summary:
            summary = f"{template['label']} on {query}"

        metrics["elapsed"] = time.time() - t_start
        metrics["citation_audit"] = citation_audit
        metrics["renderable_audit"] = renderable_audit
        wrote = await db.update_research_report(
            report_id, status="complete", report_markdown=report, summary=summary,
            sources=sources, findings=findings, metrics={**metrics, "audit": audit},
            completed_at=datetime.utcnow().isoformat(),
            unless_status="cancelled",
        )
        if not wrote:
            # User cancelled while synthesis was finishing — keep the row cancelled.
            await _emit_report_event(events, report_id, "research_error", {
                "status": "cancelled", "error": "Cancelled by user",
            })
            return {"id": report_id, "status": "cancelled"}
        await db.replace_research_sources(report_id, sources)
        await _emit_report_event(events, report_id, "research_done", {
            "status": "complete", "summary": summary, "metrics": metrics,
        })
        return {
            "id": report_id, "status": "complete", "report": report, "sources": sources,
            "findings": findings, "metrics": metrics,
        }
    except cancel_registry.RunCancelled:
        await db.update_research_report(
            report_id, status="cancelled", error="Cancelled by user",
            metrics={"elapsed": time.time() - t_start},
            completed_at=datetime.utcnow().isoformat(),
        )
        await _emit_report_event(events, report_id, "research_error", {
            "status": "cancelled", "error": "Cancelled by user",
        })
        return {"id": report_id, "status": "cancelled"}
    except Exception as e:
        await db.update_research_report(
            report_id, status="failed", error=str(e),
            metrics={"elapsed": time.time() - t_start},
            completed_at=datetime.utcnow().isoformat(),
        )
        await _emit_report_event(events, report_id, "research_error", {
            "status": "failed", "error": str(e),
        })
        return {"id": report_id, "status": "failed", "error": str(e)}
    finally:
        try:
            cancel_registry.cleanup(report_id)
        except Exception:
            pass


async def run_deep_research(http, ollama_url: str, default_model: str, events,
                            topic: str, depth: int, focus: str, mode: str, topic_b: str, conv_id: str, kb_context: str = "") -> dict:
    """Native deep research engine — runs in-process with httpx."""
    import config
    searxng_url = config.SEARXNG_URL

    t_start = time.time()
    all_findings = []
    full_pages = []
    all_sources = []
    searched = set()
    fetched = set()
    key_entities = set()
    stats = {"searches": 0, "pages_read": 0, "results": 0}
    topic_words = set(topic.lower().split())

    async def do_search(query):
        if query in searched:
            return []
        searched.add(query)
        stats["searches"] += 1
        results = await _search_searxng(http, searxng_url, query)
        stats["results"] += len(results)
        return results

    async def parallel_search(queries):
        return await _batched_searches(queries, do_search, delay=_SEARCH_BATCH_DELAY_DEEP)

    async def parallel_fetch(urls, limit=5):
        pages = []
        for i in range(0, len(urls), limit):
            batch = urls[i:i+limit]
            to_fetch = [u for u in batch if u not in fetched]
            tasks = [_fetch_page(http, u) for u in to_fetch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for u, r in zip(to_fetch, results):
                fetched.add(u)
                if isinstance(r, dict) and r:
                    pages.append(r)
                    stats["pages_read"] += 1
        return pages

    # ── Quick mode ──
    if mode == "quick":
        results = await do_search(topic)
        all_findings.extend(results)
        elapsed = time.time() - t_start
        return {
            "report": "\n".join(f"[{i+1}] **{r['title']}**\n{r['url']}\n{r['content']}" for i, r in enumerate(results)),
            "sources": [{"index": i+1, "title": r["title"], "url": r["url"]} for i, r in enumerate(results)],
            "source_count": len(results), "total_searches": 1, "pages_read": 0,
            "key_entities": [], "elapsed": elapsed,
        }

    # ── Compare mode ──
    if mode == "compare" and topic_b:
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": f"🔵 Researching {topic[:30]}..."})
        ra = await parallel_search([topic, f"{topic} pros cons", f"{topic} use cases"])
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": f"🟠 Researching {topic_b[:30]}..."})
        rb = await parallel_search([topic_b, f"{topic_b} pros cons", f"{topic_b} use cases"])
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": "🔀 Head-to-head..."})
        rv = await parallel_search([f"{topic} vs {topic_b}", f"{topic_b} vs {topic}", f"{topic} compared to {topic_b}"])
        all_r = ra + rb + rv
        top_urls = _rank_urls(all_r, fetched)
        pages = await parallel_fetch(top_urls[:5])

        ctx = f"=== {topic} ===\n" + "\n".join(f"- {r['title']}: {r['content']}" for r in ra[:10])
        ctx += f"\n\n=== {topic_b} ===\n" + "\n".join(f"- {r['title']}: {r['content']}" for r in rb[:10])
        ctx += f"\n\n=== HEAD-TO-HEAD ===\n" + "\n".join(f"- {r['title']}: {r['content']}" for r in rv[:10])
        if pages:
            ctx += "\n\n=== FULL SOURCES ===\n" + "\n".join(f"--- {p['url']} ---\n{p['content'][:2000]}" for p in pages)

        report = await _ask_ollama_streamed(http, ollama_url, events, f"Write a comparison of {topic} vs {topic_b}.\n\nData:\n{ctx}\n\nCover: overview, differences, pros/cons, use cases, recommendation. Cite sources.", conv_id, "deep_research", default_model=default_model, status_prefix="⚖️ Comparing")
        elapsed = time.time() - t_start
        seen = set()
        srcs = []
        for r in all_r:
            if r["url"] and r["url"] not in seen:
                seen.add(r["url"])
                srcs.append({"index": len(srcs)+1, "title": r["title"], "url": r["url"]})
        return {"report": report, "sources": srcs[:20], "source_count": len(seen),
                "total_searches": stats["searches"], "pages_read": stats["pages_read"],
                "key_entities": [], "elapsed": elapsed}

    # ── PHASE 1: Discovery ──
    await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": "⚡ Phase 1: Discovery — casting nets..."})
    dq = [topic, f"{topic} explained", f"{topic} overview guide", f"what is {topic}"]
    if focus:
        dq.append(f"{topic} {focus}")
    disc = await parallel_search(dq)
    all_findings.extend(disc)
    for r in disc:
        if r.get("url"):
            all_sources.append(r["url"])

    entity_text = " ".join(f"{f.get('title','')} {f.get('content','')}" for f in all_findings[:15])
    key_entities = _extract_entities(entity_text, topic_words)

    if not all_findings:
        elapsed = time.time() - t_start
        await events.emit(conv_id, "tool_end", {"tool": "deep_research", "icon": "search", "status": f"⚠️ No search results (SearXNG may be down)"})
        return {
            "report": f"No search results found for '{topic}'. SearXNG search engine may be unavailable or returned no results. Try again or check the search service.",
            "sources": [], "source_count": 0, "total_searches": stats["searches"],
            "pages_read": 0, "key_entities": [], "elapsed": elapsed,
        }

    # ── PHASE 2: Deep Dive (depth >= 2) ──
    if depth >= 2:
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": f"🧬 Phase 2: Deep Dive — {len(key_entities)} entities extracted..."})
        top_urls = _rank_urls(all_findings, fetched)
        pages = await parallel_fetch(top_urls[:2 + depth])
        full_pages.extend(pages)

        for p in pages:
            pe = _extract_entities(p["content"], topic_words)
            key_entities.update(pe)

        eq = [f"{topic} {e}" for e in list(key_entities)[:5]]
        eq.extend([f"{topic} how it works", f"{topic} examples applications"])
        er = await parallel_search(eq[:6])
        all_findings.extend(er)
        for r in er:
            if r.get("url"):
                all_sources.append(r["url"])

    # ── PHASE 3: Cross-Reference (depth >= 3) ──
    if depth >= 3:
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": "🔗 Phase 3: Cross-referencing signal threads..."})
        xr = await parallel_search([
            f"{topic} latest news {datetime.now().year}", f"{topic} criticism problems",
            f"{topic} expert analysis", f"{topic} comparison alternatives",
        ])
        all_findings.extend(xr)
        for r in xr:
            if r.get("url"):
                all_sources.append(r["url"])
        new_top = _rank_urls(all_findings, fetched)
        new_pages = await parallel_fetch(new_top[:2])
        full_pages.extend(new_pages)

    # ── PHASE 4: Niche (depth >= 4) ──
    if depth >= 4:
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": "🔭 Phase 4: Niche angle scan..."})
        nq = [f"{topic} statistics data", f"{topic} case study", f"{topic} future trends",
              f"{topic} history timeline", f"{topic} how it works explained"]
        for ent in list(key_entities)[:3]:
            nq.append(f"{topic} {ent} details")
        nr = await parallel_search(nq)
        all_findings.extend(nr)
        for r in nr:
            if r.get("url"):
                all_sources.append(r["url"])
        new_top = _rank_urls(all_findings, fetched)
        new_pages = await parallel_fetch(new_top[:3])
        full_pages.extend(new_pages)

    # ── PHASE 5: Exhaustive (depth >= 5) ──
    if depth >= 5:
        await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": "🌊 Phase 5: Exhaustive sweep — draining the ocean..."})
        sq = [f"{topic} research paper academic", f"{topic} technical deep dive",
              f"{topic} misconceptions myths", f"{topic} advanced techniques",
              f"{topic} community discussion reddit"]
        ent_list = list(key_entities)[:4]
        for i, e1 in enumerate(ent_list):
            for e2 in ent_list[i+1:]:
                sq.append(f"{e1} {e2} {topic}")
        sr = await parallel_search(sq)
        all_findings.extend(sr)
        for r in sr:
            if r.get("url"):
                all_sources.append(r["url"])
        new_top = _rank_urls(all_findings, fetched)
        new_pages = await parallel_fetch(new_top[:3])
        full_pages.extend(new_pages)

    # ── SYNTHESIZE ──
    await events.emit(conv_id, "tool_start", {"tool": "deep_research", "icon": "search", "status": f"🧠 Neural synthesis — processing {len(all_findings)} findings..."})
    unique_sources = list(dict.fromkeys(s for s in all_sources if s))

    ctx_parts = []
    if full_pages:
        ctx_parts.append("═══ FULL PAGE CONTENT ═══")
        for p in full_pages[:10]:
            ctx_parts.append(f"━━━ {p['url']} ━━━\n{p['content'][:2500]}")
    ctx_parts.append("\n═══ SEARCH RESULTS ═══")
    seen_urls = set()
    for f in all_findings:
        if f.get("url") in seen_urls:
            continue
        seen_urls.add(f.get("url", ""))
        ctx_parts.append(f"[{len(seen_urls)}] {f['title']}\n    {f.get('url','')}\n    {f.get('content','')}")
        if len(seen_urls) >= 40:
            break

    # Prepend KB context if available (pre-existing knowledge from uploaded docs)
    kb_section = ""
    if kb_context:
        kb_section = f"\n═══ KNOWLEDGE BASE (uploaded documents) ═══\n{kb_context}\n"

    length = "1000-1500" if depth >= 4 else "700-1000" if depth >= 3 else "500-700" if depth >= 2 else "300-500"
    prompt = f"""Write a comprehensive research report on: {topic}{f' (focus: {focus})' if focus else ''}
{kb_section}
Research data:
{chr(10).join(ctx_parts)}

Requirements:
1. Executive summary (2-3 paragraphs)
2. All major themes discovered
3. Specific facts, figures, data where available
4. Note conflicting information or open questions
5. Reference sources inline [Source N]
6. Key takeaways at the end

Write flowing prose, NOT a list of results. Synthesize ideas across sources.
Target length: {length} words."""

    report = await _ask_ollama_streamed(http, ollama_url, events, prompt, conv_id, "deep_research", default_model=default_model, status_prefix="📡 Compiling intelligence")

    srcs = []
    seen = set()
    for f in all_findings:
        u = f.get("url", "")
        if u and u not in seen:
            seen.add(u)
            srcs.append({"index": len(srcs)+1, "title": f["title"], "url": u,
                         "thumbnail": f.get("thumbnail", ""), "type": f.get("type", "web"),
                         "snippet": f.get("content", "")[:200]})
        if len(srcs) >= 25:
            break

    elapsed = time.time() - t_start
    return {
        "report": report, "sources": srcs, "source_count": len(unique_sources),
        "total_searches": stats["searches"], "pages_read": stats["pages_read"],
        "key_entities": sorted(list(key_entities))[:15], "elapsed": elapsed,
    }


async def run_conspiracy_research(http, ollama_url: str, default_model: str, searxng_url: str, events,
                                  topic: str, angle: str, depth: int, conv_id: str, kb_context: str = "") -> str:
    """Run conspiracy research and return raw dossier text for the model."""
    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": f"🕵️ Opening case file: {topic[:45]}...",
    })

    topic_lower = topic.lower()

    # ── Wave 1: core conspiracy search queries ──
    base_queries = [
        topic,
        f"{topic} leaked documents evidence",
        f"{topic} whistleblower testimony firsthand account",
        f"{topic} FOIA declassified released files 2023 2024 {datetime.now().year}",
        f"{topic} cover up suppressed hidden truth",
        f"{topic} independent investigation expose proof",
        f'"{topic}" classified secret confidential',
        f"{topic} site:cryptome.org",
        f"{topic} site:theblackvault.com",
        f"{topic} site:muckrock.com",
        f"{topic} site:theintercept.com",
        f"{topic} site:ddosecrets.com",
        f"{topic} site:documentcloud.org leaked",
        f"{topic} site:archive.org",
        f"{topic} site:pastebin.com OR site:ghostbin.com leaked dump",
        f"{topic} telegram channel leaked exposed",
    ]
    if angle == "key_players":
        base_queries += [
            f"{topic} key individuals named persons",
            f"{topic} organizations involved connections",
            f"{topic} cui bono who benefits network",
            f"{topic} financiers funders backers",
        ]
    elif angle == "timeline":
        base_queries += [
            f"{topic} timeline chronology events sequence",
            f"{topic} history origins beginning",
            f"{topic} what happened when year date",
        ]
    elif angle == "debunk":
        base_queries += [
            f"{topic} official explanation response",
            f"{topic} debunked fact check real story",
            f"{topic} evidence against theory",
        ]
    elif angle == "documents":
        base_queries += [
            f"{topic} official government documents records",
            f"{topic} court filings evidence exhibits",
            f"{topic} site:courtlistener.com OR site:pacer.gov",
            f"{topic} site:documentcloud.org",
        ]
    elif angle == "connections":
        base_queries += [
            f"{topic} connections network links relationships",
            f"{topic} who knew what when",
            f"{topic} follow the money financial ties",
            f"{topic} site:opensecrets.org OR site:sec.gov/edgar",
        ]
    else:
        base_queries += [
            f"{topic} proof photographs evidence eyewitness",
            f"{topic} hidden truth real story exposed",
            f"{topic} alternative explanation theory",
            f"{topic} site:archive.org OR site:web.archive.org deleted removed",
        ]

    all_findings = []
    searched = set()
    full_pages = []
    fetched = set()
    stats = {"searches": 0, "pages_read": 0}

    async def _csearch(q, categories="general,news"):
        if q in searched:
            return []
        searched.add(q)
        stats["searches"] += 1
        return await _search_searxng(http, searxng_url, q, 12, categories=categories)

    await _batched_searches(base_queries, _csearch, delay=_SEARCH_BATCH_DELAY_CONSPIRACY, sink=all_findings)

    if not all_findings:
        await events.emit(conv_id, "tool_end", {
            "tool": "conspiracy_research", "icon": "search",
            "status": f"⚠️ No search results — SearXNG may be down",
        })
        return f"# ⚠️ CONSPIRACY RESEARCH FAILED\n\nNo search results found for '{topic}'. The SearXNG search engine returned 0 results — it may be offline or unreachable at {searxng_url}.\n\nTell the user the search service appears to be down and to try again shortly."

    _candidate_urls = [f["url"] for f in all_findings if f.get("url") and f["url"] not in fetched]
    _candidate_urls.sort(key=_source_tier)
    fetch_urls = _candidate_urls[:14]
    fetch_tasks = [_fetch_page(http, u) for u in fetch_urls]
    fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    for u, r in zip(fetch_urls, fetch_results):
        fetched.add(u)
        if isinstance(r, dict) and r:
            full_pages.append(r)
            stats["pages_read"] += 1

    # ── Wave 2: deep alt-media + declassified intel ──
    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": "📡 Wave 2: alt-media, dark web archives, leaked data...",
    })
    wave2 = [
        f"{topic} reddit r/conspiracy r/conspiracytheories r/C_S_T r/Conspiracyundone",
        f"{topic} reddit r/RealConspiracy r/conspiracy_commons r/conspiracynopol",
        f"{topic} CIA FBI NSA DIA operation program classified secret",
        f"{topic} Operation codename program black budget classified",
        f"{topic} 4chan pol archived exposed thread screencap",
        f"{topic} 8kun 8chan archive post leaked",
        f"{topic} recently declassified 2022 2023 2024 {datetime.now().year} released",
        f"{topic} national archives NARA declassified batch release",
        f"{topic} FOIA vault request documents obtained released",
        f"{topic} site:archives.gov OR site:cia.gov/readingroom OR site:vault.fbi.gov",
        f"{topic} site:ddosecrets.com",
        f"{topic} site:wikileaks.org/plusd OR site:wikileaks.org/gifiles",
        f"{topic} site:distributed-denial-of-secrets.com",
        f"{topic} site:bellingcat.com investigation",
        f"{topic} site:thegrayzone.com",
        f"{topic} site:mintpressnews.com",
        f"{topic} site:zerohedge.com",
        f"{topic} site:naturalnews.com",
        f"{topic} site:infowars.com OR site:prisonplanet.com",
        f"{topic} site:activistpost.com OR site:globalresearch.ca",
        f"{topic} site:childrenshealthdefense.org OR site:greenmedinfo.com",
        f"{topic} site:westernjournal.com OR site:thegatewaypundit.com",
        f"{topic} site:rumble.com OR site:bitchute.com exposed",
        f"{topic} site:substack.com investigative leaked",
        f"{topic} court case filing lawsuit deposition unsealed",
        f"{topic} congressional hearing testimony subpoena investigation",
        f"{topic} site:courtlistener.com",
        f"{topic} data dump hack exposed internal documents",
        f"{topic} email dump hacked internal memo revealed",
    ]
    await _batched_searches(wave2, _csearch, delay=_SEARCH_BATCH_DELAY_CONSPIRACY, sink=all_findings)

    _candidate_urls2 = [f["url"] for f in all_findings if f.get("url") and f["url"] not in fetched]
    _candidate_urls2.sort(key=_source_tier)
    fetch2 = _candidate_urls2[:16]
    ft2 = [_fetch_page(http, u) for u in fetch2]
    fr2 = await asyncio.gather(*ft2, return_exceptions=True)
    for u, r in zip(fetch2, fr2):
        fetched.add(u)
        if isinstance(r, dict) and r:
            full_pages.append(r)
            stats["pages_read"] += 1

    # ── WikiLeaks Wave ──
    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": "🔓 WikiLeaks: searching cables, leaks, and classified archives...",
    })
    wl_queries = [topic]
    if len(topic.split()) > 1:
        wl_queries.append(" ".join(topic.split()[:3]))
    if angle == "documents":
        wl_queries += [f"{topic} cable", f"{topic} memo", f"{topic} classified"]
    elif angle == "key_players":
        wl_queries += [f"{topic} persons named", f"{topic} individuals involved"]
    elif angle == "connections":
        wl_queries += [f"{topic} network", f"{topic} financial"]
    else:
        wl_queries += [f"{topic} leaked", f"{topic} secret", f"{topic} classified"]

    wl_tasks = [_search_wikileaks(http, searxng_url, q, 12) for q in wl_queries]
    wl_results = await asyncio.gather(*wl_tasks, return_exceptions=True)
    wl_count = 0
    for res in wl_results:
        if isinstance(res, list):
            all_findings.extend(res)
            wl_count += len(res)

    relevant_cols = _wikileaks_collections_for_topic(topic_lower)
    wl_col_urls = []
    for col in relevant_cols[:6]:
        info = _WL_COLLECTIONS.get(col)
        if info:
            col_name, col_url = info
            wl_col_urls.append(col_url)
            all_findings.append({
                "title": f"🔓 WikiLeaks: {col_name}",
                "url": col_url,
                "content": f"WikiLeaks {col_name} archive — direct collection relevant to {topic}",
                "engine": "wikileaks",
                "type": "web",
            })

    wl_fetch_urls = [u for u in wl_col_urls if u not in fetched][:4]
    wl_fetch_tasks = [_fetch_wikileaks_page(http, u) for u in wl_fetch_urls]
    wl_fetch_results = await asyncio.gather(*wl_fetch_tasks, return_exceptions=True)
    extra_wl_links: list[str] = []
    for u, r in zip(wl_fetch_urls, wl_fetch_results):
        fetched.add(u)
        if isinstance(r, dict) and r:
            full_pages.append(r)
            stats["pages_read"] += 1
            for lnk in r.get("doc_links", [])[:8]:
                if lnk not in fetched and lnk not in extra_wl_links:
                    extra_wl_links.append(lnk)
            for pdf in r.get("pdf_links", [])[:5]:
                all_findings.append({
                    "title": f"🔓 WikiLeaks PDF: {pdf.split('/')[-1]}",
                    "url": pdf,
                    "content": f"PDF document from WikiLeaks collection: {pdf}",
                    "engine": "wikileaks",
                    "type": "web",
                })

    wl_doc_urls = [
        f["url"] for f in all_findings
        if "wikileaks.org" in f.get("url", "") and f["url"] not in fetched
    ][:8]
    for lnk in extra_wl_links:
        if lnk not in wl_doc_urls and len(wl_doc_urls) < 12:
            wl_doc_urls.append(lnk)
    wl_doc_tasks = [_fetch_wikileaks_page(http, u) for u in wl_doc_urls]
    wl_doc_results = await asyncio.gather(*wl_doc_tasks, return_exceptions=True)
    for u, r in zip(wl_doc_urls, wl_doc_results):
        fetched.add(u)
        if isinstance(r, dict) and r:
            full_pages.append(r)
            stats["pages_read"] += 1

    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": f"🔓 WikiLeaks: {wl_count} documents found, {len(relevant_cols)} collections matched",
    })

    # ── Wave 3: specialized archives & primary sources ──
    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": "🏛️ Wave 3: primary archives, court records, FOIA vaults...",
    })

    direct_urls = []

    wave3_queries = []

    if any(k in topic_lower for k in ["epstein", "jeffrey", "maxwell", "trafficking", "lolita"]):
        direct_urls += [
            "https://www.courtlistener.com/?q=epstein&type=r&order_by=score+desc",
            "https://vault.fbi.gov/jeffrey-epstein",
            "https://www.documentcloud.org/app#search/q=epstein",
            "https://muckrock.com/foi/list/?q=epstein",
            "https://www.justice.gov/usao-sdny/pr/jeffrey-epstein-indicted-federal-sex-trafficking-charges",
        ]
        wave3_queries += [
            "Epstein flight logs passengers names list",
            "Epstein island Little Saint James visitors",
            "Ghislaine Maxwell trial testimony deposition unsealed",
            "Epstein network financiers funders named",
            "Epstein blackmail intelligence operation Mossad CIA",
            "Epstein Wexner Les financial relationship",
            "Virginia Giuffre affidavit deposition names",
        ]

    if any(k in topic_lower for k in ["9/11", "nine eleven", "september 11", "wtc", "world trade", "twin towers"]):
        direct_urls += [
            "https://www.archives.gov/research/9-11",
            "https://www.fbi.gov/history/famous-cases/911-investigation",
            "https://www.cia.gov/readingroom/search/site/9-11",
            "https://vault.fbi.gov/9-11-investigation",
        ]
        wave3_queries += [
            "9/11 declassified 28 pages Saudi Arabia funding",
            "9/11 NORAD stand down order who gave",
            "9/11 insider trading put options before attack",
            "9/11 Building 7 collapse NIST report criticized",
            "9/11 commission omissions suppressed evidence",
            "9/11 hijackers CIA asset connections",
        ]

    if any(k in topic_lower for k in ["jfk", "kennedy", "assassination", "warren commission", "oswald"]):
        direct_urls += [
            "https://www.archives.gov/research/jfk",
            "https://www.maryferrell.org/pages/Main_Page.html",
            "https://www.cia.gov/readingroom/search/site/kennedy",
            "https://www.woodrowwilsoncenter.org/article/jfk-documents",
        ]
        wave3_queries += [
            "JFK assassination declassified documents CIA withheld",
            "Lee Harvey Oswald CIA handler contact",
            "JFK magic bullet theory disputed forensics",
            "JFK assassination multiple shooters Grassy Knoll witnesses",
            "George HW Bush CIA Dallas 1963",
        ]

    if any(k in topic_lower for k in ["cia", "mkultra", "mk ultra", "mind control", "monarch"]):
        direct_urls += [
            "https://www.cia.gov/readingroom/search/site/mkultra",
            "https://vault.fbi.gov/search?q=mind+control",
            "https://www.archives.gov/research/church-committee",
        ]

    if any(k in topic_lower for k in ["ufo", "uap", "alien", "roswell", "area 51", "pentagon ufo", "disclosure"]):
        direct_urls += [
            "https://www.archives.gov/research/ufo",
            "https://theblackvault.com/documentvault/ufo/",
            "https://vault.fbi.gov/unexplained-phenomenon",
            "https://www.aaro.mil/",
        ]
        wave3_queries += [
            "UAP UFO congressional testimony 2023 2024 whistleblower",
            "David Grusch UAP non-human intelligence testimony",
            "UAP crash retrieval program secret Pentagon",
            "Skinwalker Ranch government program AAWSAP",
        ]

    if any(k in topic_lower for k in ["covid", "coronavirus", "pandemic", "lab leak", "wuhan", "vaccine", "mrna"]):
        direct_urls += [
            "https://www.documentcloud.org/app#search/q=fauci+covid",
            "https://muckrock.com/foi/list/?q=covid+lab+leak",
        ]
        wave3_queries += [
            "COVID-19 lab leak Wuhan Institute Virology evidence",
            "Fauci NIH EcoHealth gain of function funding",
            "COVID pandemic preparedness simulation Event 201",
            "FOIA Fauci emails released EcoHealth",
            "mRNA vaccine adverse events VAERS suppressed data",
        ]

    if any(k in topic_lower for k in ["rothschild", "rockefeller", "bilderberg", "davos", "wef", "nwo", "new world order", "illuminati", "deep state"]):
        wave3_queries += [
            "Bilderberg Group meeting attendees decisions leaked minutes",
            "World Economic Forum great reset agenda 2030 criticism exposed",
            "Council on Foreign Relations members influence policy media",
            "Trilateral Commission membership decisions exposed documents",
            "Committee of 300 Club of Rome global governance",
            f"{topic} site:theblackvault.com OR site:cryptome.org",
        ]

    if any(k in topic_lower for k in ["great reset", "agenda 2030", "agenda 21", "depopulation", "georgia guidestones", "population control"]):
        wave3_queries += [
            "UN Agenda 2030 sustainable development depopulation goals",
            "Great Reset WEF Schwab you will own nothing",
            "Agenda 21 local implementation land grab documents",
            "Gates Foundation depopulation vaccines funding eugenics",
            "Deagel population forecast 2025 depopulation prediction",
        ]

    if any(k in topic_lower for k in ["big pharma", "fda corruption", "cdc corruption", "pharmaceutical", "drug company", "sackler", "opioid"]):
        direct_urls += [
            "https://www.documentcloud.org/app#search/q=FDA+suppressed",
            "https://muckrock.com/foi/list/?q=FDA+CDC",
        ]
        wave3_queries += [
            f"{topic} FDA approval corruption revolving door lobbying",
            f"{topic} clinical trial data suppressed hidden adverse events",
            f"{topic} whistleblower FDA CDC internal documents",
            "pharmaceutical company internal memo leaked suppressed data",
        ]

    if any(k in topic_lower for k in ["chemtrail", "geoengineering", "haarp", "weather modification", "cloud seeding"]):
        direct_urls += [
            "https://www.geoengineeringwatch.org",
            "https://patents.google.com/?q=weather+modification",
        ]
        wave3_queries += [
            "geoengineering weather modification patent documents evidence",
            "HAARP ionosphere program declassified documents",
            "cloud seeding admitted government program",
            "stratospheric aerosol injection SAI program documents",
        ]

    if any(k in topic_lower for k in ["surveillance", "nsa", "prism", "snowden", "five eyes", "mass surveillance", "spying"]):
        direct_urls += [
            "https://theintercept.com/snowden-sidtoday/",
            "https://www.theguardian.com/us-news/the-nsa-files",
            "https://cryptome.org",
        ]
        wave3_queries += [
            "NSA PRISM XKEYSCORE Snowden documents leaked",
            "Five Eyes intelligence sharing program documents",
            "GCHQ mass surveillance program Tempora documents",
            "NSA bulk collection program court ruled illegal",
            f"{topic} Snowden documents leaked NSA files",
        ]

    # ── Execute wave 3 queries in batches ──
    if wave3_queries:
        await _batched_searches(wave3_queries, _csearch, delay=_SEARCH_BATCH_DELAY_CONSPIRACY, sink=all_findings)

    direct_urls += [
        "https://vault.fbi.gov/",
        "https://www.cia.gov/readingroom/",
        "https://cryptome.org",
        "https://ddosecrets.com",
    ]

    wave3_fetch = [u for u in direct_urls if u not in fetched]
    w3_tasks = [_fetch_gov_doc_index(http, u) for u in wave3_fetch]
    w3_results = await asyncio.gather(*w3_tasks, return_exceptions=True)
    for u, gr in zip(wave3_fetch, w3_results):
        fetched.add(u)
        if isinstance(gr, dict) and gr:
            full_pages.append(gr)
            stats["pages_read"] += 1
            for pdf_url in gr.get("pdf_links", [])[:5]:
                all_findings.append({
                    "title": f"📄 Document: {pdf_url.split('/')[-1]}",
                    "url": pdf_url,
                    "content": f"Primary source document from {u}",
                })

    await events.emit(conv_id, "tool_start", {
        "tool": "conspiracy_research", "icon": "search",
        "status": f"🧠 Assembling dossier: {stats['searches']} searches, {stats['pages_read']} pages read...",
    })

    # ── Build raw dossier for model synthesis ──
    parts = [f"# 🕵️ CONSPIRACY DOSSIER: {topic}"]
    parts.append(f"**Angle:** {angle} | **Searches:** {stats['searches']} | **Pages read:** {stats['pages_read']}\n")
    parts.append("---")

    # Prepend KB context (pre-existing knowledge from uploaded documents)
    if kb_context:
        parts.append("\n## 📚 KNOWLEDGE BASE (uploaded documents)\n")
        parts.append(kb_context)
        parts.append("\n---")

    if full_pages:
        full_pages.sort(key=lambda p: _source_tier(p['url']))
        parts.append("\n## 📄 PRIMARY SOURCE CONTENT\n")
        for p in full_pages[:14]:
            url_label = p['url']
            content_snippet = p['content'][:3000]
            parts.append(f"### Source: {url_label}\n{content_snippet}\n")

    parts.append("\n## 🔍 SEARCH FINDINGS\n")
    seen = set()
    for f in all_findings:
        url = f.get("url", "")
        if url in seen or not url:
            continue
        seen.add(url)
        parts.append(f"**[{len(seen)}]** [{f.get('title','(no title)')}]({url})\n> {f.get('content','')[:300]}\n")
        if len(seen) >= 60:
            break

    srcs = []
    seen2 = set()
    for f in all_findings:
        u = f.get("url", "")
        if u and u not in seen2:
            seen2.add(u)
            srcs.append(f"[{len(srcs)+1}] {f.get('title','?')} — {u}")
        if len(srcs) >= 40:
            break
    if srcs:
        parts.append("\n## 📚 SOURCE INDEX\n")
        parts.extend(srcs)

    source_links = []
    seen_sl = set()
    for f in all_findings:
        u = f.get("url", "")
        if u and u not in seen_sl:
            seen_sl.add(u)
            source_links.append({"title": f.get("title", ""), "url": u})
        if len(source_links) >= 30:
            break
    await events.emit(conv_id, "source_links", {
        "tool": "conspiracy_research",
        "links": source_links,
    })

    await events.emit(conv_id, "tool_end", {
        "tool": "conspiracy_research", "icon": "search",
        "status": f"🕵️ Dossier ready: {len(seen2)} sources, {stats['searches']} searches, {stats['pages_read']} pages",
        "detail": json.dumps({"topic": topic, "angle": angle, "source_count": len(seen2), "pages_read": stats["pages_read"]}),
    })

    return "\n".join(parts)
