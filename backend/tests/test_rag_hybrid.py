"""
Hybrid RAG retrieval tests — FTS5 keyword leg + vector leg + RRF fusion,
via the POST /api/knowledge-bases/query probe endpoint.
"""
import io
import time

import pytest

KEYWORD_DOC = (
    "Inventory parts list.\n"
    "The ZX-9000 flux capacitor uses part number QQ-417 and ships with a "
    "calibration spanner. Stock location: warehouse bay 7, shelf C.\n"
    "Replacement gaskets for the ZX-9000 use part number QQ-418.\n"
)

SEMANTIC_DOC = (
    "Company travel policy.\n"
    "Employees may book economy class flights for trips under six hours. "
    "Hotel reimbursement is capped at 180 dollars per night in major cities. "
    "Meals while travelling are reimbursed with receipts up to 60 dollars per day.\n"
)


def _upload_and_wait(client, kb_id, filename, text, timeout=60):
    r = client.post(
        f"/api/knowledge-bases/{kb_id}/files",
        files={"file": (filename, io.BytesIO(text.encode()), "text/plain")},
    )
    assert r.status_code == 200, r.text
    file_id = r.json()["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/knowledge-bases/{kb_id}/files/{filename}/status").json()
        if s.get("status") in ("done", "error", "unknown"):
            assert s.get("status") != "error", s
            return file_id
        time.sleep(1)
    pytest.fail(f"Indexing of {filename} did not finish in {timeout}s")


@pytest.fixture(scope="module")
def hybrid_kb(client):
    r = client.post("/api/knowledge-bases", json={"name": "Hybrid Test KB", "description": "hybrid rag tests"})
    assert r.status_code == 200
    kb = r.json()
    _upload_and_wait(client, kb["id"], "parts.txt", KEYWORD_DOC)
    _upload_and_wait(client, kb["id"], "travel.txt", SEMANTIC_DOC)
    yield kb
    client.delete(f"/api/knowledge-bases/{kb['id']}")


def _query(client, kb_id, q, top_k=6):
    r = client.post("/api/knowledge-bases/query", json={"kb_ids": [kb_id], "query": q, "top_k": top_k})
    assert r.status_code == 200, r.text
    return r.json()["chunks"]


def test_exact_keyword_hits_bm25_leg(client, hybrid_kb):
    """A part number is exactly the query class pure cosine tends to miss."""
    chunks = _query(client, hybrid_kb["id"], "QQ-417 part number")
    assert chunks, "expected results for exact part-number query"
    top_files = [c["filename"] for c in chunks[:3]]
    assert "parts.txt" in top_files, f"keyword doc not in top-3: {top_files}"


def test_paraphrase_hits_vector_leg(client, hybrid_kb):
    chunks = _query(client, hybrid_kb["id"], "how much can I spend on accommodation when on a work trip?")
    assert chunks, "expected results for paraphrase query"
    top_files = [c["filename"] for c in chunks[:3]]
    assert "travel.txt" in top_files, f"semantic doc not in top-3: {top_files}"


def test_fts_metacharacters_do_not_500(client, hybrid_kb):
    for q in ['what is the "part number"?', "ZX-9000?!", 'a AND b OR (c)', "col:value*"]:
        r = client.post("/api/knowledge-bases/query", json={"kb_ids": [hybrid_kb["id"]], "query": q})
        assert r.status_code == 200, f"{q!r} → HTTP {r.status_code}: {r.text[:200]}"


def test_result_shape(client, hybrid_kb):
    chunks = _query(client, hybrid_kb["id"], "flux capacitor calibration")
    assert chunks
    for c in chunks:
        assert {"text", "filename", "kb_id", "chunk_index", "score"} <= set(c.keys())
        assert 0 <= c["score"] <= 1.001


def test_delete_file_removes_from_both_stores(client, hybrid_kb):
    fid = _upload_and_wait(client, hybrid_kb["id"], "ephemeral.txt",
                           "The WONKAVATOR-77 elevator uses part number ZZ-999.")
    chunks = _query(client, hybrid_kb["id"], "WONKAVATOR-77 ZZ-999")
    assert any(c["filename"] == "ephemeral.txt" for c in chunks)
    r = client.delete(f"/api/knowledge-bases/files/{fid}")
    assert r.status_code == 200
    time.sleep(1)
    chunks = _query(client, hybrid_kb["id"], "WONKAVATOR-77 ZZ-999")
    assert not any(c["filename"] == "ephemeral.txt" for c in chunks), "deleted file still retrievable"


def test_reindex_keeps_hybrid_working(client, hybrid_kb):
    r = client.post(f"/api/knowledge-bases/{hybrid_kb['id']}/reindex")
    assert r.status_code == 200
    chunks = _query(client, hybrid_kb["id"], "QQ-417")
    assert any(c["filename"] == "parts.txt" for c in chunks[:3])


def test_query_validation(client, hybrid_kb):
    assert client.post("/api/knowledge-bases/query", json={"kb_ids": [], "query": "x"}).status_code == 400
    assert client.post("/api/knowledge-bases/query", json={"kb_ids": [hybrid_kb["id"]], "query": ""}).status_code == 400
    assert client.post("/api/knowledge-bases/query", json={"kb_ids": ["kb-nonexistent"], "query": "x"}).status_code == 404
