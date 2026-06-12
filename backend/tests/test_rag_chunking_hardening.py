"""
Pure-logic tests for RAG chunking/upsert hardening.

Covers the chunk_size -5 incident: a junk persisted setting made chunk_text
emit one chunk per sentence (30K+ chunks for one file), which then blew
ChromaDB's ~5461-record single-upsert cap and 500'd the reindex endpoint.
No live ChromaDB or Ollama required.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if importlib.util.find_spec("chromadb") is None:
    pytest.skip("chromadb not installed", allow_module_level=True)

import rag  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


SAMPLE = " ".join(f"This is sentence number {i} in the sample document." for i in range(200))


def test_chunk_text_negative_chunk_size_does_not_explode(monkeypatch):
    monkeypatch.setattr(rag, "CHUNK_SIZE", -5)
    monkeypatch.setattr(rag, "CHUNK_OVERLAP", 50)
    chunks = rag.chunk_text(SAMPLE, "doc.txt")
    # The 400-char floor keeps chunks multi-sentence instead of one chunk per
    # sentence (200 sentences would have produced ~200 single-sentence chunks).
    assert 0 < len(chunks) < 100
    assert all(len(c["text"]) > 100 for c in chunks[:-1])


def test_chunk_text_normal_chunk_size_unchanged(monkeypatch):
    monkeypatch.setattr(rag, "CHUNK_SIZE", 500)
    monkeypatch.setattr(rag, "CHUNK_OVERLAP", 50)
    chunks = rag.chunk_text(SAMPLE, "doc.txt")
    assert chunks
    target_chars = 500 * rag.CHARS_PER_TOKEN
    assert all(len(c["text"]) <= target_chars + 600 for c in chunks)


def test_index_file_upsert_is_sliced_below_chroma_cap(monkeypatch, tmp_path):
    n_chunks = 12000  # > 2x the 5000-record slice

    monkeypatch.setattr(rag, "chunk_document", lambda text, filename: [
        {"text": f"chunk {i}", "filename": filename, "chunk_index": i} for i in range(n_chunks)
    ])

    async def fake_embed(texts):
        return [[0.0, 1.0] for _ in texts]

    async def fake_remove(kb_id, filename):
        return None

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    monkeypatch.setattr(rag, "remove_file", fake_remove)

    upsert_sizes = []

    class _FakeCollection:
        def upsert(self, ids, documents, metadatas, embeddings):
            assert len(ids) == len(documents) == len(metadatas) == len(embeddings)
            upsert_sizes.append(len(ids))

    monkeypatch.setattr(rag, "_get_collection", lambda kb_id: _FakeCollection())

    f = tmp_path / "big.txt"
    f.write_text("content")
    result = _run(rag.index_file("kb-test", "big.txt", str(f)))

    assert result["chunks"] == n_chunks
    assert sum(upsert_sizes) == n_chunks
    assert all(s <= rag.CHROMA_UPSERT_BATCH for s in upsert_sizes)
    assert len(upsert_sizes) == 3  # 5000 + 5000 + 2000
