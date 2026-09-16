"""Chat uploads: actual route logic with an in-memory chunk receiver."""
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from .optional_deps import load_route_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")
from fastapi import HTTPException, UploadFile


class ChunkReceiver:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.fail_chunk = None

    async def post(self, url, *, files, data, **kwargs):
        self.calls.append(data.copy())
        if len(self.calls) == self.fail_chunk:
            return SimpleNamespace(status_code=503, text="unavailable")
        path = data["path"]
        before = b"" if data["truncate"] == "true" else self.files[path]
        self.files[path] = before + files["file"][1]
        await asyncio.sleep(0)  # overlapping multi-chunk requests interleave
        return SimpleNamespace(status_code=200)


@pytest.fixture
def route(monkeypatch):
    route = load_route_module(monkeypatch, "chat_files")
    receiver = ChunkReceiver()
    warms = []

    def track(coro):
        warms.append(True)
        coro.close()

    async def owned(conv_id):
        return {"id": conv_id}

    monkeypatch.setattr(route.db, "get_conversation", owned)
    monkeypatch.setattr(route, "_CHUNK", 3)
    monkeypatch.setattr(route, "route_context", lambda: SimpleNamespace(http=receiver, track_bg=track))
    return route, receiver, warms


def upload(name, data):
    buffer = io.BytesIO(data)
    buffer._rolled = False  # model Starlette's in-memory SpooledTemporaryFile
    return UploadFile(buffer, filename=name)


@pytest.mark.parametrize("names", [("same.csv", "same.csv"), ("report?.xlsx", "report*.xlsx")])
def test_overlapping_uploads_preserve_both_files(route, names):
    mod, receiver, warms = route
    contents = (b"\x00\xffabcdef", b"\xfe\x00ghijkl")

    async def run():
        return await asyncio.gather(*[
            mod.stage_chat_file(upload(name, data), "conv-1")
            for name, data in zip(names, contents)
        ])

    results = asyncio.run(run())
    assert results[0]["sandbox_path"] != results[1]["sandbox_path"]
    for result, data in zip(results, contents):
        assert receiver.files[result["sandbox_path"]] == data
        assert result["size_bytes"] == len(data)
        assert set(result) == {"name", "sandbox_path", "size_bytes"}
        assert result["sandbox_path"].startswith("/root/chat_files/conv-1/")
    assert len(warms) == 2


def test_repeat_upload_does_not_replace_history(route):
    mod, receiver, _ = route

    async def run():
        first = await mod.stage_chat_file(upload("data.json", b"first"), "conv-1")
        second = await mod.stage_chat_file(upload("data.json", b"second"), "conv-1")
        return first, second

    first, second = asyncio.run(run())
    assert receiver.files[first["sandbox_path"]] == b"first"
    assert receiver.files[second["sandbox_path"]] == b"second"


def test_safe_name_keeps_extension_and_cannot_traverse(route):
    mod, _, _ = route
    assert mod.safe_filename("../../..\\file.csv") == "file.csv"
    assert mod.safe_filename("../...") == "file"
    name = mod.safe_filename("x" * 200 + ".xlsx")
    assert len(name) == 120
    assert name.endswith(".xlsx")


def test_size_check_is_bounded_and_happens_before_network(route, monkeypatch):
    mod, receiver, _ = route
    monkeypatch.setattr(mod.config, "MAX_UPLOAD_SIZE_MB", 1)
    file = upload("big.csv", b"x" * (2 * 1024 * 1024))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.stage_chat_file(file, "conv-1"))
    assert exc.value.status_code == 413
    assert file.file.tell() == 1024 * 1024 + 1
    assert receiver.calls == []


def test_exact_size_limit_is_accepted(route, monkeypatch):
    mod, receiver, _ = route
    monkeypatch.setattr(mod.config, "MAX_UPLOAD_SIZE_MB", 1)
    monkeypatch.setattr(mod, "_CHUNK", 1024 * 1024)
    data = b"x" * (1024 * 1024)
    result = asyncio.run(mod.stage_chat_file(upload("data.csv", data), "conv-1"))
    assert receiver.files[result["sandbox_path"]] == data


@pytest.mark.parametrize("conv_id,content,status", [("../bad", b"data", 400), ("conv-1", b"", 400)])
def test_invalid_input_never_stages(route, conv_id, content, status):
    mod, receiver, _ = route
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.stage_chat_file(upload("data.csv", content), conv_id))
    assert exc.value.status_code == status
    assert receiver.calls == []


def test_nonowned_conversation_is_rejected_before_read(route, monkeypatch):
    mod, receiver, _ = route

    async def missing(_id):
        return None

    monkeypatch.setattr(mod.db, "get_conversation", missing)
    file = upload("data.csv", b"private")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.stage_chat_file(file, "foreign-conv"))
    assert exc.value.status_code == 404
    assert file.file.tell() == 0
    assert receiver.calls == []


def test_failed_chunk_returns_error_without_warmup(route):
    mod, receiver, warms = route
    receiver.fail_chunk = 2
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.stage_chat_file(upload("data.csv", b"0123456789"), "conv-1"))
    assert exc.value.status_code == 502
    assert len(receiver.calls) == 2
    assert warms == []
