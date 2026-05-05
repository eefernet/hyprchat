"""
Research engines — deep research, conspiracy research, and search helpers.
"""
import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime

# ── Search rate-limit tuning ──
_SEARCH_BATCH_SIZE = 3
_SEARCH_BATCH_DELAY_DEEP = 2.0          # seconds between batches in deep research
_SEARCH_BATCH_DELAY_CONSPIRACY = 2.5    # seconds between batches in conspiracy research


async def _search_google_fallback(http, query: str, count: int = 10) -> list:
    """Fallback: scrape Google search results when SearXNG is down."""
    try:
        params = urllib.parse.urlencode({"q": query, "num": count, "hl": "en"})
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        r = await http.get(f"https://www.google.com/search?{params}", timeout=12, headers=headers, follow_redirects=True)
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


async def _search_searxng(http, searxng_url: str, query: str, count: int = 10, categories: str = "general", safesearch: str | None = None) -> list:
    """Search SearXNG and return structured results. Falls back to Google scrape if SearXNG returns nothing."""
    results = []
    try:
        _params = {"q": query, "format": "json", "language": "en", "categories": categories}
        if safesearch is not None:
            _params["safesearch"] = safesearch
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
            elif thumbnail or any(url_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                r_type = "image"
            results.append({
                "title": item.get("title", ""), "url": url,
                "content": (item.get("content", "") or "")[:500],
                "engine": item.get("engine", ""), "score": item.get("score", 0),
                "thumbnail": thumbnail, "type": r_type,
            })
        for box in data.get("infoboxes", []):
            results.append({
                "title": box.get("infobox", "Infobox"),
                "url": (box.get("urls", [{}])[0].get("url", "") if box.get("urls") else ""),
                "content": box.get("content", ""), "engine": "infobox", "score": 100,
            })
    except Exception:
        pass
    # Fallback to Google scrape if SearXNG returned nothing
    if not results:
        results = await _search_google_fallback(http, query, count)
    return results


async def _search_wikileaks(http, searxng_url: str, query: str, count: int = 15) -> list:
    """Search WikiLeaks directly via their search API, with SearXNG fallback."""
    results = []
    try:
        params = urllib.parse.urlencode({"query": query, "include_onion": "false"})
        r = await http.get(
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
                import re as _re2
                for m in _re2.finditer(r'href="(https?://wikileaks\.org/[^"]+)"[^>]*>([^<]{5,200})<', text):
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


async def _fetch_page(http, url: str) -> dict | None:
    """Fetch and clean a web page."""
    skip = ["youtube.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
            ".pdf", "linkedin.com", "tiktok.com",
            "snopes.com", "politifact.com", "factcheck.org", "leadstories.com",
            "fullfact.org", "mediabiasfactcheck.com"]
    if any(p in url.lower() for p in skip):
        return None
    try:
        r = await http.get(url, timeout=15, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
        if r.status_code >= 400:
            return None
        ct = r.headers.get("content-type", "")
        if "text" not in ct and "json" not in ct:
            return None
        text = r.text
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
        text = re.sub(r"-----BEGIN PGP [A-Z ]+-----.*?-----END PGP [A-Z ]+-----", "[PGP block removed]", text, flags=re.DOTALL)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
        if len(text) < 200:
            return None
        return {"url": url, "content": text[:6000]}
    except Exception:
        return None


async def _fetch_gov_doc_index(http, url: str) -> dict | None:
    """Fetch government document index pages (including PDF links) for conspiracy research."""
    try:
        r = await http.get(url, timeout=15, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"})
        ct = r.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return None
        text = r.text
        pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', text, re.IGNORECASE)
        doc_links = re.findall(r'href=["\']([^"\']*(?:document|file|exhibit|report)[^"\']*)["\']', text, re.IGNORECASE)
        for tag in ["script", "style", "nav", "header", "footer"]:
            text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        result = {"url": url, "content": text[:5000], "pdf_links": [], "doc_links": []}
        base = "/".join(url.split("/")[:3])
        for lnk in pdf_links[:20]:
            full = lnk if lnk.startswith("http") else base + lnk
            result["pdf_links"].append(full)
        for lnk in doc_links[:10]:
            full = lnk if lnk.startswith("http") else base + lnk
            result["doc_links"].append(full)
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


async def _fetch_wikileaks_page(http, url: str) -> dict | None:
    """Fetch a WikiLeaks page, extracting article text and document/PDF links."""
    lower = url.lower()
    if any(lower.endswith(ext) for ext in (".zip", ".tar", ".gz", ".rar", ".7z")):
        return {"url": url, "content": f"[Archive file — direct download: {url}]"}
    if ".pdf" in lower:
        return {"url": url, "content": f"[PDF document — direct download: {url}]"}
    try:
        r = await http.get(url, timeout=15, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"})
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return None
        text = r.text
        base = "/".join(url.split("/")[:3])

        wl_links = re.findall(r'href=["\']((https?://(?:www\.)?wikileaks\.org)?(/[^"\'#?][^"\']*?))["\']', text, re.IGNORECASE)
        pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', text, re.IGNORECASE)

        doc_links = []
        for match in wl_links[:30]:
            full = match[0] if match[0].startswith("http") else base + match[2]
            if full != url and full not in doc_links:
                doc_links.append(full)
        pdf_full = []
        for lnk in pdf_links[:15]:
            full = lnk if lnk.startswith("http") else base + "/" + lnk.lstrip("/")
            pdf_full.append(full)

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
        text = re.sub(r"-----BEGIN PGP [A-Z ]+-----.*?-----END PGP [A-Z ]+-----", "[PGP block removed]", text, flags=re.DOTALL)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

        if len(text) < 100:
            return None

        result: dict = {"url": url, "content": text[:6000]}
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
    try:
        r = await http.post(f"{ollama_url}/api/generate", json={
            "model": model or default_model,
            "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_predict": max_tokens},
        }, timeout=180)
        data = r.json()
        return (data.get("response", "") or "").strip()
    except Exception as e:
        return f"[AI synthesis failed: {e}]"


async def _ask_ollama_streamed(
    http, ollama_url: str, events, prompt: str, conv_id: str, tool_name: str,
    model: str = None, default_model: str = "qwen3.5:27b",
    max_tokens: int = 4096, status_prefix: str = "🧠 Synthesizing",
) -> str:
    """Stream from Ollama, emitting periodic status events so the user sees live progress."""
    accumulated = ""
    last_emit_len = 0
    try:
        async with http.stream("POST", f"{ollama_url}/api/generate", json={
            "model": model or default_model,
            "prompt": prompt, "stream": True,
            "options": {"temperature": 0.3, "num_predict": max_tokens},
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
        return accumulated.strip()
    except Exception as e:
        return f"[AI synthesis failed: {e}]"


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
        flat = []
        for batch_start in range(0, len(queries), _SEARCH_BATCH_SIZE):
            batch = queries[batch_start:batch_start + _SEARCH_BATCH_SIZE]
            tasks = [do_search(q) for q in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    flat.extend(r)
            if batch_start + _SEARCH_BATCH_SIZE < len(queries):
                await asyncio.sleep(_SEARCH_BATCH_DELAY_DEEP)
        return flat

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

    for batch_start in range(0, len(base_queries), _SEARCH_BATCH_SIZE):
        batch = base_queries[batch_start:batch_start + _SEARCH_BATCH_SIZE]
        tasks = [_csearch(q) for q in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_findings.extend(r)
        if batch_start + _SEARCH_BATCH_SIZE < len(base_queries):
            await asyncio.sleep(_SEARCH_BATCH_DELAY_CONSPIRACY)

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
    for batch_start in range(0, len(wave2), _SEARCH_BATCH_SIZE):
        batch = wave2[batch_start:batch_start + _SEARCH_BATCH_SIZE]
        t2 = [_csearch(q) for q in batch]
        r2 = await asyncio.gather(*t2, return_exceptions=True)
        for r in r2:
            if isinstance(r, list):
                all_findings.extend(r)
        if batch_start + _SEARCH_BATCH_SIZE < len(wave2):
            await asyncio.sleep(_SEARCH_BATCH_DELAY_CONSPIRACY)

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
        for batch_start in range(0, len(wave3_queries), _SEARCH_BATCH_SIZE):
            batch = wave3_queries[batch_start:batch_start + _SEARCH_BATCH_SIZE]
            tasks = [_csearch(q) for q in batch if q not in searched]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        all_findings.extend(r)
            if batch_start + _SEARCH_BATCH_SIZE < len(wave3_queries):
                await asyncio.sleep(_SEARCH_BATCH_DELAY_CONSPIRACY)

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
