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

# ── ChromaDB persistent client ──
_chroma_client: Optional[chromadb.ClientAPI] = None
CHROMA_DIR = os.path.join(os.path.dirname(config.KB_DIR), "chroma_db")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# Chunking settings
CHUNK_SIZE = 500       # target tokens per chunk (~2000 chars)
CHUNK_OVERLAP = 50     # overlap tokens between chunks
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

    target_chars = CHUNK_SIZE * CHARS_PER_TOKEN
    overlap_chars = CHUNK_OVERLAP * CHARS_PER_TOKEN

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
    """Embed a batch of texts using Ollama's embedding endpoint."""
    embeddings = []
    async with httpx.AsyncClient(timeout=120) as client:
        # Ollama supports batch embedding
        for i in range(0, len(texts), 10):  # batch of 10
            batch = texts[i:i+10]
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

    # Upsert into ChromaDB (idempotent — same IDs overwrite)
    collection = _get_collection(kb_id)
    collection.upsert(
        ids=list(v_ids),
        documents=list(v_texts),
        metadatas=list(v_metas),
        embeddings=list(v_embeds),
    )

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


async def delete_kb_index(kb_id: str):
    """Delete the entire ChromaDB collection for a KB."""
    try:
        client = get_chroma()
        client.delete_collection(_collection_name(kb_id))
        print(f"[RAG] Deleted collection for {kb_id}")
    except Exception as e:
        print(f"[RAG] Error deleting collection {kb_id}: {e}")


async def query(kb_ids: list[str], query_text: str, top_k: int = 6) -> list[dict]:
    """Query multiple KBs and return the most relevant chunks.

    Args:
        kb_ids: List of KB IDs to search
        query_text: The user's question/query
        top_k: Number of chunks to return (total across all KBs)

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

    # Query each KB's collection
    all_results = []
    for kb_id in kb_ids:
        try:
            collection = _get_collection(kb_id)
            count = collection.count()
            if count == 0:
                continue

            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, count),
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

    # Sort by score descending and take top_k
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:top_k]


def format_context(chunks: list[dict], max_chars: int = 6000) -> str:
    """Format retrieved chunks into a context string for the system prompt."""
    if not chunks:
        return ""

    parts = []
    total = 0
    for chunk in chunks:
        if total >= max_chars:
            break
        header = f"[{chunk['filename']} (relevance: {chunk['score']:.0%})]"
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
