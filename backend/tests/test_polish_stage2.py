"""Polishing pass, stage 2 (2026-11-18): panels and chat. One test per finding. Offline."""
import asyncio
import json

import pytest

import database as db
import workspace_service as ws


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "stage2.db"))
    asyncio.run(db.init_db())
    return tmp_path


class _Stream:
    def __init__(self, lines, status=200):
        self.lines, self.status_code = lines, status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def aiter_lines(self):
        for line in self.lines:
            yield line
    async def aread(self): return b"refused"


class _Http:
    """Ollama stand-in: /api/pull succeeds, /api/create answers what the test says."""
    def __init__(self, create_lines, create_status=200):
        self.create_lines, self.create_status, self.calls = create_lines, create_status, []
    def stream(self, method, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("/api/pull"):
            return _Stream(['{"status":"pulling manifest"}', '{"status":"success"}'])
        return _Stream(self.create_lines, self.create_status)


class _Request:
    def __init__(self, body): self.body = body
    async def json(self): return self.body


async def _download(http, monkeypatch, resolved="exact-file"):
    import hf
    seen = {}
    async def resolve(client, repo, primary):
        seen["asked"] = primary
        return resolved, {}
    monkeypatch.setattr(hf.hf_compat, "resolve_tag", resolve)
    response = await hf.hf_download(http, _Request({"repo_id": "org/repo", "filenames": ["model-noMTP-Q4_K_M.gguf"], "model_name": "my-model"}))
    events = [json.loads(chunk[6:]) async for chunk in response.body_iterator if chunk.startswith("data: ")]
    return events, seen


def test_hf_download_pulls_the_exact_file_it_was_asked_for(monkeypatch):
    """`hf.py` used `asyncio.wait_for` without importing asyncio; the NameError was swallowed and every
    download fell back to the ambiguous quant tag (the wrong-file bug the lookup exists to prevent)."""
    async def run():
        http = _Http(['{"status":"success"}'])
        events, seen = await _download(http, monkeypatch)
        assert seen["asked"] == "model-noMTP-Q4_K_M.gguf"
        assert http.calls[0] == ("pull", {"name": "hf.co/org/repo:exact-file", "stream": True})
        # "Ready" is said once, after the alias exists; the pull's own success line is not forwarded.
        assert [e["status"] for e in events].count("done") == 1 and events[-1]["status"] == "done"
    asyncio.run(run())


def test_hf_download_does_not_say_ready_when_the_name_could_not_be_created(monkeypatch):
    async def run():
        events, _ = await _download(_Http(['{"error":"invalid model name"}']), monkeypatch)
        assert events[-1]["status"] == "error" and "invalid model name" in events[-1]["message"]
        assert "done" not in [e["status"] for e in events]
        events, _ = await _download(_Http([], create_status=400), monkeypatch)
        assert events[-1]["status"] == "error" and "done" not in [e["status"] for e in events]
    asyncio.run(run())


def test_deleting_a_workspace_takes_its_document_store_with_it(fresh, monkeypatch):
    """The store outlived the workspace, lost its "internal" marker with it, and appeared in the Knowledge
    Bases list full of files named by random ids."""
    main = pytest.importorskip("main")
    monkeypatch.setattr(main.config, "KB_DIR", str(fresh / "kb"))
    async def run():
        await db.create_workspace("w", "Home Lab Rebuild")
        await db.create_kb("mine", "Python Reference")
        store = await ws.document_store("w")
        assert await ws.document_store_id("w") == store and await ws.document_store_id("missing") == ""
        await main.delete_workspace_ep("w")
        assert [k["id"] for k in await db.get_kbs()] == ["mine"]
        await main.delete_workspace_ep("w")                     # already gone: no error
    asyncio.run(run())


def test_workspace_picker_does_not_offer_another_workspaces_document_store(fresh):
    async def run():
        await db.create_workspace("a", "Alpha"); await db.create_workspace("b", "Beta")
        await db.create_kb("kb", "Shared reference")
        await ws.document_store("b")
        names = [item["title"] for item in (await ws.picker("a", "knowledge"))["items"]]
        assert names == ["Shared reference"]
    asyncio.run(run())


def test_a_finished_report_counts_as_ready_in_a_workspace(fresh):
    """Reports finish as `complete`; the readiness check listed only names reports never use."""
    async def run():
        await db.create_workspace("a", "Alpha")
        await db.create_research_report("r", query="q", title="Report")
        await db.update_research_report("r", status="complete", report_markdown="# Report\n\nBody")
        await ws.link_source("a", "research", "r")
        rows = (await ws.list_sources("a", kind="research"))
        items = rows["items"] if isinstance(rows, dict) else rows
        assert items and items[0]["readiness"] == "ready" and items[0]["can_use"]
    asyncio.run(run())


def test_settings_saves_do_not_overwrite_each_other(fresh, monkeypatch):
    """Both writers read the file, awaited, and wrote the whole file back."""
    import config
    from routes import settings as routes
    order = []
    async def slow(body):
        order.append(("start", body["n"])); await asyncio.sleep(0.02); order.append(("end", body["n"]))
        return {}
    monkeypatch.setattr(routes, "_update_app_settings", slow)
    async def run():
        await asyncio.gather(routes.update_app_settings({"n": 1}), routes.update_app_settings({"n": 2}))
        assert order == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
        assert not config.SETTINGS_LOCK.locked()
    asyncio.run(run())


def test_events_after_a_reply_are_marked_with_that_reply(fresh):
    """Memory suggestions finish up to a minute after their reply; a queued next reply took their events."""
    import events as bus_module
    async def run():
        bus = bus_module.EventBus()
        queue = await bus.subscribe("c")
        async def late():
            await bus.emit("c", "tool_start", {"tool": "memory"})
        await asyncio.create_task(bus_module.after_reply(41, late()))
        await bus.emit("c", "tool_start", {"tool": "execute_code"})        # the reply streaming now
        first, second = await queue.get(), await queue.get()
        assert first["reply_id"] == 41 and "reply_id" not in second
    asyncio.run(run())


def test_message_exists_is_scoped_to_its_owner(fresh):
    async def run():
        await db.create_conversation(id="c", title="C")
        mid = await db.add_message("c", "assistant", "text")
        assert await db.message_exists(mid) and not await db.message_exists(mid + 99)
        other = await db.create_user("Other"); token = db.set_current_user_id(other["id"])
        try:
            assert not await db.message_exists(mid)
        finally:
            db.reset_current_user_id(token)
    asyncio.run(run())
