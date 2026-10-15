"""Execution-time context records, separate from mutable message metadata."""
import json
import re
import database as db


def redact(value):
    text=str(value or '')
    text=re.sub(r'\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}', '[credential removed]', text)
    text=re.sub(r'(?i)(password|api[_ -]?key|authorization|bearer|access[_ -]?token)(\s*[:=]\s*|\s+)[^\s,;]+',r'\1 [credential removed]',text)
    text=re.sub(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@',r'\1[credential removed]@',text)
    return text


async def capture(message_id, *, model, provider, options, memories, retrieved, sources,
                  tools, messages, original_chars, thinking=None):
    if message_id is None: return
    payload={'version':1,'model':model,'provider':provider,
        'settings':{k:v for k,v in options.items() if k in {'num_ctx','num_predict','temperature','top_p','top_k','repeat_penalty','seed'}},
        'thinking_enabled':thinking,
        'memories':{name:{'ids':list(info.get('memory_ids') or []),'block_ids':list(info.get('block_ids') or []),
            'selected_context':redact(info.get('context'))[:20000]} for name,info in memories.items()},
        'retrieved_context':redact(retrieved)[:60000],
        'search_sources':[{'title':redact(s.get('title'))[:300],'url':redact(s.get('url'))[:2000]} for s in sources[:50] if isinstance(s,dict)],
        'available_tools':sorted(tools),
        'message_count':len(messages),
        'context_chars':sum(len(m.get('content') or '') for m in messages),
        'truncated_chars':max(0,original_chars-sum(len(m.get('content') or '') for m in messages)),
        'limits_note':'Character counts describe the assembled request. Provider tokenization and internal truncation are not observable.'}
    conn=await db.get_db()
    try:
        await conn.execute('''INSERT INTO message_context(message_id,sequence,payload)
            SELECT ?,COALESCE((SELECT MAX(sequence)+1 FROM message_context WHERE message_id=?),1),?
            WHERE EXISTS(SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND c.user_id=?)''',
            (message_id,message_id,json.dumps(payload),message_id,db.current_user_id()))
        await conn.commit()
    finally: await conn.close()


async def inspect(message_id):
    conn=await db.get_db()
    try:
        owned=await conn.execute_fetchall('SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND c.user_id=?',(message_id,db.current_user_id()))
        if not owned: raise LookupError('Message not found')
        rows=await conn.execute_fetchall('SELECT sequence,payload,created_at FROM message_context WHERE message_id=? ORDER BY sequence',(message_id,))
        return {'available':bool(rows),'rounds':[{'sequence':r['sequence'],'created_at':r['created_at'],**json.loads(r['payload'])} for r in rows]}
    finally: await conn.close()


async def record_tool(message_id, name):
    if message_id is None:return
    conn=await db.get_db()
    try:
        await conn.execute('''UPDATE message_context SET payload=json_insert(
            json_set(payload,'$.executed_tools',json(COALESCE(json_extract(payload,'$.executed_tools'),'[]'))),
            '$.executed_tools[#]',?) WHERE message_id=? AND sequence=(SELECT MAX(sequence) FROM message_context WHERE message_id=?)
            AND EXISTS(SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND c.user_id=?)''',
            (name[:200],message_id,message_id,message_id,db.current_user_id()))
        await conn.commit()
    finally: await conn.close()
