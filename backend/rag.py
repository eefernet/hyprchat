"""
RAG pipeline — ChromaDB + Ollama embeddings.
Chunks documents, embeds via Ollama, stores in ChromaDB, retrieves relevant chunks.
"""
import hashlib
import os
import re
from typing import Optional

import chromadb
import httpx

import config
import database as db

# ── ChromaDB persistent client ──
_chroma_client: Optional[chromadb.ClientAPI] = None
CHROMA_DIR = os.path.join(os.path.dirname(config.KB_DIR), "chroma_db")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# Chunking settings
CHUNK_SIZE = 500       # target tokens per chunk (~2000 chars)
CHUNK_OVERLAP = 50     # overlap tokens between chunks
CHROMA_UPSERT_BATCH = 5000  # Chroma rejects single upserts above ~5461 records
CHARS_PER_TOKEN = 4    # rough estimate


def get_chroma() -> chromadb.ClientAPI:
    """Get or create the persistent ChromaDB client."""
    global _chroma_client
    if _chroma_client is None:
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
        print(f"[RAG] ChromaDB initialized at {CHROMA_DIR}")
    return _chroma_client


def _collection_name(kb_id: str) -> str:
    """ChromaDB collection name for a KB. Must be 3-63 chars, alphanumeric + hyphens."""
    # kb_id is like "kb-a1b2c3d4e5f6" — already valid
    return kb_id


def _get_collection(kb_id: str):
    """Get or create a ChromaDB collection for a KB."""
    client = get_chroma()
    return client.get_or_create_collection(
        name=_collection_name(kb_id),
        metadata={"hnsw:space": "cosine"},
    )


# ── Document Parsing ──

def parse_file(filepath: str, filename: str) -> str:
    """Extract text content from a file. Supports txt, md, py, pdf, etc."""
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".pdf":
        return _parse_pdf(filepath)
    else:
        # Plain text, markdown, code, etc.
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            print(f"[RAG] Failed to read {filepath}: {e}")
            return ""


def _parse_pdf(filepath: str) -> str:
    """Extract text from PDF using pypdf."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"[Page {i+1}]\n{text}")
        return "\n\n".join(pages)
    except ImportError:
        print("[RAG] pypdf not installed — reading PDF as raw text")
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    except Exception as e:
        print(f"[RAG] PDF parse error for {filepath}: {e}")
        return ""


# ── Chunking ──

def chunk_text(text: str, filename: str = "") -> list[dict]:
    """Split text into overlapping chunks with metadata.

    Uses sentence-aware splitting: tries to break at sentence boundaries
    within the target chunk size.
    """
    if not text.strip():
        return []

    # Floor guards: a junk CHUNK_SIZE (e.g. negative from a bad settings file)
    # would otherwise make every sentence its own chunk and explode the index.
    target_chars = max(400, CHUNK_SIZE * CHARS_PER_TOKEN)
    overlap_chars = max(0, CHUNK_OVERLAP * CHARS_PER_TOKEN)

    # Split into sentences (keep delimiters)
    sentences = re.split(r'(?<=[.!?\n])\s+', text)
    sentences = [s for s in sentences if s.strip()]

    chunks = []
    current = ""
    chunk_idx = 0

    for sentence in sentences:
        if len(current) + len(sentence) > target_chars and current:
            chunks.append({
                "text": current.strip(),
                "filename": filename,
                "chunk_index": chunk_idx,
            })
            chunk_idx += 1
            # Keep overlap from end of current chunk
            if overlap_chars > 0 and len(current) > overlap_chars:
                current = current[-overlap_chars:] + " " + sentence
            else:
                current = sentence
        else:
            current = (current + " " + sentence) if current else sentence

    # Last chunk
    if current.strip():
        chunks.append({
            "text": current.strip(),
            "filename": filename,
            "chunk_index": chunk_idx,
        })

    return chunks


def chunk_code(text: str, filename: str = "") -> list[dict]:
    """Split code files by functions/classes, falling back to line-based chunking."""
    ext = os.path.splitext(filename)[1].lower()

    # Try to split Python by top-level defs
    if ext in (".py", ".pyi"):
        blocks = re.split(r'\n(?=(?:def |class |async def ))', text)
        blocks = [b for b in blocks if b.strip()]
        if len(blocks) > 1:
            chunks = []
            for i, block in enumerate(blocks):
                if len(block.strip()) < 10:
                    continue
                chunks.append({
                    "text": block.strip(),
                    "filename": filename,
                    "chunk_index": i,
                })
            if chunks:
                return chunks

    # Try to split JS/TS by function/class declarations
    if ext in (".js", ".ts", ".jsx", ".tsx"):
        blocks = re.split(r'\n(?=(?:function |class |const \w+ = |export ))', text)
        blocks = [b for b in blocks if b.strip()]
        if len(blocks) > 1:
            chunks = []
            for i, block in enumerate(blocks):
                if len(block.strip()) < 10:
                    continue
                chunks.append({
                    "text": block.strip(),
                    "filename": filename,
                    "chunk_index": i,
                })
            if chunks:
                return chunks

    # Fallback: line-based chunking
    return chunk_text(text, filename)


def chunk_document(text: str, filename: str) -> list[dict]:
    """Route to appropriate chunker based on file type."""
    ext = os.path.splitext(filename)[1].lower()
    code_exts = {".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
                 ".rs", ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".sh", ".bash"}
    if ext in code_exts:
        return chunk_code(text, filename)
    return chunk_text(text, filename)


# ── Embedding via Ollama ──

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts using Ollama's embedding endpoint.

    Tries the modern batched /api/embed endpoint (one HTTP call per batch),
    falling back to per-prompt /api/embeddings for any batch that fails.
    Return shape is load-bearing: same length/order as input, None per
    failed item.
    """
    embeddings = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(texts), 64):
            batch = texts[i:i+64]
            try:
                resp = await client.post(
                    f"{config.OLLAMA_URL}/api/embed",
                    json={"model": EMBED_MODEL, "input": batch},
                )
                if resp.status_code == 200:
                    embs = resp.json().get("embeddings")
                    if isinstance(embs, list) and len(embs) == len(batch):
                        embeddings.extend(embs)
                        continue
                print(f"[RAG] Batch embed failed (HTTP {resp.status_code}), falling back to per-prompt")
            except Exception as e:
                print(f"[RAG] Batch embed error, falling back to per-prompt: {e}")
            for text in batch:
                try:
                    resp = await client.post(
                        f"{config.OLLAMA_URL}/api/embeddings",
                        json={"model": EMBED_MODEL, "prompt": text},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        embeddings.append(data["embedding"])
                    else:
                        print(f"[RAG] Embedding failed (HTTP {resp.status_code}): {resp.text[:200]}")
                        embeddings.append(None)
                except Exception as e:
                    print(f"[RAG] Embedding error: {e}")
                    embeddings.append(None)
    return embeddings


async def embed_single(text: str) -> Optional[list[float]]:
    """Embed a single text string."""
    results = await embed_texts([text])
    return results[0] if results else None


# ── Index & Query ──

async def index_file(kb_id: str, filename: str, filepath: str) -> dict:
    """Parse, chunk, embed, and store a file in ChromaDB.

    Returns stats about the indexing operation.
    """
    # Parse file content
    text = parse_file(filepath, filename)
    if not text.strip():
        return {"filename": filename, "chunks": 0, "error": "empty file"}

    # Chunk it
    chunks = chunk_document(text, filename)
    if not chunks:
        return {"filename": filename, "chunks": 0, "error": "no chunks produced"}

    # Clear any prior chunks for this filename first. Chunk IDs are
    # md5(kb_id:filename:index); without this, re-indexing a SHORTER version of
    # a file leaves the old higher-index chunks orphaned and still retrievable.
    await remove_file(kb_id, filename)

    # Generate stable IDs based on kb_id + filename + chunk_index
    ids = []
    texts = []
    metadatas = []
    for chunk in chunks:
        chunk_id = hashlib.md5(
            f"{kb_id}:{filename}:{chunk['chunk_index']}".encode()
        ).hexdigest()
        ids.append(chunk_id)
        texts.append(chunk["text"])
        metadatas.append({
            "filename": filename,
            "chunk_index": chunk["chunk_index"],
            "kb_id": kb_id,
            "char_count": len(chunk["text"]),
        })

    # Embed all chunks
    embeddings = await embed_texts(texts)

    # Filter out any failed embeddings
    valid = [(i, t, m, e) for i, t, m, e in zip(ids, texts, metadatas, embeddings) if e is not None]
    if not valid:
        return {"filename": filename, "chunks": len(chunks), "error": "all embeddings failed"}

    v_ids, v_texts, v_metas, v_embeds = zip(*valid)

    # Upsert into ChromaDB (idempotent — same IDs overwrite). Sliced because
    # Chroma hard-caps a single upsert at ~5461 records.
    collection = _get_collection(kb_id)
    for s in range(0, len(v_ids), CHROMA_UPSERT_BATCH):
        collection.upsert(
            ids=list(v_ids[s:s + CHROMA_UPSERT_BATCH]),
            documents=list(v_texts[s:s + CHROMA_UPSERT_BATCH]),
            metadatas=list(v_metas[s:s + CHROMA_UPSERT_BATCH]),
            embeddings=list(v_embeds[s:s + CHROMA_UPSERT_BATCH]),
        )

    # Mirror into the SQLite FTS5 keyword index (hybrid retrieval). FTS failure
    # must never fail indexing — vector search still works without it.
    try:
        await db.kb_fts_replace_file(kb_id, filename, [
            {"chunk_id": cid, "text": txt, "chunk_index": meta["chunk_index"]}
            for cid, txt, meta in zip(v_ids, v_texts, v_metas)
        ])
    except Exception as e:
        print(f"[RAG] FTS index error for {filename} in {kb_id}: {e}")

    print(f"[RAG] Indexed {filename} → {len(v_ids)} chunks in {kb_id}")
    return {"filename": filename, "chunks": len(v_ids), "total_chars": sum(len(t) for t in v_texts)}


async def remove_file(kb_id: str, filename: str):
    """Remove all chunks for a file from ChromaDB."""
    try:
        collection = _get_collection(kb_id)
        # Query for all chunks with this filename
        results = collection.get(where={"filename": filename})
        if results["ids"]:
            collection.delete(ids=results["ids"])
            print(f"[RAG] Removed {len(results['ids'])} chunks for {filename} from {kb_id}")
    except Exception as e:
        print(f"[RAG] Error removing {filename} from {kb_id}: {e}")
    try:
        await db.kb_fts_remove_file(kb_id, filename)
    except Exception as e:
        print(f"[RAG] FTS remove error for {filename} in {kb_id}: {e}")


async def delete_kb_index(kb_id: str):
    """Delete the entire ChromaDB collection for a KB."""
    try:
        client = get_chroma()
        client.delete_collection(_collection_name(kb_id))
        print(f"[RAG] Deleted collection for {kb_id}")
    except Exception as e:
        print(f"[RAG] Error deleting collection {kb_id}: {e}")
    try:
        await db.kb_fts_delete_kb(kb_id)
    except Exception as e:
        print(f"[RAG] FTS delete error for {kb_id}: {e}")


async def query(kb_ids: list[str], query_text: str, top_k: int = 6,
                prefer_filename_hints: list[str] | None = None) -> list[dict]:
    """Query multiple KBs and return the most relevant chunks.

    Args:
        kb_ids: List of KB IDs to search
        query_text: The user's question/query
        top_k: Number of chunks to return (total across all KBs)
        prefer_filename_hints: Optional list of filename substrings (e.g. ["java_", "javafx_",
            "spring_"]) used to bias retrieval toward language/framework-relevant files. Hints
            are case-insensitive substring matches on the chunk's source filename. When set,
            the function over-fetches (3× top_k) per KB, then partitions results into
            "matching hint" and "other" groups and returns matching first (filling any leftover
            slots with the best non-matching chunks). When unset, behavior is unchanged.

    Returns:
        List of dicts with keys: text, filename, kb_id, score, chunk_index
    """
    if not kb_ids or not query_text.strip():
        return []

    # Embed the query
    query_embedding = await embed_single(query_text)
    if query_embedding is None:
        print("[RAG] Query embedding failed — falling back to empty results")
        return []

    # When biasing by filename hints, over-fetch so we have headroom to re-rank.
    fetch_k = top_k * 3 if prefer_filename_hints else top_k

    all_results = _vector_query(kb_ids, query_embedding, fetch_k)
    all_results.sort(key=lambda x: x["score"], reverse=True)

    if prefer_filename_hints:
        return _apply_filename_hints(all_results, prefer_filename_hints)[:top_k]

    return all_results[:top_k]


def _vector_query(kb_ids: list[str], query_embedding: list[float], fetch_k: int) -> list[dict]:
    """Cosine-similarity leg: query each KB's Chroma collection."""
    all_results = []
    for kb_id in kb_ids:
        try:
            collection = _get_collection(kb_id)
            count = collection.count()
            if count == 0:
                continue

            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(fetch_k, count),
                include=["documents", "metadatas", "distances"],
            )

            if results["documents"] and results["documents"][0]:
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    all_results.append({
                        "text": doc,
                        "filename": meta.get("filename", "?"),
                        "kb_id": kb_id,
                        "chunk_index": meta.get("chunk_index", 0),
                        "score": 1 - dist,  # cosine distance → similarity
                    })
        except Exception as e:
            print(f"[RAG] Query error for {kb_id}: {e}")
    return all_results


def _apply_filename_hints(results: list[dict], hints: list[str]) -> list[dict]:
    """Partition results into hint-matching first, others after."""
    hints = [h.lower() for h in hints if h]
    matched, other = [], []
    for r in results:
        fn = (r.get("filename") or "").lower()
        (matched if any(h in fn for h in hints) else other).append(r)
    return matched + other


async def hybrid_query(kb_ids: list[str], query_text: str, top_k: int = 6,
                       prefer_filename_hints: list[str] | None = None) -> list[dict]:
    """Hybrid retrieval: vector (Chroma cosine) + keyword (SQLite FTS5 bm25),
    fused with Reciprocal Rank Fusion.

    Degrades gracefully: embedding failure → keyword-only; empty/missing FTS
    index → vector-only (identical to query()). Result dicts match query()'s
    shape ({text, filename, kb_id, chunk_index, score}) so format_context and
    all existing consumers work unchanged.
    """
    if not kb_ids or not query_text.strip():
        return []

    fetch_k = max(top_k * 3, 12)

    # Vector leg
    vector_results: list[dict] = []
    query_embedding = await embed_single(query_text)
    if query_embedding is not None:
        vector_results = _vector_query(kb_ids, query_embedding, fetch_k)
        vector_results.sort(key=lambda x: x["score"], reverse=True)
    else:
        print("[RAG] Query embedding failed — keyword-only retrieval")

    # Keyword leg (bm25 over kb_chunks_fts)
    try:
        keyword_results = await db.kb_fts_search(kb_ids, query_text, limit=fetch_k)
    except Exception as e:
        print(f"[RAG] FTS leg error: {e}")
        keyword_results = []

    if not keyword_results and not vector_results:
        return []

    # Reciprocal Rank Fusion keyed on (kb_id, filename, chunk_index)
    RRF_K = 60
    fused: dict[tuple, dict] = {}
    for leg in (vector_results, keyword_results):
        for rank, r in enumerate(leg):
            key = (r["kb_id"], r["filename"], int(r.get("chunk_index", 0)))
            entry = fused.get(key)
            if entry is None:
                entry = fused[key] = {
                    "text": r["text"],
                    "filename": r["filename"],
                    "kb_id": r["kb_id"],
                    "chunk_index": int(r.get("chunk_index", 0)),
                    "score": 0.0,   # cosine similarity for display (filled below)
                    "_rrf": 0.0,
                }
            entry["_rrf"] += 1.0 / (RRF_K + rank + 1)
            if "score" in r and r.get("score") is not None and r.get("score", 0) > entry["score"]:
                entry["score"] = r["score"]

    ordered = sorted(fused.values(), key=lambda x: x["_rrf"], reverse=True)

    # Keyword-only hits have no cosine score; estimate one for display from the
    # lowest real cosine so the pill's "avg relevance" stays meaningful.
    floor = min((r["score"] for r in ordered if r["score"] > 0), default=0.5)
    for r in ordered:
        if r["score"] <= 0:
            r["score"] = round(floor * 0.95, 4)
        r.pop("_rrf", None)

    if prefer_filename_hints:
        ordered = _apply_filename_hints(ordered, prefer_filename_hints)

    return ordered[:top_k]


async def backfill_fts():
    """One-time lazy backfill of kb_chunks_fts from ChromaDB documents.

    Runs at lifespan startup (non-blocking). For each KB whose FTS index is
    empty but whose Chroma collection has documents, page through the stored
    documents and bulk-insert. Idempotent via the count guard; KB collections
    only (research/code memory stay pure-vector).
    """
    try:
        kb_ids = await db.list_all_kb_ids()
    except Exception as e:
        print(f"[RAG] FTS backfill: failed to list KBs: {e}")
        return

    for kb_id in kb_ids:
        try:
            if await db.kb_fts_count(kb_id) > 0:
                continue
            collection = _get_collection(kb_id)
            total = collection.count()
            if total == 0:
                continue
            offset = 0
            page = 500
            inserted = 0
            # Group rows per filename so kb_fts_replace_file's delete-then-insert
            # stays correct (one file's chunks can span pages — accumulate first).
            by_file: dict[str, list[dict]] = {}
            while offset < total:
                got = collection.get(include=["documents", "metadatas"], limit=page, offset=offset)
                ids = got.get("ids") or []
                if not ids:
                    break
                for cid, doc, meta in zip(ids, got.get("documents") or [], got.get("metadatas") or []):
                    fn = (meta or {}).get("filename", "?")
                    by_file.setdefault(fn, []).append({
                        "chunk_id": cid,
                        "text": doc or "",
                        "chunk_index": (meta or {}).get("chunk_index", 0),
                    })
                offset += len(ids)
            for fn, chunks in by_file.items():
                await db.kb_fts_replace_file(kb_id, fn, chunks)
                inserted += len(chunks)
            if inserted:
                print(f"[RAG] FTS backfill: {kb_id} → {inserted} chunks from {len(by_file)} files")
        except Exception as e:
            print(f"[RAG] FTS backfill error for {kb_id}: {e}")


def format_context(chunks: list[dict], max_chars: int = 6000, numbered: bool = False) -> str:
    """Format retrieved chunks into a context string for the system prompt.

    numbered=True prefixes each excerpt with [n] (1-based, matching the chunk
    list order) so the model can cite sources inline.
    """
    if not chunks:
        return ""

    parts = []
    total = 0
    for n, chunk in enumerate(chunks, 1):
        if total >= max_chars:
            break
        prefix = f"[{n}] " if numbered else ""
        header = f"{prefix}[{chunk['filename']} (relevance: {chunk['score']:.0%})]"
        text = chunk["text"]
        if total + len(text) > max_chars:
            text = text[:max_chars - total]
        parts.append(f"{header}\n{text}")
        total += len(text)

    return "\n\n---\n\n".join(parts)


async def ensure_embed_model():
    """Pull the embedding model if not already available."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{config.OLLAMA_URL}/api/show",
                json={"name": EMBED_MODEL},
            )
            if resp.status_code == 200:
                print(f"[RAG] Embedding model '{EMBED_MODEL}' is available")
                return True
    except Exception:
        pass

    print(f"[RAG] Embedding model '{EMBED_MODEL}' not found — pulling...")
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{config.OLLAMA_URL}/api/pull",
                json={"name": EMBED_MODEL, "stream": False},
                timeout=600,
            )
            if resp.status_code == 200:
                print(f"[RAG] Successfully pulled '{EMBED_MODEL}'")
                return True
            else:
                print(f"[RAG] Failed to pull '{EMBED_MODEL}': {resp.text[:200]}")
                return False
    except Exception as e:
        print(f"[RAG] Error pulling '{EMBED_MODEL}': {e}")
        return False


# ── Research Auto-Indexing ──

RESEARCH_TOOLS = {"research", "deep_research", "conspiracy_research", "fetch_url"}


def _research_collection_name(persona_id: str) -> str:
    """Collection name for a persona's auto-indexed research."""
    # "mc-23ed6b99d765" → "research-mc-23ed6b99d765"
    return f"research-{persona_id}"


def _get_research_collection(persona_id: str):
    """Get or create a ChromaDB collection for a persona's research memory."""
    client = get_chroma()
    return client.get_or_create_collection(
        name=_research_collection_name(persona_id),
        metadata={"hnsw:space": "cosine"},
    )


async def index_research(persona_id: str, tool_name: str, query: str,
                         result_text: str, conv_id: str = "") -> dict:
    """Auto-index a research tool result into the persona's research memory.

    Args:
        persona_id: The persona that ran the research
        tool_name: Which tool produced the result (research, fetch_url, etc.)
        query: The original query/URL that was researched
        result_text: The tool's output text
        conv_id: Conversation ID for metadata

    Returns:
        Stats about the indexing operation
    """
    if not result_text or len(result_text.strip()) < 50:
        return {"indexed": False, "reason": "result too short"}

    # Chunk the research result
    source_label = f"{tool_name}:{query[:80]}"
    chunks = chunk_text(result_text, source_label)
    if not chunks:
        return {"indexed": False, "reason": "no chunks"}

    # Generate IDs — use hash of content for deduplication
    ids = []
    texts = []
    metadatas = []
    for chunk in chunks:
        content_hash = hashlib.md5(chunk["text"].encode()).hexdigest()
        ids.append(content_hash)
        texts.append(chunk["text"])
        metadatas.append({
            "filename": source_label,
            "tool": tool_name,
            "query": query[:200],
            "conv_id": conv_id,
            "chunk_index": chunk["chunk_index"],
            "persona_id": persona_id,
            "char_count": len(chunk["text"]),
        })

    # Embed
    embeddings = await embed_texts(texts)
    valid = [(i, t, m, e) for i, t, m, e in zip(ids, texts, metadatas, embeddings) if e is not None]
    if not valid:
        return {"indexed": False, "reason": "embedding failed"}

    v_ids, v_texts, v_metas, v_embeds = zip(*valid)

    collection = _get_research_collection(persona_id)
    collection.upsert(
        ids=list(v_ids),
        documents=list(v_texts),
        metadatas=list(v_metas),
        embeddings=list(v_embeds),
    )

    print(f"[RAG] Auto-indexed {len(v_ids)} research chunks for {persona_id} from {tool_name}")
    return {"indexed": True, "chunks": len(v_ids), "tool": tool_name}


async def query_research(persona_id: str, query_text: str, top_k: int = 4) -> list[dict]:
    """Query a persona's research memory for relevant past findings.

    Returns:
        List of dicts with keys: text, filename, score, tool, query
    """
    if not query_text.strip():
        return []

    query_embedding = await embed_single(query_text)
    if query_embedding is None:
        return []

    try:
        collection = _get_research_collection(persona_id)
        count = collection.count()
        if count == 0:
            return []

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )

        if not results["documents"] or not results["documents"][0]:
            return []

        out = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            out.append({
                "text": doc,
                "filename": meta.get("filename", "research"),
                "tool": meta.get("tool", ""),
                "query": meta.get("query", ""),
                "score": 1 - dist,
            })
        return out
    except Exception as e:
        print(f"[RAG] Research query error for {persona_id}: {e}")
        return []


async def reindex_kb(kb_id: str, files: list[dict]) -> list[dict]:
    """Reindex all files in a KB. Used for initial migration or manual reindex.

    Args:
        files: List of dicts with 'filename' and 'filepath' keys

    Returns:
        List of indexing results per file
    """
    results = []
    for f in files:
        r = await index_file(kb_id, f["filename"], f["filepath"])
        results.append(r)
    return results


# ============================================================
# CODE MEMORY — index generated code for future reference
# ============================================================
_CODE_COLLECTION = "code_memory"


def _get_code_collection():
    """Get or create the global code memory collection."""
    client = get_chroma()
    return client.get_or_create_collection(
        name=_CODE_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


async def index_generated_code(task: str, language: str, file_contents: dict,
                                conv_id: str = "", project_id: str = "") -> dict:
    """Index generated code into code memory for future reference.

    Args:
        task: The original task description
        language: Programming language
        file_contents: Dict of {filepath: code_content}
        conv_id: Conversation ID
        project_id: Project ID from generate_code

    Returns:
        Stats about the indexing operation
    """
    if not file_contents:
        return {"indexed": False, "reason": "no files"}

    ids = []
    texts = []
    metadatas = []
    for filepath, content in file_contents.items():
        if not content or len(content.strip()) < 20:
            continue
        # Chunk code content
        source_label = f"{filepath} ({language})"
        chunks = chunk_text(content, source_label)
        for chunk in chunks:
            content_hash = hashlib.md5(chunk["text"].encode()).hexdigest()
            ids.append(content_hash)
            texts.append(chunk["text"])
            metadatas.append({
                "filename": filepath,
                "language": language,
                "task": task[:300],
                "conv_id": conv_id,
                "project_id": project_id,
                "chunk_index": chunk["chunk_index"],
                "char_count": len(chunk["text"]),
            })

    if not texts:
        return {"indexed": False, "reason": "no substantial content"}

    embeddings = await embed_texts(texts)
    valid = [(i, t, m, e) for i, t, m, e in zip(ids, texts, metadatas, embeddings) if e is not None]
    if not valid:
        return {"indexed": False, "reason": "embedding failed"}

    v_ids, v_texts, v_metas, v_embeds = zip(*valid)
    collection = _get_code_collection()
    collection.upsert(
        ids=list(v_ids),
        documents=list(v_texts),
        metadatas=list(v_metas),
        embeddings=list(v_embeds),
    )
    print(f"[RAG] Indexed {len(v_ids)} code chunks from {len(file_contents)} files ({language})")
    return {"indexed": True, "chunks": len(v_ids), "files": len(file_contents)}


async def query_code_memory(query_text: str, top_k: int = 4, language: str = "") -> list[dict]:
    """Query code memory for similar past projects/code.

    Returns:
        List of dicts with keys: text, filename, score, task, language
    """
    if not query_text.strip():
        return []

    query_embedding = await embed_single(query_text)
    if query_embedding is None:
        return []

    try:
        collection = _get_code_collection()
        count = collection.count()
        if count == 0:
            return []

        where_filter = {"language": language} if language else None
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, count),
            where=where_filter,
        )
        if not results or not results.get("documents"):
            return []

        matches = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results.get("metadatas") else {}
            dist = results["distances"][0][i] if results.get("distances") else 0
            matches.append({
                "text": doc,
                "filename": meta.get("filename", ""),
                "score": 1 - dist,
                "task": meta.get("task", ""),
                "language": meta.get("language", ""),
                "project_id": meta.get("project_id", ""),
            })
        return matches
    except Exception as e:
        print(f"[RAG] Code memory query failed: {e}")
        return []
