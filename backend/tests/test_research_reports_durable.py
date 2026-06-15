import asyncio
import importlib
import sys
from datetime import datetime
from pathlib import Path

import pytest

from .optional_deps import HAS_AIOSQLITE, HAS_CHROMADB, HAS_FASTAPI, install_rag_stub


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if not HAS_AIOSQLITE:
    pytest.skip("aiosqlite not installed", allow_module_level=True)

import database as db  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _import_main_for_research_routes(monkeypatch):
    if not HAS_FASTAPI:
        pytest.skip("fastapi not installed")
    if not HAS_CHROMADB:
        install_rag_stub(monkeypatch)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


async def _source_row_count(report_id: str) -> int:
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT COUNT(*) AS n FROM research_sources WHERE report_id=?",
            (report_id,),
        )
        return int(rows[0]["n"])
    finally:
        await conn.close()


def test_research_report_database_round_trip_sources_events_and_delete_cleanup(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())

    _run(db.create_research_report(
        "research-db",
        title="Local AI Privacy",
        query="local AI privacy",
        focus="small business",
        report_type="technical",
        depth=4,
        model="research-model",
        planner_model="planner-model",
        auditor_model="auditor-model",
        kb_ids=["kb-1"],
        inputs=[{"name": "notes.md", "content": "private notes"}],
        status="running",
    ))
    _run(db.update_research_report(
        "research-db",
        status="complete",
        report_markdown="# Local AI Privacy\n\nBody [S1].",
        summary="Body summary",
        sources=[{"index": 1, "title": "JSON source", "url": "https://old.example"}],
        findings=[{"finding_id": 1, "claim": "Local keeps data nearby."}],
        metrics={"source_count": 1, "pages_read": 2, "elapsed": 3.5},
        completed_at=datetime.utcnow().isoformat(),
    ))

    _run(db.replace_research_sources("research-db", [
        {
            "index": 1,
            "title": "Old source",
            "url": "https://old.example",
            "snippet": "old",
            "tier": 2,
            "type": "web",
        },
        {
            "index": 2,
            "title": "Second source",
            "url": "https://second.example",
            "snippet": "second",
            "tier": 1,
            "type": "web",
        },
    ]))
    _run(db.replace_research_sources("research-db", [{
        "index": 7,
        "title": "Official docs",
        "url": "https://docs.example/current",
        "snippet": "current docs",
        "tier": 0,
        "type": "official",
        "credibility_score": 91,
        "thumbnail": "https://docs.example/og.png",
        "metadata": {"publisher": "Docs Team"},
    }]))

    for ev_type in ("research_phase", "research_error", "research_done"):
        _run(db.append_research_event("research-db", {
            "type": ev_type,
            "data": {"status": ev_type},
        }))

    report = _run(db.get_research_report("research-db"))
    assert report["query"] == "local AI privacy"
    assert report["focus"] == "small business"
    assert report["report_type"] == "technical"
    assert report["inputs"] == [{"name": "notes.md", "content": "private notes"}]
    assert report["kb_ids"] == ["kb-1"]
    assert report["sources"][0]["title"] == "Official docs"
    assert report["sources"][0]["source_index"] == 7
    assert report["sources"][0]["type"] == "official"
    assert report["sources"][0]["credibility_score"] == 91
    assert report["sources"][0]["metadata"]["publisher"] == "Docs Team"
    assert _run(_source_row_count("research-db")) == 1

    events = report["events_log"]
    assert [e["type"] for e in events] == ["research_phase", "research_error", "research_done"]
    assert all(e.get("ts") for e in events)

    listed = _run(db.list_research_reports(query="privacy"))
    assert [r["id"] for r in listed] == ["research-db"]
    assert listed[0]["source_count"] == 1

    _run(db.create_workspace("ws-research", "Research Workspace"))
    _run(db.add_research_report_to_workspace("ws-research", "research-db"))
    workspace = _run(db.get_workspace("ws-research"))
    assert [r["id"] for r in workspace["reports"]] == ["research-db"]

    _run(db.delete_research_report("research-db"))
    assert _run(db.get_research_report("research-db")) is None
    assert _run(_source_row_count("research-db")) == 0
    assert _run(db.get_workspace("ws-research"))["reports"] == []


def test_update_research_report_unless_status_protects_cancelled_rows(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())

    _run(db.create_research_report("research-race", query="race", status="queued"))
    _run(db.update_research_report("research-race", status="cancelled", error="Cancelled by user"))

    # A late "complete" from the runner must not resurrect a cancelled report.
    wrote = _run(db.update_research_report(
        "research-race", status="complete", report_markdown="# Late",
        unless_status="cancelled",
    ))
    assert wrote is False
    row = _run(db.get_research_report("research-race"))
    assert row["status"] == "cancelled"
    assert not row.get("report_markdown")

    # Unguarded updates still work and report success.
    assert _run(db.update_research_report("research-race", summary="s")) is True
    # unless_status on a non-matching status applies normally.
    assert _run(db.update_research_report(
        "research-race", status="failed", unless_status="complete",
    )) is True


def test_research_token_events_are_ephemeral(tmp_path):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    import research

    _run(db.create_research_report("research-tokens", query="tokens", status="running"))

    emitted = []

    class _FakeEvents:
        async def emit(self, channel, ev_type, data):
            emitted.append((channel, ev_type))

    fake_events = _FakeEvents()
    _run(research._emit_report_event(fake_events, "research-tokens", "research_phase", {"phase": "planning"}))
    _run(research._emit_report_event(fake_events, "research-tokens", "research_token", {"content": "chunk"}))
    _run(research._emit_report_event(fake_events, "research-tokens", "research_done", {"status": "complete"}))

    # Live SSE got all three; persistence skipped the token chunk.
    assert [e[1] for e in emitted] == ["research_phase", "research_token", "research_done"]
    report = _run(db.get_research_report("research-tokens"))
    assert [e["type"] for e in report["events_log"]] == ["research_phase", "research_done"]

    # Legacy rows that already persisted token events get them filtered on read.
    _run(db.append_research_event("research-tokens", {
        "type": "research_token", "data": {"content": "legacy"},
    }))
    report = _run(db.get_research_report("research-tokens"))
    assert "research_token" not in [e["type"] for e in report["events_log"]]


def test_research_report_routes_cancel_and_rerun_preserve_original_fields(tmp_path, monkeypatch):
    db.DATABASE_PATH = str(tmp_path / "hyprchat.db")
    _run(db.init_db())
    main = _import_main_for_research_routes(monkeypatch)

    _run(db.create_research_report(
        "research-cancel",
        query="cancel me",
        title="Cancel Me",
        status="queued",
    ))
    cancel_result = _run(main.cancel_research_report("research-cancel"))
    cancelled = _run(db.get_research_report("research-cancel"))
    assert cancel_result["marked"] is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["events_log"][-1]["type"] == "research_error"
    assert cancelled["events_log"][-1]["data"]["status"] == "cancelled"

    _run(db.create_research_report(
        "research-old",
        title="Original title",
        query="durable research",
        focus="edge devices",
        report_type="technical",
        depth=5,
        model="research-model",
        planner_model="planner-model",
        auditor_model="auditor-model",
        kb_ids=["kb-a", "kb-b"],
        inputs=[{"name": "input.txt", "content": "source notes"}],
        status="complete",
    ))
    _run(db.update_research_report(
        "research-old",
        report_markdown="# Old report",
        summary="Old summary",
        completed_at=datetime.utcnow().isoformat(),
    ))

    async def fake_run_research_report(*args, **_kwargs):
        report_id = args[4]
        await db.update_research_report(
            report_id,
            status="complete",
            report_markdown="# Rerun report",
            summary="Rerun summary",
            completed_at=datetime.utcnow().isoformat(),
        )
        await db.append_research_event(report_id, {
            "type": "research_done",
            "data": {"status": "complete"},
        })

    monkeypatch.setattr(main, "run_research_report", fake_run_research_report)

    async def scenario():
        rerun = await main.rerun_research_report("research-old")
        await asyncio.sleep(0)
        return rerun, await db.get_research_report(rerun["id"]), await db.get_research_report("research-old")

    rerun_response, rerun_report, old_report = _run(scenario())

    assert rerun_response["id"] != "research-old"
    assert rerun_report["query"] == "durable research"
    assert rerun_report["focus"] == "edge devices"
    assert rerun_report["report_type"] == "technical"
    assert rerun_report["depth"] == 5
    assert rerun_report["model"] == "research-model"
    assert rerun_report["planner_model"] == "planner-model"
    assert rerun_report["auditor_model"] == "auditor-model"
    assert rerun_report["kb_ids"] == ["kb-a", "kb-b"]
    assert rerun_report["inputs"] == [{"name": "input.txt", "content": "source notes"}]
    assert rerun_report["status"] == "complete"
    assert rerun_report["events_log"][-1]["type"] == "research_done"
    assert old_report["report_markdown"] == "# Old report"
    assert old_report["summary"] == "Old summary"
