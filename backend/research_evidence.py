"""Durable, report-scoped evidence and hybrid retrieval. No global KB pollution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime

import database as db

DOCUMENT_CHAR_LIMIT = 100_000
CORPUS_CHAR_LIMIT = 2_000_000
CHUNK_CHARS = 1536  # ~512 tokens, using the shared conservative 3 chars/token estimate
OVERLAP_CHARS = 192


def collection_name(report_id):
    return 'report-evidence-' + hashlib.sha256(report_id.encode()).hexdigest()[:32]


def chunk_document(text):
    """Keep source offsets and heading context; do not flatten document structure."""
    start, ordinal = 0, 0
    headings = list(re.finditer(r'(?m)^#{1,6}\s+(.+)$', text))
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            split = max(text.rfind('\n\n', start, end), text.rfind('. ', start, end))
            if split > start + CHUNK_CHARS // 2:
                end = split + 1
        body = text[start:end]
        if body.strip():
            heading = next((m.group(1) for m in reversed(headings) if m.start() <= start), '')
            yield dict(ordinal=ordinal, start_char=start, end_char=end, heading=heading, text=body)
            ordinal += 1
        if end == len(text):
            break
        start = max(start + 1, end - OVERLAP_CHARS)


async def owned_report(conn, report_id, *, active=False):
    rows = await conn.execute_fetchall(
        'SELECT id,status FROM research_reports WHERE id=? AND user_id=?',
        (report_id, db.current_user_id()),
    )
    return bool(rows and (not active or rows[0]['status'] in ('queued', 'running')))


async def store_documents(report_id, documents):
    """Upsert changed documents only. Stop/delete cannot resurrect evidence."""
    conn = await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        if not await owned_report(conn, report_id, active=True):
            return False
        existing = await conn.execute_fetchall(
            'SELECT source_id,content FROM research_documents WHERE report_id=?', (report_id,))
        content_by_id = {r['source_id']: r['content'] for r in existing}
        corpus_size = sum(map(len, content_by_id.values()))
        for doc in documents:
            sid = doc['source_id']
            original = str(doc.get('content') or '')
            remaining = max(0, CORPUS_CHAR_LIMIT - corpus_size + len(content_by_id.get(sid, '')))
            content = original[:min(DOCUMENT_CHAR_LIMIT, remaining)]
            meta = dict(doc.get('metadata') or {})
            meta.update(truncated=len(content) < len(original), original_chars=len(original),
                        retrieved_at=meta.get('retrieved_at') or datetime.utcnow().isoformat())
            if content_by_id.get(sid) == content:
                continue
            corpus_size += len(content) - len(content_by_id.get(sid, ''))
            content_by_id[sid] = content
            await conn.execute('DELETE FROM research_chunks WHERE report_id=? AND source_id=?', (report_id, sid))
            await conn.execute(
                'INSERT INTO research_documents(report_id,source_id,title,url,content,metadata_json) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(report_id,source_id) DO UPDATE SET title=excluded.title,url=excluded.url,'
                'content=excluded.content,metadata_json=excluded.metadata_json',
                (report_id, sid, doc.get('title', ''), doc.get('url', ''), content, json.dumps(meta)),
            )
            for chunk in chunk_document(content):
                cid = hashlib.sha256(f"{report_id}:{sid}:{chunk['ordinal']}:{chunk['text']}".encode()).hexdigest()
                await conn.execute(
                    'INSERT INTO research_chunks(id,report_id,source_id,ordinal,start_char,end_char,heading,text) VALUES(?,?,?,?,?,?,?,?)',
                    (cid, report_id, sid, chunk['ordinal'], chunk['start_char'], chunk['end_char'], chunk['heading'], chunk['text']),
                )
                await conn.execute('INSERT INTO research_chunks_fts(text,title,heading,chunk_id,report_id) VALUES(?,?,?,?,?)',
                                   (chunk['text'], doc.get('title', ''), chunk['heading'], cid, report_id))
        await conn.commit()
        return True
    finally:
        await conn.close()


async def index_pending(report_id):
    """Embedding failure never loses the SQLite/FTS copy. One bounded attempt/batch."""
    conn = await db.get_db()
    try:
        if not await owned_report(conn, report_id, active=True):
            return {'chunks': 0, 'embedded': 0, 'fallback': True}
        rows = await conn.execute_fetchall(
            'SELECT c.*,d.title FROM research_chunks c JOIN research_documents d USING(report_id,source_id) '
            'WHERE c.report_id=? AND c.embedded=0', (report_id,))
    finally:
        await conn.close()
    try:
        import rag
        import config
        import httpx
        import cancel_registry
        async with httpx.AsyncClient(timeout=30) as client:
            for offset in range(0, len(rows), 32):
                if cancel_registry.is_cancelled(report_id):
                    raise cancel_registry.RunCancelled(report_id)
                batch = rows[offset:offset + 32]
                response = await cancel_registry.await_cancellable(client.post(
                    f'{config.OLLAMA_URL}/api/embed', json={'model': rag.EMBED_MODEL,
                    'input': [f"{r['title']}\n{r['heading']}\n{r['text']}" for r in batch]}), report_id)
                response.raise_for_status()
                embeddings = response.json().get('embeddings') or []
                if len(embeddings) != len(batch):
                    raise ValueError('Incomplete embedding batch')
                conn = await db.get_db()
                try:
                    # Serialize publication with report deletion.
                    await conn.execute('BEGIN IMMEDIATE')
                    if not await owned_report(conn, report_id, active=True):
                        return {'chunks': len(rows), 'embedded': 0, 'fallback': True}
                    def upsert():
                        collection = rag.get_chroma().get_or_create_collection(
                            collection_name(report_id), metadata={'hnsw:space': 'cosine', 'kind': 'research_evidence'})
                        collection.upsert(ids=[r['id'] for r in batch], embeddings=embeddings,
                                          metadatas=[{'report_id': report_id, 'source_id': r['source_id']} for r in batch])
                    publication = asyncio.create_task(asyncio.to_thread(upsert))
                    cancelled = False
                    while True:
                        try:
                            await asyncio.shield(publication)
                            break
                        except asyncio.CancelledError:
                            # Stop followed by Delete can cancel us twice.
                            # Neither cancellation may release the lock while
                            # a worker thread can still recreate the index.
                            cancelled = True
                            if publication.cancelled():
                                raise
                    if cancelled:
                        raise asyncio.CancelledError
                    await conn.executemany('UPDATE research_chunks SET embedded=1 WHERE id=?', [(r['id'],) for r in batch])
                    await conn.commit()
                finally:
                    await conn.close()
    except Exception as exc:
        import cancel_registry
        if isinstance(exc, cancel_registry.RunCancelled):
            raise
        stats = await evidence_stats(report_id)
        return {**stats, 'fallback': True, 'error': str(exc)[:200]}
    return {**await evidence_stats(report_id), 'fallback': False}


async def evidence_stats(report_id):
    conn = await db.get_db()
    try:
        if not await owned_report(conn, report_id):
            return {'documents': 0, 'chunks': 0, 'embedded': 0}
        docs = await conn.execute_fetchall('SELECT metadata_json,length(content) AS chars FROM research_documents WHERE report_id=?', (report_id,))
        rows = await conn.execute_fetchall('SELECT COUNT(*) AS n,COALESCE(SUM(embedded),0) AS e FROM research_chunks WHERE report_id=?', (report_id,))
        return {'documents': len(docs), 'chunks': rows[0]['n'], 'embedded': rows[0]['e'],
                'document_chars': sum(r['chars'] for r in docs),
                'truncated_documents': sum(bool(json.loads(r['metadata_json']).get('truncated')) for r in docs)}
    finally:
        await conn.close()


async def retrieve(report_id, queries, *, top_k=10, neighbors=True):
    """Search each question independently; fuse BM25/vector ranks by chunk identity."""
    conn = await db.get_db()
    try:
        if not await owned_report(conn, report_id):
            return []
        rows = await conn.execute_fetchall(
            'SELECT c.*,d.title,d.url,d.metadata_json FROM research_chunks c '
            'JOIN research_documents d USING(report_id,source_id) WHERE c.report_id=?', (report_id,))
        by_id = {r['id']: dict(r) for r in rows}
        if not by_id:
            return []
        has_embeddings = any(r['embedded'] for r in rows)
        scores = {}
        def fuse(ids):
            for rank, cid in enumerate(ids):
                if cid in by_id:
                    scores[cid] = scores.get(cid, 0) + 1 / (60 + rank + 1)
        for query in [str(q).strip() for q in queries if str(q or '').strip()][:12]:
            tokens = list(dict.fromkeys(re.findall(r'[\w]+', query)))[:32]
            if tokens:
                match = ' OR '.join('"' + t + '"' for t in tokens)
                hits = await conn.execute_fetchall(
                    'SELECT chunk_id FROM research_chunks_fts WHERE research_chunks_fts MATCH ? AND report_id=? '
                    'ORDER BY bm25(research_chunks_fts) LIMIT ?', (match, report_id, top_k * 3))
                fuse([r['chunk_id'] for r in hits])
            try:
                if not has_embeddings:
                    continue
                import rag
                embedding = await asyncio.wait_for(rag.embed_single(query), timeout=20)
                if embedding is not None:
                    def search():
                        c = rag.get_chroma().get_collection(collection_name(report_id))
                        count = c.count()
                        return c.query(query_embeddings=[embedding], n_results=min(top_k * 3, count),
                                       where={'report_id': report_id}) if count else {}
                    found = await asyncio.to_thread(search)
                    fuse((found.get('ids') or [[]])[0])
            except Exception:
                pass
        ordered = [dict(by_id[cid], score=score) for cid, score in sorted(scores.items(), key=lambda x: -x[1])]
        try:
            import reranker
            if reranker.enabled() and ordered:
                ordered = await reranker.rerank('\n'.join(queries), ordered[:12], 12) + ordered[12:]
        except Exception:
            pass
        chosen, per_source = [], {}
        for row in ordered:
            sid = row['source_id']
            if per_source.get(sid, 0) >= 3:
                continue
            row['metadata'] = json.loads(row.pop('metadata_json'))
            if neighbors:
                adjacent = [dict(r) for r in rows if r['source_id'] == sid and abs(r['ordinal'] - row['ordinal']) <= 1]
                adjacent.sort(key=lambda r: r['ordinal'])
                # Source offsets let us join overlapping passages exactly.
                text, end = '', -1
                for r in adjacent:
                    text += r['text'][max(0, end - r['start_char']):]
                    end = r['end_char']
                row['text'] = text
                row['start_char'] = adjacent[0]['start_char']
                row['end_char'] = adjacent[-1]['end_char']
            # Do not pack overlapping windows repeatedly.
            if any(r['source_id'] == sid and r['start_char'] < row['end_char'] and row['start_char'] < r['end_char'] for r in chosen):
                continue
            per_source[sid] = per_source.get(sid, 0) + 1
            chosen.append(row)
            if len(chosen) >= top_k:
                break
        return chosen
    finally:
        await conn.close()


async def inspect_source(report_id, source_id, *, offset=0, limit=10):
    conn = await db.get_db()
    try:
        if not await owned_report(conn, report_id):
            return None
        docs = await conn.execute_fetchall('SELECT title,url,metadata_json FROM research_documents WHERE report_id=? AND source_id=?', (report_id, source_id))
        if not docs:
            return {'available': False, 'chunks': []}
        rows = await conn.execute_fetchall('SELECT id,ordinal,heading,text,start_char,end_char FROM research_chunks WHERE report_id=? AND source_id=? ORDER BY ordinal LIMIT ? OFFSET ?',
                                           (report_id, source_id, min(50, max(1, limit)), max(0, offset)))
        count = await conn.execute_fetchall('SELECT COUNT(*) AS n FROM research_chunks WHERE report_id=? AND source_id=?', (report_id, source_id))
        return {'available': True, 'source_id': source_id, 'title': docs[0]['title'], 'url': docs[0]['url'],
                'metadata': json.loads(docs[0]['metadata_json']), 'total': count[0]['n'], 'chunks': [dict(r) for r in rows]}
    finally:
        await conn.close()


async def retry_cleanup():
    """Persistent queue survives Chroma outages and process restarts."""
    conn = await db.get_db()
    try:
        pending = await conn.execute_fetchall('SELECT collection_name FROM research_index_cleanup LIMIT 50')
        if not pending:
            return
        import rag
        client = rag.get_chroma()
        collections = await asyncio.to_thread(client.list_collections)
        names = {c if isinstance(c, str) else c.name for c in collections}
        for row in pending:
            name = row['collection_name']
            if name in names:
                await asyncio.to_thread(client.delete_collection, name)
            await conn.execute('DELETE FROM research_index_cleanup WHERE collection_name=?', (name,))
        await conn.commit()
    except Exception:
        pass  # Queue retained for the next scheduled cleanup/startup/delete.
    finally:
        await conn.close()
