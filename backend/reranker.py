"""LLM reranker for RAG hybrid retrieval.

Reorders the fused (RRF) candidate list from `rag.hybrid_query` with a single
batched relevance-scoring call to a small local model (Workspace Model) before
the top_k cut. One LLM call scores all candidates at once — not one call per
chunk — so the added latency is a single short generation.

Contract:
  - Config-gated (`config.RAG_RERANKER == "llm"`), off by default.
  - Fails open: any LLM error, timeout, or unparseable output returns the
    original fused order — retrieval quality can only degrade to the status
    quo, never below it.
  - Local-only: the scoring model goes through `model_providers.reject_cloud`,
    so a cloud-prefixed chat model can never silently route scoring calls to
    a paid API.
"""

import asyncio
import json
import re

import httpx

import config
import model_providers

# How many fused candidates are offered to the scorer. hybrid_query fetches
# ~3x top_k per leg; 12 balances coverage against what a 4B scorer can
# reliably enumerate (16 made it drop entries) and keeps the prompt short.
RERANK_CANDIDATES = 12
_EXCERPT_CHARS = 600

# Index-KEYED output on purpose: small models miscount long plain arrays
# (observed live: 10 scores for 16 excerpts — twice), which misaligns
# positions and forces a fail-open. Keyed entries stay aligned even when the
# model skips or truncates some.
_PROMPT = """You are a search relevance judge. Score how relevant each excerpt is to the query on a 0-10 scale (10 = directly answers it, 0 = unrelated).

Query: {query}

Excerpts:
{excerpts}

Respond with ONLY a JSON object mapping each excerpt number to its score, covering all {n} excerpts, no explanation, no other keys:
{{"1": s1, "2": s2, ..., "{n}": s{n}}}"""


def enabled() -> bool:
    return str(getattr(config, "RAG_RERANKER", "none")).strip().lower() == "llm"


def _scoring_model() -> str:
    return (
        model_providers.reject_cloud(getattr(config, "WORKSPACE_MODEL", "") or "")
        or model_providers.reject_cloud(getattr(config, "DEFAULT_MODEL", "") or "")
    )


def _clamp(v) -> float:
    return max(0.0, min(10.0, float(v)))


def _parse_scores(raw: str, n: int) -> list[float] | None:
    """Parse scorer output into exactly n scores; None = fail open.

    Accepts (in order of preference):
      - index-keyed object {"1": s1, ...} — partial results stay position-
        aligned; missing indices get a neutral 5.0
      - {"scores": [...]} or a bare array — clipped when over-long, rejected
        when short (positions would misalign)
      - regex-recovered "idx": score pairs from truncated/dirty output
    """
    if not (raw or "").strip():
        return None
    obj = None
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\d\s.,eE+-]+\]", raw)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
        if obj is None:
            # Truncated/dirty keyed output — salvage the "idx": score pairs.
            pairs = re.findall(r'"(\d+)"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
            if pairs:
                obj = {k: v for k, v in pairs}
            else:
                return None
    if isinstance(obj, dict) and isinstance(obj.get("scores"), (list, dict)):
        obj = obj["scores"]
    if isinstance(obj, dict):
        scores = [5.0] * n  # neutral default keeps unscored items mid-pack
        seen = 0
        for k, v in obj.items():
            try:
                i = int(str(k).strip())
                s = _clamp(v)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= n:
                scores[i - 1] = s
                seen += 1
        return scores if seen >= max(2, n // 2) else None
    if not isinstance(obj, list):
        return None
    # More scores than excerpts: trust the leading n (models occasionally
    # append extras). Fewer is unrecoverable — positions would misalign.
    if len(obj) > n:
        obj = obj[:n]
    if len(obj) != n:
        return None
    scores = []
    for s in obj:
        try:
            scores.append(_clamp(s))
        except (TypeError, ValueError):
            return None
    return scores


async def rerank(query_text: str, chunks: list[dict], top_k: int) -> list[dict]:
    """Reorder fused candidates by LLM relevance score, then cut to top_k.

    `chunks` is the full fused/ordered list from hybrid_query (pre-truncation).
    Ties and parse failures preserve the incoming RRF order.
    """
    if not enabled() or len(chunks) <= 1:
        return chunks[:top_k]
    model = _scoring_model()
    if not model:
        return chunks[:top_k]

    candidates = chunks[:RERANK_CANDIDATES]
    excerpt_lines = []
    for i, c in enumerate(candidates):
        flat = re.sub(r"\s+", " ", str(c.get("text") or ""))[:_EXCERPT_CHARS]
        excerpt_lines.append(f"[{i + 1}] {flat}")
    excerpts = "\n".join(excerpt_lines)
    prompt = _PROMPT.format(query=query_text.strip()[:500], excerpts=excerpts,
                            n=len(candidates))
    timeout_s = max(2.0, float(getattr(config, "RAG_RERANK_TIMEOUT", 8) or 8))

    try:
        async with httpx.AsyncClient(timeout=timeout_s + 5) as http:
            raw = await asyncio.wait_for(
                model_providers.complete_chat(
                    http, model, prompt,
                    temperature=0.0, num_ctx=8192, num_predict=256,
                    format_json=True, timeout=int(timeout_s) + 5,
                    ollama_url=config.OLLAMA_URL,
                ),
                timeout=timeout_s,
            )
    except Exception as e:
        print(f"[RERANK] fail-open ({type(e).__name__}: {e}) — keeping fused order")
        return chunks[:top_k]

    scores = _parse_scores(raw, len(candidates))
    if scores is None:
        print(f"[RERANK] unparseable scores ({(raw or '')[:80]!r}) — keeping fused order")
        return chunks[:top_k]

    order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
    reranked = []
    for i in order:
        c = dict(candidates[i])
        c["rerank_score"] = scores[i]
        reranked.append(c)
    dropped = [i + 1 for i in order[top_k:]]
    if dropped:
        print(f"[RERANK] reordered {len(candidates)} candidates → top {top_k} (dropped fused ranks {dropped[:6]})")
    # Anything beyond the candidate window keeps its fused position behind.
    return (reranked + chunks[len(candidates):])[:top_k]
