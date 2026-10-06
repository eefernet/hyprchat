# Deep Research reports

The saved-report panel uses `run_research_report` in `backend/research.py`.
The legacy chat/Daedalus `run_deep_research` tool remains separate.

## Depth and writing targets

| Depth | Search query budget | Source target | Full-page read budget | Body word target |
| --- | ---: | ---: | ---: | ---: |
| 1 | 6 | 12 | 5 | 400–700 |
| 2 | 9 | 22 | 9 | 700–1,200 |
| 3 | 13 | 34 | 14 | 1,200–2,000 |
| 4 | 18 | 48 | 20 | 2,000–3,000 |
| 5 | 24 | 65 | 28 | 3,000–5,000 |

These are budgets and targets, not promised amounts of evidence. Failed reads
do not count as pages read. About 40% of the page budget at depths 2–5 is
reserved for adaptive research. Previously admitted sources retain their IDs
through reranking, so the final source count can exceed the source target.
Explicit numeric word requests override the default writing target.

Depths 4–5 write each template section separately, with executive summaries
written last. Each step retrieves original passages for its questions. Lower
depths use a single bounded draft unless an explicit long-form word request
requires sections. Up to two section/full-draft repairs address
completion problems and advisory review feedback. A word shortfall is disclosed;
missing evidence must not be replaced with invented detail or padding.
When the entire stored corpus has fewer than 2,000 characters, the writer uses
one concise draft with a target capped at 600 words. The original depth target
and the evidence limitation remain in metrics; the report explains the reduced
target. It does not run expansion repairs just to lengthen a thin report.

## Evidence storage and retrieval

`research_evidence.py` retains report-owned documents and chunks in SQLite,
including source IDs, URLs, headings, character offsets and truncation metadata.
FTS5 supplies keyword retrieval; a separate Chroma collection supplies semantic
retrieval with the configured local embedding model. Each query is searched
independently, ranks are fused, and overlapping adjacent passages are joined.
The existing optional reranker is reused. Embedding failure leaves keyword
retrieval and original text available. Evidence never enters a user's global KB.

Bounds: 100,000 characters per document, 2,000,000 per report; chunks of roughly
512 tokens with 64-token overlap. Fetches cap bodies at 2 MiB and decoded text
at 400,000 characters. Text PDFs retain page headings; scanned PDFs without
extractable text are reported unreadable. Storage truncation is disclosed.

`research_writer.py` budgets instructions, excerpts, output and a safety reserve
against the existing Settings-owned research context. It does not raise the
configured context. Retrieval IDs and generation finish reasons are retained
in report metrics. SQLite is the durable text store; Chroma is an index.

## Completion and recovery

Writing checkpoints replace the saved Markdown with increasing revisions.
The frontend rejects older snapshots and polling results. Refreshing restores
the saved draft; process restart does not resume inference automatically.
An interrupted stream, invalid/missing citations, omitted sections, unclosed
fences or citations absent from the writing evidence produce a partial report
after bounded repairs. Empty output fails. Model coverage scores and draft
reviews are advisory and do not certify factual accuracy.

The panel links grouped citations, lists every source, and exposes retained
passages through the user-scoped evidence endpoint. Markdown, PDF and print
exports retain completion/evidence warnings. Old reports without stored
passages remain readable and show evidence as unavailable.

Deleting a report stops its runner, cascades through documents/chunks/FTS, and
queues Chroma deletion durably. Cleanup retries on deletion, startup and the
six-hour maintenance loop. A cancelled embedding worker must finish publication
before deletion can remove its index, preventing late recreation.

## Validation boundaries

Deterministic tests cover isolation, deletion/cancellation races, stream errors,
depth budgets, context bounds, citations and snapshots. Browser checks cover
diagram recovery, all-source visibility, evidence inspection and exports.
Live reports still depend on upstream search quality and model judgment. Search
engine failures and discarded off-topic results are retained as diagnostics;
longer output or valid citation syntax alone is not evidence of factual quality.
