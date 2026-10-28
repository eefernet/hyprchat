"""Workspace associations and effective chat context, shared by UI and workers."""
import json
import database as db


async def migrate(conn):
    for table, column, declaration in (
        ('workspaces', 'chat_defaults', "TEXT NOT NULL DEFAULT '{}'"),
        ('workspaces', 'source_context_enabled', 'INTEGER NOT NULL DEFAULT 1'),
        ('conversations', 'workspace_context', "TEXT NOT NULL DEFAULT '{\"mode\":\"auto\"}'"),
        ('scheduled_tasks', 'workspace_id', "TEXT NOT NULL DEFAULT ''"),
    ):
        columns = await conn.execute_fetchall(f'PRAGMA table_info({table})')
        if column not in {r['name'] for r in columns}:
            await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
    await conn.executescript('''
        CREATE TABLE IF NOT EXISTS workspace_source_preferences (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, source_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, pinned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(workspace_id,kind,source_id));
        CREATE TABLE IF NOT EXISTS workspace_document_stores (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
            kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS workspace_source_files (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, source_id TEXT NOT NULL, revision TEXT NOT NULL,
            kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES kb_files(id) ON DELETE CASCADE,
            filename TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
            PRIMARY KEY(workspace_id,kind,source_id,revision));
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
        ids = [identity for identity in await knowledge_ids(ws_id) if await source(ws_id,'knowledge',identity)]
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


def chat_defaults(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or set(value) - {'model', 'model_config_id', 'tool_ids'}:
        raise ValueError('Chat defaults must contain only model, model_config_id, and tool_ids')
    result = {}
    for key, item in value.items():
        if item is None:
            continue
        if key == 'tool_ids':
            if not isinstance(item, list) or any(not isinstance(t, str) or not t for t in item):
                raise ValueError('Tool defaults must be a list; [] disables optional tools')
            result[key] = list(dict.fromkeys(item))
        elif not isinstance(item, str):
            raise ValueError(f'{key} must be text')
        else:
            result[key] = item
    return result


def normalize_workspace(ws):
    if ws is not None:
        ws['chat_defaults'] = chat_defaults(ws.get('chat_defaults') or {})
        ws['source_context_enabled'] = bool(ws.get('source_context_enabled', 1))
    return ws


async def resolve_defaults(choices, workspace_id=None):
    import config
    defaults = chat_defaults((await require_workspace(workspace_id)).get('chat_defaults') or {}) if workspace_id else {}
    explicit = {k: v for k, v in choices.items() if v is not None}
    selected = {**defaults, **explicit}
    persona_id = selected.get('model_config_id') or None
    persona = await db.get_model_config(persona_id) if persona_id else None
    if persona_id and not persona:
        raise ValueError('Configured persona is unavailable. Choose another persona in workspace settings or for this chat.')
    return {
        'model': selected.get('model') or (persona or {}).get('base_model') or config.DEFAULT_MODEL,
        'model_config_id': persona_id,
        'tool_ids': selected.get('tool_ids', (persona or {}).get('tool_ids', [])),
        'system_prompt': explicit.get('system_prompt', (persona or {}).get('system_prompt', '')),
        'persona_name': (persona or {}).get('name', ''),
        'persona_avatar': ((persona or {}).get('parameters') or {}).get('avatar', ''),
    }

# Membership is derived on every read; preferences never grant access.
_SOURCE_SQL = '''
WITH owned AS (SELECT id,user_id FROM workspaces WHERE id=? AND user_id=?),
sources AS (
 SELECT 'chat' kind,c.id source_id,c.title title,c.updated_at revision,'' parent_id,
        CASE WHEN EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.role IN ('user','assistant') AND length(m.content)>0) THEN 'ready' ELSE 'empty' END readiness
 FROM conversations c JOIN workspace_conversations l ON l.conversation_id=c.id JOIN owned w ON w.id=l.workspace_id AND w.user_id=c.user_id
 UNION ALL
 SELECT 'note',n.id,n.title,n.updated_at,'',CASE WHEN length(n.content)>0 THEN 'ready' ELSE 'empty' END
 FROM notes n JOIN workspace_notes l ON l.note_id=n.id JOIN owned w ON w.id=l.workspace_id AND w.user_id=n.user_id
 UNION ALL
 SELECT 'research',r.id,r.title,r.updated_at,'',CASE WHEN r.status IN ('done','completed','succeeded') AND length(r.report_markdown)>0 THEN 'ready' ELSE r.status END
 FROM research_reports r JOIN workspace_research_reports l ON l.report_id=r.id JOIN owned w ON w.id=l.workspace_id AND w.user_id=r.user_id
 UNION ALL
 SELECT 'artifact',a.id,COALESCE(NULLIF(a.title,''),a.filename),a.updated_at,COALESCE(a.conversation_id,''),CASE WHEN length(a.content_text)>0 THEN 'ready' ELSE 'unindexed' END
 FROM artifacts a JOIN owned w ON w.user_id=a.user_id WHERE a.workspace_id=w.id
 OR EXISTS(SELECT 1 FROM artifact_workspaces l WHERE l.workspace_id=w.id AND l.artifact_id=a.id)
 OR EXISTS(SELECT 1 FROM workspace_conversations l WHERE l.workspace_id=w.id AND l.conversation_id=a.conversation_id)
 UNION ALL
 SELECT 'knowledge',k.id,k.name,k.updated_at,'','unknown'
 FROM knowledge_bases k JOIN workspace_knowledge_bases l ON l.kb_id=k.id JOIN owned w ON w.id=l.workspace_id AND w.user_id=k.user_id
 WHERE NOT EXISTS(SELECT 1 FROM workspace_document_stores d WHERE d.kb_id=k.id)
 UNION ALL
 SELECT 'document',f.source_id,f.title,f.revision,f.kb_id,'unknown'
 FROM workspace_source_files f JOIN owned w ON w.id=f.workspace_id
 JOIN kb_files k ON k.id=f.file_id WHERE f.kind='document'
)
SELECT s.*,COALESCE(p.enabled,1) enabled,COALESCE(p.pinned,0) pinned
FROM sources s LEFT JOIN workspace_source_preferences p ON p.workspace_id=(SELECT id FROM owned) AND p.kind=s.kind AND p.source_id=s.source_id
'''


def destination(kind, identity, parent_id='', **extra):
    from urllib.parse import urlencode
    return '#source?' + urlencode({'kind': kind, 'id': identity, 'parent': parent_id, **extra})


async def source_rows(ws_id, *, query='', kind='', limit=50, offset=0, pinned=False):
    await require_workspace(ws_id)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(_SOURCE_SQL + ''' WHERE (?='' OR s.kind=?)
            AND (?='' OR instr(lower(s.title),lower(?))>0) AND (?=0 OR COALESCE(p.pinned,0)=1)
            ORDER BY COALESCE(p.pinned,0) DESC,s.revision DESC,s.kind,s.source_id LIMIT ? OFFSET ?''',
            (ws_id, db.current_user_id(), kind, kind, query, query, int(pinned), max(1,min(limit,100))+1,max(0,offset)))
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def source(ws_id, kind, identity):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(_SOURCE_SQL + ' WHERE s.kind=? AND s.source_id=?',
                                           (ws_id,db.current_user_id(),kind,str(identity)))
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def set_source_preferences(ws_id, kind, identity, values):
    if not await source(ws_id,kind,identity):
        raise LookupError('Source is no longer in this workspace')
    if not values or set(values)-{'enabled','pinned'} or any(type(v) is not bool for v in values.values()):
        raise ValueError('Choose boolean enabled and/or pinned settings')
    conn=await db.get_db()
    try:
        await conn.execute('INSERT OR IGNORE INTO workspace_source_preferences(workspace_id,kind,source_id) VALUES(?,?,?)',(ws_id,kind,str(identity)))
        await conn.execute('UPDATE workspace_source_preferences SET '+','.join(f'{k}=?' for k in values)+' WHERE workspace_id=? AND kind=? AND source_id=?',
                           (*[int(v) for v in values.values()],ws_id,kind,str(identity)))
        await conn.commit()
    finally: await conn.close()
    return await source(ws_id,kind,identity)


async def source_files(ws_id):
    await require_workspace(ws_id)
    conn=await db.get_db()
    try:
        return [dict(r) for r in await conn.execute_fetchall('SELECT f.* FROM workspace_source_files f JOIN kb_files k ON k.id=f.file_id WHERE workspace_id=?',(ws_id,))]
    finally: await conn.close()


SUPPORTED_DOCUMENTS = {'.pdf','.docx','.pptx','.xlsx','.txt','.md','.csv','.json','.jsonl','.html','.htm','.py','.js','.ts','.jsx','.tsx','.css','.sh','.yaml','.yml','.toml','.xml','.log','.rs','.go','.java','.c','.cpp','.h','.ini','.conf'}


def artifact_revision(artifact):
    import hashlib
    # Include text as well as file identity: edits within one timestamp tick still invalidate.
    return hashlib.sha256(json.dumps({k:artifact.get(k) for k in ('updated_at','sha256','storage_path','url','filename','content_text')},sort_keys=True).encode()).hexdigest()


async def document_store(ws_id):
    import uuid
    ws=await require_workspace(ws_id)
    conn=await db.get_db()
    try:
        await conn.execute('BEGIN IMMEDIATE')
        rows=await conn.execute_fetchall('SELECT kb_id FROM workspace_document_stores WHERE workspace_id=?',(ws_id,))
        if rows:
            return rows[0]['kb_id']
        identity='kb-'+uuid.uuid4().hex
        await conn.execute('INSERT INTO knowledge_bases(id,user_id,name,description) VALUES(?,?,?,?)',(identity,db.current_user_id(),ws['name']+' documents','Original workspace documents and artifact revisions'))
        await conn.execute('INSERT INTO workspace_document_stores(workspace_id,kb_id) VALUES(?,?)',(ws_id,identity))
        await conn.execute('INSERT INTO workspace_knowledge_bases(workspace_id,kb_id) VALUES(?,?)',(ws_id,identity))
        await conn.commit()
        return identity
    finally: await conn.close()


async def save_source_file(ws_id, kind, identity, revision, title, content, track_bg):
    import asyncio, hashlib, uuid
    from pathlib import Path
    import config, indexing_jobs
    kb_id=await document_store(ws_id)
    filename=uuid.uuid4().hex + Path(title).suffix.lower()
    folder=Path(config.KB_DIR)/kb_id
    await asyncio.to_thread(folder.mkdir,parents=True,exist_ok=True)
    path=folder/filename
    await asyncio.to_thread(path.write_bytes,content)
    sha=hashlib.sha256(content).hexdigest()
    file_id=await db.add_kb_file(kb_id,filename,str(path),len(content),'application/octet-stream')
    conn=await db.get_db()
    try:
        await conn.execute('INSERT INTO workspace_source_files(workspace_id,kind,source_id,revision,kb_id,file_id,filename,title,sha256) VALUES(?,?,?,?,?,?,?,?,?)',
                           (ws_id,kind,identity,revision,kb_id,file_id,filename,title,sha))
        await conn.commit()
    finally: await conn.close()
    job_id=None
    if Path(title).suffix.lower() in SUPPORTED_DOCUMENTS:
        job_id=await indexing_jobs.submit(kb_id,filename,str(path))
        track_bg(indexing_jobs.execute(job_id))
    return {'source_id':identity,'file_id':file_id,'kb_id':kb_id,'job_id':job_id,'status':'queued' if job_id else 'unsupported'}


async def index_artifact(ws_id, identity, track_bg):
    import asyncio
    from pathlib import Path
    import config
    from artifact_files import artifact_path_for_row
    member=await source(ws_id,'artifact',identity)
    if not member or not member['enabled']: return None
    artifact=await db.get_artifact(identity)
    if not artifact: return None
    revision=artifact_revision(artifact)
    existing=next((f for f in await source_files(ws_id) if f['kind']=='artifact' and f['source_id']==identity and f['revision']==revision),None)
    if existing: return existing
    title=artifact.get('filename') or 'artifact.txt'
    if Path(title).suffix.lower() not in SUPPORTED_DOCUMENTS: return None
    path,_=artifact_path_for_row(artifact)
    if not path: return None
    def read():
        with open(path,'rb') as stream:
            return stream.read(config.MAX_UPLOAD_SIZE_MB*1024*1024+1)
    content=await asyncio.to_thread(read)
    if len(content)>config.MAX_UPLOAD_SIZE_MB*1024*1024: raise ValueError('Artifact exceeds upload size limit')
    # Recheck after I/O; never publish a revision under a changed source identity.
    current=await db.get_artifact(identity)
    if not current or artifact_revision(current)!=revision: return None
    return await save_source_file(ws_id,'artifact',identity,revision,title,content,track_bg)


async def list_sources(ws_id, query='', kind='', limit=50, offset=0, pinned=False):
    from pathlib import Path
    import indexing_jobs
    ws=await require_workspace(ws_id)
    rows=await source_rows(ws_id,query=query,kind=kind,limit=limit,offset=offset,pinned=pinned)
    files=await source_files(ws_id)
    result=[]
    for row in rows[:max(1,min(limit,100))]:
        row['url']=destination(row['kind'],row['source_id'])
        row['can_use']=bool(ws['source_context_enabled'] and row['enabled'])
        if row['kind']=='knowledge':
            kb=await knowledge_status(await db.get_kb(row['source_id']))
            states=[f['indexing']['status'] for f in kb['files']]
            row['readiness']='ready' if kb['ready'] else 'partial' if 'partial' in states else 'needs attention' if 'error' in states else 'indexing' if 'indexing' in states else 'unindexed'
            row['files']=kb['files']
        elif row['kind'] in {'document','artifact'}:
            artifact=await db.get_artifact(row['source_id']) if row['kind']=='artifact' else None
            revision=artifact_revision(artifact) if artifact else row['revision']
            f=next((f for f in files if f['kind']==row['kind'] and f['source_id']==row['source_id'] and f['revision']==revision),None)
            if f:
                latest=await indexing_jobs.latest(f['kb_id'],f['filename'])
                row['indexing']=latest
                row['readiness']=(latest or {}).get('state','unindexed')
                if row['readiness']=='succeeded': row['readiness']='ready'
                row['file_id'],row['kb_id']=f['file_id'],f['kb_id']
                if row['kind']=='document':row['url']=destination('document',str(f['file_id']),f['kb_id'])
            if Path((artifact or {}).get('filename') or row['title']).suffix.lower() not in SUPPORTED_DOCUMENTS and row['readiness']!='ready':
                row['readiness']='unsupported — original available'
        row['can_use']=row['can_use'] and row['readiness'] in {'ready','partial'}
        result.append(row)
    return {'items':result,'has_more':len(rows)>limit,'source_context_enabled':ws['source_context_enabled']}


async def picker(ws_id, kind, query='', limit=30, offset=0):
    await require_workspace(ws_id)
    tables={'chat':('conversations','title'),'note':('notes','title'),'research':('research_reports','title'),'artifact':('artifacts',"COALESCE(NULLIF(title,''),filename)"),'knowledge':('knowledge_bases','name')}
    if kind not in tables: raise ValueError('Unknown source kind')
    table,title=tables[kind]
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall(f'SELECT id,{title} title FROM {table} WHERE user_id=? AND instr(lower({title}),lower(?))>0 ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                                         (db.current_user_id(),query,max(1,min(limit,100))+1,max(0,offset)))
        items=[{**dict(r),'linked':bool(await source(ws_id,kind,r['id']))} for r in rows[:limit]]
        return {'items':items,'has_more':len(rows)>limit}
    finally: await conn.close()


async def link_source(ws_id, kind, identity, remove=False):
    await require_workspace(ws_id)
    if kind in {'note','knowledge'}:
        return await link(ws_id,{'note':'notes','knowledge':'knowledge'}[kind],identity,remove=remove)
    tables={'chat':('conversations','workspace_conversations','conversation_id'),
            'research':('research_reports','workspace_research_reports','report_id'),
            'artifact':('artifacts','artifact_workspaces','artifact_id')}
    if kind not in tables: raise ValueError('Unknown source kind')
    original,association,column=tables[kind]
    conn=await db.get_db()
    try:
        owned=await conn.execute_fetchall(f'SELECT id FROM {original} WHERE id=? AND user_id=?',(identity,db.current_user_id()))
        if not owned: raise LookupError('Content not found')
        if remove:
            await conn.execute(f'DELETE FROM {association} WHERE workspace_id=? AND {column}=?',(ws_id,identity))
            if kind=='artifact':
                await conn.execute("UPDATE artifacts SET workspace_id=NULL WHERE id=? AND workspace_id=?",(identity,ws_id))
        else:
            await conn.execute(f'INSERT OR IGNORE INTO {association}(workspace_id,{column}) VALUES(?,?)',(ws_id,identity))
        await conn.execute('UPDATE workspaces SET updated_at=CURRENT_TIMESTAMP WHERE id=?',(ws_id,))
        await conn.commit()
    finally: await conn.close()


def library_revision(row):
    import hashlib
    return hashlib.sha256((row['title']+'\n'+row['body']).encode()).hexdigest()


async def library_passages(ws_id, query, conversation_id, top_k):
    import library_search
    hits=(await library_search.search(query,workspace_id=ws_id,question=True,exclude_conversation=conversation_id,limit=top_k))['results']
    return [{**h,'filename':h['title'],'text':h['excerpt'],'revision':library_revision(h),
             'location':f"{h.get('role') or h['kind']} excerpt" + (f" · message {h['source_id']}" if h['kind']=='chat' else ''),
             'url':destination(h['kind'],h['source_id'],h['parent_id']), 'score':1/(1+i),
             'truncated':len(h['body'])>len(h['excerpt'])} for i,h in enumerate(hits)]


async def validate_library_passage(ws_id, hit, conversation_id):
    if hit['kind']=='chat' and hit['parent_id']==conversation_id: return False
    member=await source(ws_id,hit['kind'],hit['parent_id'] if hit['kind']=='chat' else hit['source_id'])
    if not member or not member['enabled']: return False
    conn=await db.get_db()
    try:
        rows=await conn.execute_fetchall('SELECT title,body FROM library_entries WHERE kind=? AND source_id=? AND user_id=?',
                                        (hit['kind'],hit['source_id'],db.current_user_id()))
        return bool(rows and library_revision(dict(rows[0]))==hit['revision'])
    finally: await conn.close()


async def retrieve(ws_id, conversation_id, query, persona_kbs, top_k, max_chars):
    """Concurrent retrieval with a shared deadline and final live authorization checks."""
    import asyncio, time
    import config, rag, indexing_jobs
    deadline=time.monotonic()+config.RAG_RETRIEVAL_DEADLINE_SECONDS
    ws=await require_workspace(ws_id) if ws_id else None
    if ws and not ws['source_context_enabled']: ws_id=None
    if ws_id:
        try:
            from routes.context import route_context
            route_context().track_bg(prepare_workspace_artifacts(ws_id))
        except RuntimeError:
            pass
    workspace_kbs=await knowledge_ids(ws_id) if ws_id else []
    if ws_id:
        workspace_kbs=[k for k in workspace_kbs if not (await source(ws_id,'knowledge',k)) or (await source(ws_id,'knowledge',k))['enabled']]
    # Persona KB ownership is checked even for callers other than the chat route.
    persona_kbs=[k for k in persona_kbs if await db.get_kb(k)]
    kb_ids=list(dict.fromkeys(persona_kbs+workspace_kbs))
    tasks=[asyncio.create_task(rag.hybrid_query(kb_ids,query,top_k=top_k*3,deadline=deadline-min(.25,config.RAG_RETRIEVAL_DEADLINE_SECONDS*.2)))] if kb_ids else []
    if ws_id: tasks.append(asyncio.create_task(library_passages(ws_id,query,conversation_id,top_k*3)))
    results=[]; partial=False
    try:
        if tasks:
            done,pending=await asyncio.wait(tasks,timeout=max(0,deadline-time.monotonic()-min(.1,config.RAG_RETRIEVAL_DEADLINE_SECONDS*.1)))
            partial=bool(pending)
            # Preserve source ordering deterministically regardless of completion order.
            for task in tasks:
                if task in done and not task.cancelled() and task.exception() is None: results.append(task.result())
                else: partial=True
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    mappings=await source_files(ws_id) if ws_id else []
    internal_ids={f['kb_id'] for f in mappings}
    files={}
    for k in kb_ids:
        kb=await db.get_kb(k)
        if kb:
            for f in kb.get('files',[]):files[k,f['filename']]=f
    # Interleave independently ranked paths; scores across FTS and vector are incomparable.
    candidates=[]
    for i in range(max((len(r) for r in results),default=0)):
        for leg in results:
            if i<len(leg): candidates.append(leg[i])
    selected=[];seen=set();seen_text=set();seen_artifacts=set()
    for hit in candidates:
        if len(selected)>=top_k: break
        if hit.get('kind'):
            if not await validate_library_passage(ws_id,hit,conversation_id):continue
        else:
            kb_id=hit['kb_id']; filename=hit['filename']
            file=files.get((kb_id,filename))
            if not file: continue
            from pathlib import Path
            if not Path(file['filepath']).is_file():continue
            mapping=next((f for f in mappings if f['kb_id']==kb_id and f['filename']==filename),None)
            latest=await indexing_jobs.latest(kb_id,filename)
            # A failed replacement retains the old index; don't attribute it to the new file.
            if latest and latest['state'] not in {'succeeded','partial'}: continue
            if mapping:
                member=await source(ws_id,mapping['kind'],mapping['source_id'])
                if not member or not member['enabled']:continue
                if mapping['kind']=='artifact':
                    artifact=await db.get_artifact(mapping['source_id'])
                    if not artifact or artifact_revision(artifact)!=mapping['revision']:continue
                hit={**hit,'kind':mapping['kind'],'source_id':mapping['source_id'],'revision':mapping['revision'],
                     'filename':mapping['title'],'url':destination('artifact',mapping['source_id']) if mapping['kind']=='artifact' else destination('document',str(file['id']),kb_id,chunk=hit.get('chunk_index',0))}
            else:
                if kb_id in internal_ids: continue
                if kb_id not in persona_kbs:
                    member=await source(ws_id,'knowledge',kb_id)
                    if not member or not member['enabled']:continue
                hit={**hit,'kind':'document','source_id':str(file['id']),'revision':file['created_at'],
                     'url':destination('document',str(file['id']),kb_id,chunk=hit.get('chunk_index',0))}
            if latest:
                job=await indexing_jobs.get(latest['job_id'])
                try:
                    current_sha=await asyncio.wait_for(asyncio.to_thread(indexing_jobs.digest,file['filepath']),timeout=max(.001,deadline-time.monotonic()))
                except (OSError,asyncio.TimeoutError):
                    partial=True;continue
                if current_sha!=job['sha256']:continue
                if mapping and mapping['kind']=='artifact':
                    from artifact_files import artifact_path_for_row
                    original,_=artifact_path_for_row(artifact)
                    try:
                        original_sha=await asyncio.wait_for(asyncio.to_thread(indexing_jobs.digest,original),timeout=max(.001,deadline-time.monotonic())) if original else None
                    except (OSError,asyncio.TimeoutError):
                        partial=True;continue
                    if original_sha!=mapping['sha256']:continue
                hit['revision']=job['sha256']
                hit['partial']=latest['state']=='partial'
        key=(hit['kind'],hit['source_id'],hit.get('chunk_index'),hit['text'].strip())
        normalized=' '.join(hit['text'].split())
        if key in seen or normalized in seen_text or (hit['kind']=='artifact' and hit['source_id'] in seen_artifacts):continue
        seen.add(key);seen_text.add(normalized)
        if hit['kind']=='artifact':seen_artifacts.add(hit['source_id'])
        selected.append(hit)
    # Recheck the workspace-wide switch after awaiting providers.
    if ws_id and not (await require_workspace(ws_id))['source_context_enabled']:
        selected=[h for h in selected if h.get('kb_id') in persona_kbs]
    context,sources=format_passages(selected,max_chars)
    return context,sources,partial


def format_passages(passages, max_chars):
    """Budget headers and separators too; citations describe only supplied text."""
    import rag
    parts=[];sources=[];remaining=max_chars
    for hit in passages:
        location=hit.get('location') or rag.location_from_text(hit['text'])
        header=f"[{len(sources)+1}] {hit['filename']} ({location or hit.get('kind','document')})\n"
        separator='\n\n---\n\n' if parts else ''
        available=remaining-len(header)-len(separator)
        if available<=0:break
        text=hit['text'][:available]
        if not text.strip():continue
        part=separator+header+text
        parts.append(part);remaining-=len(part)
        sources.append({k:v for k,v in {**hit,'n':len(sources)+1,'location':location,'snippet':text[:300],
            'truncated':hit.get('truncated',False) or len(text)<len(hit['text'])}.items() if k not in {'text','body','excerpt'}})
    return ''.join(parts),sources


_artifact_index_locks={}


async def prepare_artifact(ws_id, identity):
    import asyncio
    from routes.context import route_context
    key=(ws_id,identity)
    lock=_artifact_index_locks.setdefault(key,asyncio.Lock())
    try:
        async with lock:
            return await index_artifact(ws_id,identity,route_context().track_bg)
    except (OSError,ValueError,LookupError) as exc:
        # Job failures themselves are durable. Missing originals remain unindexed.
        print(f'[Workspace] Cannot prepare artifact {identity}: {exc}')
    finally:
        if not lock.locked() and not getattr(lock,'_waiters',None):
            _artifact_index_locks.pop(key,None)


async def prepare_workspace_artifacts(ws_id):
    """Schedule missing immutable file revisions outside the interactive retrieval budget."""
    offset=0
    while True:
        try:
            rows=await source_rows(ws_id,kind='artifact',limit=100,offset=offset)
        except LookupError:
            return
        for row in rows[:100]:
            if row['enabled']:
                await prepare_artifact(ws_id,row['source_id'])
        if len(rows)<=100:return
        offset+=100
