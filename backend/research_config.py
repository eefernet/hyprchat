"""Research report templates and depth budgets."""
from __future__ import annotations

import config


REPORT_TEMPLATES = [
    {
        "id": "analyst",
        "label": "Analyst Report",
        "description": "Executive-grade synthesis with evidence, uncertainty, implications, and recommendations.",
        "default_depth": 4,
        "sections": ["Executive Summary", "Key Findings", "Evidence Review", "Conflicts and Uncertainty", "Implications", "Recommendations", "Sources"],
    },
    {
        "id": "academic",
        "label": "Academic Review",
        "description": "Literature-review style report with methodology, themes, limitations, and bibliography.",
        "default_depth": 5,
        "sections": ["Abstract", "Methodology", "Background", "Literature Themes", "Evidence Synthesis", "Limitations", "Bibliography"],
    },
    {
        "id": "decision",
        "label": "Decision Brief",
        "description": "Options, tradeoffs, risks, and a recommended course of action.",
        "default_depth": 3,
        "sections": ["Decision Context", "Options", "Evaluation Criteria", "Tradeoffs", "Risks", "Recommendation", "Next Steps"],
    },
    {
        "id": "market",
        "label": "Market Intelligence",
        "description": "Competitive landscape, market signals, risks, and strategic opportunities.",
        "default_depth": 4,
        "sections": ["Executive Brief", "Market Landscape", "Competitors", "Demand Signals", "Risks", "Opportunities", "Strategic Readout"],
    },
    {
        "id": "technical",
        "label": "Technical Deep Dive",
        "description": "Architecture, implementation details, benchmarks, failure modes, and practical guidance.",
        "default_depth": 4,
        "sections": ["Overview", "Architecture", "Implementation Details", "Benchmarks and Data", "Failure Modes", "Best Practices", "References"],
    },
    {
        "id": "timeline",
        "label": "Investigative Timeline",
        "description": "Chronological dossier with actors, evidence, contradictions, and open questions.",
        "default_depth": 5,
        "sections": ["Briefing", "Timeline", "Key Actors", "Evidence Trail", "Contradictions", "Open Questions", "Appendix"],
    },
    {
        "id": "digest",
        "label": "Source Digest",
        "description": "Source-by-source summary for fast review, triage, and follow-up research.",
        "default_depth": 2,
        "sections": ["Overview", "Highest-Value Sources", "Source Summaries", "Patterns", "Gaps", "Follow-up Queries"],
    },
]
REPORT_TEMPLATE_MAP = {t["id"]: t for t in REPORT_TEMPLATES}


RESEARCH_DEPTH_BUDGETS = {
    1: {"queries": 6, "results_per_query": 8, "target_sources": 12, "page_reads": 5, "source_briefs": 18, "page_extracts": 7, "findings": 10, "context_chars": 52000},
    2: {"queries": 9, "results_per_query": 10, "target_sources": 22, "page_reads": 9, "source_briefs": 28, "page_extracts": 10, "findings": 12, "context_chars": 64000},
    3: {"queries": 13, "results_per_query": 12, "target_sources": 34, "page_reads": 14, "source_briefs": 42, "page_extracts": 14, "findings": 16, "context_chars": 76000},
    4: {"queries": 18, "results_per_query": 14, "target_sources": 48, "page_reads": 20, "source_briefs": 60, "page_extracts": 18, "findings": 20, "context_chars": 90000},
    5: {"queries": 24, "results_per_query": 16, "target_sources": 65, "page_reads": 28, "source_briefs": 80, "page_extracts": 24, "findings": 24, "context_chars": 110000},
}


def research_depth_budget(depth: int) -> dict:
    return RESEARCH_DEPTH_BUDGETS.get(max(1, min(5, int(depth or 3))), RESEARCH_DEPTH_BUDGETS[3])


def research_num_ctx() -> int:
    """Context window for research LLM calls (settings/env: research_num_ctx)."""
    n = int(getattr(config, "RESEARCH_NUM_CTX", 0) or getattr(config, "DEFAULT_NUM_CTX", 0) or 16384)
    return n if n > 0 else 16384


def effective_context_chars(budget: dict, *, reserve_tokens: int = 5200, overhead_chars: int = 24000) -> int:
    """Clamp the evidence-context size to the configured context window."""
    avail = (research_num_ctx() - reserve_tokens) * 3 - overhead_chars
    return max(8000, min(int(budget.get("context_chars", 52000)), avail))


_RESEARCH_DEPTH_BUDGETS = RESEARCH_DEPTH_BUDGETS
_research_depth_budget = research_depth_budget
_research_num_ctx = research_num_ctx
_effective_context_chars = effective_context_chars
