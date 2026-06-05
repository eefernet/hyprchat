"""
Pure-logic tests for first-class Deep Research report query construction.
"""
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import research  # noqa: E402


def test_long_report_topic_preserves_search_budget_diversity():
    budget = research._research_depth_budget(4)
    long_topic = (
        "Assess whether small businesses should adopt local/private AI assistants instead "
        "of cloud AI assistants for internal operations in 2026. Compare local-first stacks, "
        "cloud-hosted assistants, and hybrid architectures across privacy, security, cost, "
        "reliability, maintenance burden, model quality, latency, compliance, employee adoption, "
        "RAG document access, and tool automation. Include executive recommendation, cost model, "
        "security matrix, implementation roadmap, failure modes, and practical recommendations."
    )

    queries = research._make_report_search_queries(long_topic, "", [], "analyst", budget)

    assert len(queries) == budget["queries"]
    assert len({q.lower() for q in queries}) == budget["queries"]
    assert any("expert analysis" in q.lower() for q in queries)
    assert any("latest developments" in q.lower() for q in queries)
    assert all(len(q) <= 220 for q in queries)
