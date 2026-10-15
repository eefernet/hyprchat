"""Owner-scoped SQLite FTS across the user's saved library."""
import re
import database as db


async def migrate(conn):
    await conn.executescript('''
        CREATE TABLE IF NOT EXISTS library_entries (
            rowid INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, source_id TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS library_owner ON library_entries(user_id,kind,created_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(title,body,content='library_entries',content_rowid='rowid',tokenize='unicode61');
        CREATE TRIGGER IF NOT EXISTS library_fts_insert AFTER INSERT ON library_entries BEGIN
            INSERT INTO library_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body); END;
        CREATE TRIGGER IF NOT EXISTS library_fts_delete AFTER DELETE ON library_entries BEGIN
            INSERT INTO library_fts(library_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body); END;
        CREATE TRIGGER IF NOT EXISTS library_fts_update AFTER UPDATE ON library_entries BEGIN
            INSERT INTO library_fts(library_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body);
            INSERT INTO library_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body); END;
    ''')
    sources={
        'messages':('chat',"SELECT m.id,c.user_id,m.conversation_id,c.title,m.content,m.created_at,c.updated_at FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.role IN ('user','assistant')"),
        'notes':('note',"SELECT id,user_id,'',title,content,created_at,updated_at FROM notes"),
        'research_reports':('research',"SELECT id,user_id,'',title,query||char(10)||report_markdown,created_at,updated_at FROM research_reports"),
        'artifacts':('artifact',"SELECT id,user_id,COALESCE(conversation_id,''),COALESCE(NULLIF(title,''),filename),filename||char(10)||description||char(10)||content_text,created_at,updated_at FROM artifacts"),
    }
    for table,(kind,query) in sources.items():
        view='library_projection_'+table
        await conn.execute(f'DROP VIEW IF EXISTS {view}')
        await conn.execute(f'CREATE VIEW {view}(source_id,user_id,parent_id,title,body,created_at,updated_at) AS {query}')
        insert=f"""INSERT INTO library_entries(key,user_id,kind,source_id,parent_id,title,body,created_at,updated_at)
            SELECT '{kind}:'||source_id,user_id,'{kind}',source_id,parent_id,COALESCE(title,''),COALESCE(body,''),created_at,updated_at FROM {view}"""
        upsert='''ON CONFLICT(key) DO UPDATE SET title=excluded.title,body=excluded.body,parent_id=excluded.parent_id,updated_at=excluded.updated_at
            WHERE library_entries.title IS NOT excluded.title OR library_entries.body IS NOT excluded.body OR library_entries.updated_at IS NOT excluded.updated_at'''
        for action in ('insert','update'):
            trigger=f'library_{table}_{action}'
            await conn.execute(f'DROP TRIGGER IF EXISTS {trigger}')
            await conn.execute(f'CREATE TRIGGER {trigger} AFTER {action.upper()} ON {table} BEGIN {insert} WHERE source_id=new.id {upsert}; END')
        await conn.execute(f"CREATE TRIGGER IF NOT EXISTS library_{table}_delete AFTER DELETE ON {table} BEGIN DELETE FROM library_entries WHERE key='{kind}:'||old.id; END")
        await conn.execute(f'{insert} WHERE true {upsert}')
    await conn.execute('''CREATE TRIGGER IF NOT EXISTS library_conversation_title AFTER UPDATE OF title ON conversations BEGIN
        UPDATE library_entries SET title=new.title,updated_at=new.updated_at WHERE kind='chat' AND parent_id=new.id; END''')
    await conn.commit()


async def search(query, *, kind='', workspace_id='', start='', end='', limit=50, offset=0):
    words=re.findall(r'\w+',query,flags=re.UNICODE)[:24]
    if not words: return {'results':[]}
    expression=' AND '.join('"'+word+'"' for word in words)
    conn=await db.get_db()
    try:
        where='library_fts MATCH ? AND e.user_id=?'
        args=[expression,db.current_user_id()]
        if kind:
            where+=' AND e.kind=?';args.append(kind)
        if start:
            where+=' AND e.created_at>=?';args.append(start)
        if end:
            where+=' AND e.created_at<?';args.append(end)
        if workspace_id:
            where+=''' AND EXISTS(SELECT 1 FROM workspaces w WHERE w.id=? AND w.user_id=e.user_id AND (
                (e.kind='chat' AND EXISTS(SELECT 1 FROM workspace_conversations l WHERE l.workspace_id=w.id AND l.conversation_id=e.parent_id)) OR
                (e.kind='research' AND EXISTS(SELECT 1 FROM workspace_research_reports l WHERE l.workspace_id=w.id AND l.report_id=e.source_id)) OR
                (e.kind='artifact' AND EXISTS(SELECT 1 FROM artifact_workspaces l WHERE l.workspace_id=w.id AND l.artifact_id=e.source_id))))'''
            args.append(workspace_id)
        rows=await conn.execute_fetchall(f'''SELECT e.kind,e.source_id,e.parent_id,e.title,e.created_at,e.updated_at,
            snippet(library_fts,1,'','',' … ',40) excerpt FROM library_fts JOIN library_entries e ON e.rowid=library_fts.rowid
            WHERE {where} ORDER BY rank,e.updated_at DESC LIMIT ? OFFSET ?''',(*args,max(1,min(limit,100)),max(0,offset)))
        return {'results':[dict(r) for r in rows]}
    finally: await conn.close()


async def rebuild():
    conn=await db.get_db()
    try:
        await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
        await conn.commit()
        return {'status':'rebuilt'}
    finally: await conn.close()
