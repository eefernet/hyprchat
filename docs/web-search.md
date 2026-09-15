# Interactive web search

Quick Search, the model's `research` tool, council chat, and the standalone
Quick Search endpoint use `quick_search.run_quick_search_for_chat` and
`search_agent.run_search_agent`. Deep Research keeps its iterative workflow
and shares the SearXNG provider and safe fetcher.

## Execution

A deterministic plan starts immediately. Query cleanup separates answer-format
instructions from the subject and preserves country acronyms such as US.
Balanced mode starts with two queries;
quality starts with three. A local planner runs concurrently and contributes
queries only if it has finished when the first wave is collected. Total planned
query budgets are two/four/five in speed/balanced/quality modes. Literal and
refinement fallbacks share the time limit; same-day questions also get a small
general-web fallback for undated event pages.

`SearchRun` in `search_runtime.py` holds a per-request deadline, diagnostics,
completed sources/pages and child tasks. The total budget is 6/12/20 seconds for
speed/balanced/quality, derived from `QUICK_SEARCH_TIMEOUT=12`. Provider waits,
embedding reranking, DNS, page fetches and thumbnails all fit inside that
budget. Completed sources survive a slow peer or optional-stage timeout.
Cancellation propagates and outstanding asynchronous work is cancelled and
joined. HTML extraction runs off the event loop with a bounded response size.

Public fetches retain `research.fetch_bytes_safely` and the configured VPN
proxy. Redirect and DNS checks still reject private addresses. Neither a VPN
failure nor an exhausted budget enables direct-network bypass.

## Ranking and context

Results merge by URL after removing fragments and known tracking parameters;
meaningful query parameters stay distinct. All contributing queries and engines
remain available. Heuristic ranking considers relevance, publication freshness,
source category, snippet quality and domain diversity. Publication freshness
decays within the requested window, and a historical year alone does not apply
a current-month filter. Since SearXNG has no native week filter, weekly requests
retrieve with its month filter and retain a seven-day ranking preference.

Optional Ollama embedding reranking uses weighted reciprocal-rank fusion:
heuristic rank has weight 2, semantic rank 1, and query-consensus rank 0.5.
Nomic text models receive `search_query:` and `search_document:` prefixes.
Embedding similarity alone cannot remove a source: duplicate suppression also
requires very high text overlap. This preserves independent conflicting
accounts. Embedding failure preserves heuristic results. A failed modern embed
request does not fan out into many legacy calls; legacy fallback is reserved
for an unsupported endpoint.

Page excerpts are selected across the extracted article using query terms,
rather than always taking its beginning. Unrelated passages do not pad an excerpt
when matching passages exist. Answer images are optional, and citation/image
URLs must come from supplied source records. Context is packed as complete source
records with URLs and dates using `context_policy.estimate_tokens`, capped at
one quarter of chat input capacity and 3,500 estimated tokens (1,500 for councils).
This is an estimate, not a tokenizer guarantee. Sources are marked as untrusted
text and the prompt distinguishes publication dates from event dates. Undated
snippets saying “today” are insufficient same-day evidence.

## Diagnostics and compatibility

The list-returning provider API remains compatible with Deep Research. Optional
diagnostics distinguish no matches, connection failures, proxy failures,
timeouts, HTTP errors, invalid responses, rate limits, suspended engines and
partial success. Health checks use the same classifier. An HTTP 200 listener
with failing engines is degraded, not automatically “rate limited.”

Existing tool names and schemas remain unchanged. Automatic and council search
keep `tool_done` and `source: quick_search`; model `research` keeps `tool_end`
and its existing carousel channel. New status/diagnostic/provenance fields are
additive. Cache keys include the provider URL and filters; cached results are
copied and count-limited. TTLs are 60 seconds for day searches, 300 for other
filtered searches and 600 for evergreen searches. Empty failures are not cached.

No paid provider or new reranking service is required. Existing controls remain:
`QUICK_SEARCH_PLANNER=deterministic`, `QUICK_SEARCH_RERANKER=none`, and
`QUICK_SEARCH_RANKING=legacy` disable optional planning, embeddings, and the new
embedding fusion respectively. The legacy ranking switch does not disable
routing safeguards, deadlines or diagnostics.

## Validation

```bash
python3 -m pytest backend/tests/test_search_agent.py backend/tests/test_search_runtime.py backend/tests/test_research_quality_upgrade.py backend/tests/test_agent_research_hardening.py -q
```

The synthetic ranking fixtures cover dated news, conflicting accounts, a
technical primary source and an evergreen game query. The new ranker retained
6/6 expected sources at each fixture's specified cutoff; legacy retained 3/6.
These cases verify known failure modes, not general search-quality superiority.
Live comparisons depend on engine availability, rate limits and model warmth.

See [SearXNG operations](../scripts/searxng/README.md) for VPN recovery and
configuration backup instructions. `search_runtime.py` is included in the deploy
watch list; infrastructure scripts require manual deployment.

Design references: [SearXNG API](https://docs.searxng.org/dev/search_api.html),
[Nomic task prefixes](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5),
and [reciprocal rank fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf).
