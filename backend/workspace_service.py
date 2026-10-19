"""Workspace associations and effective chat context, shared by UI and workers."""
import json
import database as db


async def migrate(conn):
    for table, column, declaration in (
        ('conversations', 'workspace_context', "TEXT NOT NULL DEFAULT '{\"mode\":\"auto\"}'"),
        ('scheduled_tasks', 'workspace_id', "TEXT NOT NULL DEFAULT ''"),
    ):
        columns = await conn.execute_fetchall(f'PRAGMA table_info({table})')
        if column not in {r['name'] for r in columns}:
            await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
    await conn.executescript('''
        CREATE TABLE IF NOT EXISTS workspace_knowledge_bases (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(workspace_id,kb_id));
        CREATE TABLE IF NOT EXISTS workspace_notes (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(workspace_id,note_id));
    ''')
    await conn.commit()


def selection(value):
    if isinstance(value, str):
        value = json.loads(value)
    value = value or {'mode': 'auto'}
    if not isinstance(value, dict) or value.get('mode') not in {'auto', 'selected', 'off'}:
        raise ValueError('Workspace context mode must be auto, selected, or off')
    if value['mode'] == 'selected':
        if not isinstance(value.get('workspace_id'), str) or not value['workspace_id']:
            raise ValueError('Choose a workspace')
        return {'mode': 'selected', 'workspace_id': value['workspace_id']}
    return {'mode': value['mode']}


async def require_workspace(ws_id):
    ws = await db.get_workspace_basic(ws_id)
    if not ws:
        raise LookupError('Workspace not found')
    return ws


async def validate_selection(value, conversation_id=None):
    value = selection(value)
    if value['mode'] == 'selected':
        await require_workspace(value['workspace_id'])
        if conversation_id:
            conn = await db.get_db()
            try:
                rows = await conn.execute_fetchall('SELECT 1 FROM workspace_conversations WHERE workspace_id=? AND conversation_id=?',
                                                   (value['workspace_id'], conversation_id))
                if not rows:
                    raise ValueError('Add this conversation to the workspace before selecting its context')
            finally:
                await conn.close()
    return value


async def resolve(conversation_id, explicit_workspace=None):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('SELECT workspace_context FROM conversations WHERE id=? AND user_id=?',
                                           (conversation_id, db.current_user_id()))
        if not rows:
            return None
        chosen = selection(rows[0]['workspace_context'])
        if explicit_workspace:
            chosen = await validate_selection({'mode': 'selected', 'workspace_id': explicit_workspace}, conversation_id)
        if chosen['mode'] == 'off':
            return None
        if chosen['mode'] == 'selected':
            linked = await conn.execute_fetchall('SELECT 1 FROM workspace_conversations WHERE workspace_id=? AND conversation_id=?',
                                                 (chosen['workspace_id'], conversation_id))
            return await db.get_workspace_basic(chosen['workspace_id']) if linked else None
        return await db.get_workspace_for_conversation(conversation_id)
    finally:
        await conn.close()


async def context_info(conversation):
    conversation['workspace_context'] = selection(conversation.get('workspace_context'))
    ws = await resolve(conversation['id'])
    conversation['effective_workspace'] = ({'id': ws['id'], 'name': ws['name'],
        'memory_enabled': ws['memory_enabled'], 'kb_ids': await knowledge_ids(ws['id'])} if ws else None)
    return conversation


async def knowledge_ids(ws_id):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('''SELECT l.kb_id FROM workspace_knowledge_bases l
            JOIN workspaces w ON w.id=l.workspace_id JOIN knowledge_bases k ON k.id=l.kb_id
            WHERE w.id=? AND w.user_id=? AND k.user_id=w.user_id ORDER BY l.added_at,l.kb_id''',
            (ws_id, db.current_user_id()))
        return [r['kb_id'] for r in rows]
    finally:
        await conn.close()


async def link(ws_id, kind, identity, *, remove=False, snapshot=None):
    await require_workspace(ws_id)
    table, column, source = {'knowledge': ('workspace_knowledge_bases', 'kb_id', 'knowledge_bases'),
                             'notes': ('workspace_notes', 'note_id', 'notes')}[kind]
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(f'SELECT id FROM {source} WHERE id=? AND user_id=?', (identity, db.current_user_id()))
        if not rows:
            raise LookupError('Linked content not found')
        if remove:
            await conn.execute(f'DELETE FROM {table} WHERE workspace_id=? AND {column}=?', (ws_id, identity))
        else:
            await conn.execute(f'INSERT OR IGNORE INTO {table}(workspace_id,{column}) VALUES(?,?)', (ws_id, identity))
            if snapshot is not None and kind == 'knowledge':
                await conn.execute('UPDATE workspace_knowledge_bases SET snapshot_json=? WHERE workspace_id=? AND kb_id=?',
                                   (json.dumps(snapshot), ws_id, identity))
        await conn.execute('UPDATE workspaces SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (ws_id,))
        await conn.commit()
    finally:
        await conn.close()


async def knowledge_status(kb):
    import indexing_jobs
    conn = await db.get_db()
    try:
        for file in kb.get('files', []):
            latest = await indexing_jobs.latest(kb['id'], file['filename'])
            metadata = await conn.execute_fetchall('SELECT * FROM kb_index_metadata WHERE kb_id=? AND filename=?',
                                                    (kb['id'], file['filename']))
            file['indexing'] = latest or ({'status': 'partial' if json.loads(metadata[0]['warnings_json']) else 'done', 'chunks': metadata[0]['chunk_count'],
                'warnings': json.loads(metadata[0]['warnings_json'])} if metadata else {'status': 'unknown'})
        kb['ready'] = bool(kb.get('files')) and all(f['indexing']['status'] == 'done' for f in kb['files'])
        return kb
    finally:
        await conn.close()


async def related(ws_id, kind, limit=50, offset=0):
    await require_workspace(ws_id)
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    if kind == 'knowledge':
        ids = await knowledge_ids(ws_id)
        conn = await db.get_db()
        try:
            items = []
            for identity in ids[offset:offset + limit]:
                kb = await knowledge_status(await db.get_kb(identity))
                rows = await conn.execute_fetchall('SELECT snapshot_json,added_at FROM workspace_knowledge_bases WHERE workspace_id=? AND kb_id=?', (ws_id, identity))
                kb['snapshot'] = json.loads(rows[0]['snapshot_json'])
                kb['linked_at'] = rows[0]['added_at']
                items.append(kb)
            return {'items': items, 'has_more': len(ids) > offset + limit}
        finally:
            await conn.close()
    conn = await db.get_db()
    try:
        if kind == 'notes':
            rows = await conn.execute_fetchall('''SELECT n.* FROM notes n JOIN workspace_notes l ON l.note_id=n.id
                WHERE l.workspace_id=? AND n.user_id=? ORDER BY n.done,COALESCE(NULLIF(n.due_at,''),'9999'),n.updated_at DESC LIMIT ? OFFSET ?''',
                (ws_id, db.current_user_id(), limit + 1, offset))
            items = [db._row_to_note(r) for r in rows]
        elif kind == 'tasks':
            rows = await conn.execute_fetchall('SELECT * FROM scheduled_tasks WHERE workspace_id=? AND user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?',
                (ws_id, db.current_user_id(), limit + 1, offset))
            items = [db._row_to_scheduled_task(r) for r in rows]
        else:
            raise ValueError('Unknown content type')
        return {'items': items[:limit], 'has_more': len(items) > limit}
    finally:
        await conn.close()


async def note_workspaces(note_id):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall('''SELECT w.id FROM workspaces w JOIN workspace_notes l ON l.workspace_id=w.id
            WHERE l.note_id=? AND w.user_id=?''', (note_id, db.current_user_id()))
        return [r['id'] for r in rows]
    finally:
        await conn.close()


async def set_note_workspaces(note_id, ids):
    ids = list(dict.fromkeys(ids))
    for ws_id in ids:
        await require_workspace(ws_id)
    if not await db.get_note(note_id):
        raise LookupError('Note not found')
    conn = await db.get_db()
    try:
        await conn.execute('DELETE FROM workspace_notes WHERE note_id=?', (note_id,))
        await conn.executemany('INSERT INTO workspace_notes(workspace_id,note_id) VALUES(?,?)', [(w, note_id) for w in ids])
        await conn.commit()
    finally:
        await conn.close()


async def attach_task_context(conn, task_id, user_id):
    """Keep task conversation creation and association in its save transaction."""
    import uuid
    rows = await conn.execute_fetchall('SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?',(task_id,user_id))
    if not rows: return
    task = dict(rows[0]); workspace_id = task.get('workspace_id') or ''
    if workspace_id:
        owned = await conn.execute_fetchall('SELECT id FROM workspaces WHERE id=? AND user_id=?',(workspace_id,user_id))
        if not owned: raise ValueError('Workspace not found')
        if task['task_type'] not in {'llm','research'}: raise ValueError('Workspace context is available for agent and research tasks')
    if task['task_type']!='llm': return
    conversation_id = task.get('conversation_id') or ''
    if not conversation_id and (workspace_id or json.loads(task['tool_ids'] or '[]')):
        conversation_id = 'conv-task-'+uuid.uuid4().hex[:12]
        chosen = {'mode':'selected','workspace_id':workspace_id} if workspace_id else {'mode':'off'}
        await conn.execute('INSERT INTO conversations(id,user_id,title,model,workspace_context) VALUES(?,?,?,?,?)',
            (conversation_id,user_id,task['title'],task['model'] or '',json.dumps(chosen)))
        await conn.execute('UPDATE scheduled_tasks SET conversation_id=? WHERE id=?',(conversation_id,task_id))
    if conversation_id:
        owned = await conn.execute_fetchall('SELECT id FROM conversations WHERE id=? AND user_id=?',(conversation_id,user_id))
        if not owned: raise ValueError('Conversation not found')
        if workspace_id:
            await conn.execute('INSERT OR IGNORE INTO workspace_conversations(workspace_id,conversation_id) VALUES(?,?)',(workspace_id,conversation_id))
