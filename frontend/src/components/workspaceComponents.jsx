import React,{useEffect,useRef,useState} from 'react';
import {apiJson} from '../api.js';
import NotesPanel from '../panels/NotesPanel.jsx';
import TasksPanel from '../panels/TasksPanel.jsx';
import JobsPanel from '../panels/JobsPanel.jsx';

export function WorkspaceHome({t,btnS,inputS,workspaces,onOpen,onCreated,onNewChat,notify}){
  const [name,setName]=useState(''),[busy,setBusy]=useState(false);
  const create=async()=>{if(!name.trim()||busy)return;setBusy(true);try{const ws=await apiJson('/api/workspaces',{method:'POST',body:{name}});setName('');onCreated(ws);}catch(e){notify({type:'error',text:'Workspace not created',detail:e.message});}finally{setBusy(false);}};
  return <section style={{padding:20,overflowY:'auto',color:t.text}}><h2>Workspaces</h2><p style={{color:t.mut}}>Chats, knowledge, notes and ongoing work for each project.</p>
    <form onSubmit={e=>{e.preventDefault();create();}} style={{display:'flex',gap:8,flexWrap:'wrap',marginBottom:20}}><input aria-label="Workspace name" value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder="Project name" style={{...inputS,flex:'1 1 180px'}}/><button disabled={busy||!name.trim()} style={btnS(t.acc)}>New workspace</button></form>
    {!workspaces.length&&<p>Create a workspace, then start a chat or add existing content.</p>}
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(230px,100%),1fr))',gap:12}}>{workspaces.map(w=><article key={w.id} style={{border:`1px solid ${t.brd}`,borderRadius:8,padding:16}}><button style={{...btnS(t.acc),textAlign:'left'}} onClick={()=>onOpen(w.id)}><strong>{w.name}</strong></button><p>{w.description}</p><span>{w.conv_count||0} chats · {w.report_count||0} reports · {w.file_count||0} artifacts</span><WorkspaceDefaultSummary ws={w} t={t}/><button style={btnS(t.acc)} onClick={()=>onNewChat(w.id)}>New chat in this workspace</button></article>)}</div>
  </section>;
}

export function KnowledgeReadiness({kbId,t,btnS,inputS,onReady}){
  const [kb,setKb]=useState(null),[error,setError]=useState(''),[query,setQuery]=useState(''),[chunks,setChunks]=useState(null),[busy,setBusy]=useState(false);
  const epoch=useRef(0);
  useEffect(()=>{const version=++epoch.current,c=new AbortController();let pending=false;const load=async()=>{if(pending)return;pending=true;try{const d=await apiJson(`/api/knowledge-bases/${kbId}/readiness`,{signal:c.signal});if(version===epoch.current){setKb(d);setError('');onReady?.(d.ready);}}catch(e){if(e.name!=='AbortError'&&version===epoch.current)setError(e.message);}finally{pending=false;}};load();const timer=setInterval(load,5000);return()=>{epoch.current++;c.abort();clearInterval(timer);};},[kbId]);
  const index=async f=>{setError('');try{await apiJson(`/api/knowledge-bases/${kbId}/files/${f.id}/index`,{method:'POST'});setKb(p=>({...p,ready:false,files:p.files.map(x=>x.id===f.id?{...x,indexing:{status:'indexing'}}:x)}));onReady?.(false);}catch(e){setError(e.message);}};
  const probe=async()=>{setBusy(true);setError('');try{setChunks((await apiJson('/api/knowledge-bases/query',{method:'POST',body:{kb_ids:[kbId],query}})).chunks);}catch(e){setError(e.message);}finally{setBusy(false);}};
  return <div style={{fontSize:12,marginTop:8}}>{error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {!kb?<p>Checking knowledge readiness…</p>:kb.files.map(f=><div key={f.id} style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center',margin:'8px 0'}}><span style={{overflowWrap:'anywhere'}}>{f.filename}: <strong>{({done:'Ready',indexing:'Indexing',partial:'Partial — review warnings',error:'Needs attention',unknown:'Not verified'})[f.indexing.status]||f.indexing.status}</strong>{f.indexing.chunks!=null&&` · ${f.indexing.chunks} passages`}</span>
      {f.indexing.error&&<span style={{color:t.err}}>{f.indexing.error}</span>}{f.indexing.warnings?.map(w=><span key={w} style={{color:t.warm}}>{w}</span>)}
      {f.indexing.status!=='indexing'&&<button style={btnS(t.acc)} onClick={()=>index(f)}>{f.indexing.status==='done'?'Reindex':'Index / retry'}</button>}</div>)}
    <details><summary style={{cursor:'pointer'}}>Test knowledge</summary><div style={{display:'flex',gap:8,flexWrap:'wrap',marginTop:8}}><input aria-label="Knowledge query" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Ask something in these sources" style={{...inputS,flex:'1 1 180px'}}/><button disabled={busy||!query.trim()} style={btnS(t.acc)} onClick={probe}>Find passages</button></div>
      {chunks&&(!chunks.length?<p>No matching passages. Check indexing status or try another query.</p>:chunks.map((c,i)=><blockquote key={i} style={{margin:'10px 0',padding:10,borderLeft:`3px solid ${t.acc}`,whiteSpace:'pre-wrap'}}><strong>{c.filename} {c.location||''}</strong><div>{c.text}</div></blockquote>))}</details>
  </div>;
}

export function WorkspaceKnowledge({ws,t,btnS,inputS,onPersonaCreated,models,notify}){
  const [items,setItems]=useState([]),[available,setAvailable]=useState([]),[error,setError]=useState(''),[ready,setReady]=useState({}),[busy,setBusy]=useState(false),[page,setPage]=useState(0),[more,setMore]=useState(false);
  const version=useRef(0), actionPending=useRef(false);
  const load=async(signal)=>{const v=version.current;const [linked,kbs]=await Promise.all([apiJson(`/api/workspaces/${ws.id}/content/knowledge?offset=${page*50}`,{signal}),apiJson('/api/knowledge-bases',{signal})]);if(v!==version.current)return;setItems(linked.items);setMore(linked.has_more);setAvailable(kbs);};
  useEffect(()=>{version.current++;const c=new AbortController();load(c.signal).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>{version.current++;c.abort();};},[ws.id,page]);
  const action=async fn=>{if(actionPending.current)return;actionPending.current=true;setBusy(true);setError('');try{await fn();await load();}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{actionPending.current=false;setBusy(false);}};
  const agent=async kb=>{const v=version.current;await action(async()=>{const current=await apiJson(`/api/knowledge-bases/${kb.id}/readiness`);if(!current.ready)throw new Error('Wait until every snapshot file is indexed.');const mc=await apiJson('/api/model-configs',{method:'POST',body:{name:`${ws.name} Agent`,base_model:models[0]||'',system_prompt:`Use the attached knowledge snapshot from ${ws.name}. It reflects the sources at snapshot creation, not later changes.`,kb_ids:[kb.id],tool_ids:[],parameters:{profile_type:'agent',workspace_snapshot_kb_id:kb.id}}});if(v===version.current)onPersonaCreated(mc);});};
  return <div><p>Snapshots are explicit copies of bounded excerpts. Live sources are managed above.</p>
    <div style={{display:'flex',gap:8,flexWrap:'wrap'}}><select aria-label="Link knowledge base" value="" disabled={busy} style={inputS} onChange={e=>{const id=e.target.value;if(id)action(()=>apiJson(`/api/workspaces/${ws.id}/links/knowledge/${id}`,{method:'POST'}));}}><option value="">Link an existing knowledge base…</option>{available.filter(k=>!items.some(i=>i.id===k.id)).map(k=><option key={k.id} value={k.id}>{k.name}</option>)}</select>
      <button disabled={busy} style={btnS(t.acc)} onClick={()=>action(()=>apiJson(`/api/workspaces/${ws.id}/create-kb`,{method:'POST',body:{name:`${ws.name} snapshot`}}))}>Create knowledge snapshot</button></div>
    <p style={{color:t.mut,fontSize:12}}>Snapshots include bounded excerpts from linked chats and reports. They are not automatically refreshed.</p>{error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {items.map(k=><article key={k.id} style={{border:`1px solid ${t.brd}`,borderRadius:8,padding:14,marginTop:12}}><strong>{k.name}</strong>
      {k.snapshot?.created_at&&<p style={{fontSize:12,color:t.mut}}>Snapshot {k.snapshot.created_at} · {k.snapshot.conversation_ids.length} chats · {k.snapshot.report_ids.length} reports{k.snapshot.truncated?' · Excerpts truncated':''}{k.snapshot.omitted_conversations||k.snapshot.omitted_reports?` · Omitted: ${k.snapshot.omitted_conversations} chats, ${k.snapshot.omitted_reports} reports`:''}</p>}
      <KnowledgeReadiness kbId={k.id} t={t} btnS={btnS} inputS={inputS} onReady={value=>setReady(p=>({...p,[k.id]:value}))}/>
      <div style={{display:'flex',gap:8,flexWrap:'wrap',marginTop:10}}>{k.snapshot?.created_at&&<button disabled={busy||!ready[k.id]} onClick={()=>agent(k)} style={btnS(t.acc)}>Create agent from snapshot</button>}<button disabled={busy} style={btnS(t.mut)} onClick={()=>action(()=>apiJson(`/api/workspaces/${ws.id}/links/knowledge/${k.id}`,{method:'DELETE'}))}>Unlink</button></div></article>)}
    <div style={{display:'flex',gap:8,marginTop:10}}><button disabled={busy||!page} onClick={()=>setPage(p=>p-1)} style={btnS(t.acc)}>Previous</button><button disabled={busy||!more} onClick={()=>setPage(p=>p+1)} style={btnS(t.acc)}>Next</button></div>
  </div>;
}

export function WorkspaceDaily({tab,ws,t,btnS,inputS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate}){
  const [taskTab,setTaskTab]=useState('tasks');
  const cardS={padding:14,borderRadius:8,border:`1px solid ${t.brd}`,background:t.surface};
  return tab==='notes'?<NotesPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,workspaces,initialCreate}} workspaceId={ws.id}/>:tab==='tasks'?<TasksPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate}} workspaceId={ws.id} tab={taskTab} setTab={setTaskTab}/>:<JobsPanel key={ws.id} {...{t,btnS,inputS,onNavigate,workspaces}} workspaceId={ws.id}/>;
}

export function WorkspaceOverview({ws,t,btnS,onNewChat,onSearch,onTab,onOpenConv,onOpenReport,onNavigate}){
  const [data,setData]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();apiJson(`/api/workspaces/${ws.id}/overview`,{signal:c.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,ws.updated_at]);
  return <div><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><button style={btnS(t.acc)} onClick={onSearch}>Search this workspace</button><button style={btnS(t.acc)} onClick={()=>onTab('notes',true)}>New note / todo</button><button style={btnS(t.acc)} onClick={()=>onTab('tasks',true)}>New scheduled task</button><button style={btnS(t.acc)} onClick={()=>onTab('add')}>Add existing content</button></div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}{!data&&!error&&<p role="status">Loading project overview…</p>}
    <h3>Pinned content</h3>{data?.pinned.map(s=><p key={s.kind+s.source_id}><a style={{color:t.acc}} href={s.url}>{s.title}</a></p>)}{data&&!data.pinned.length&&<p>Pin sources for quick access here.</p>}
    <h3>Recent chats</h3>{ws.conversations.slice(0,5).map(c=><p key={c.id}><button style={btnS(t.acc)} onClick={()=>onOpenConv(c.id)}>{c.title||'New chat'}</button></p>)}{!ws.conversations.length&&<p>Start a project chat or add an existing conversation.</p>}
    <h3>Open todos</h3>{data?.todos.map(n=><p key={n.id}><button style={btnS(t.acc)} onClick={()=>onNavigate('notes',n.id)}>{n.title}{n.due_at?` · due ${n.due_at}`:''}</button></p>)}<button style={btnS(t.acc)} onClick={()=>onTab('notes')}>View notes and todos</button>
    <h3>Active jobs</h3>{data?.jobs.map(j=><p key={j.id}><button style={btnS(t.acc)} onClick={()=>onNavigate('jobs',j.id)}>{j.title} · {j.phase}</button></p>)}{data&&!data.jobs.length&&<p>No active work.</p>}
    <h3>Needs attention</h3>{!!data?.pending_memories&&<p><button style={btnS(t.acc)} onClick={()=>onTab('memory')}>{data.pending_memories} memory suggestions to review</button></p>}{data?.failures.map(j=><p key={j.id}><button style={btnS(t.acc)} onClick={()=>onNavigate('jobs',j.id)}>{j.title} · {j.status}</button></p>)}{data&&!data.pending_memories&&!data.failures.length&&<p>No items need attention.</p>}
    <h3>Recent reports and files</h3>{(ws.reports||[]).slice(0,3).map(r=><p key={r.id}><button style={btnS(t.acc)} onClick={()=>onOpenReport?.(r.id)}>{r.title||r.query}</button></p>)}{(ws.files||[]).slice(0,3).map(f=><p key={f.id}><button style={btnS(t.acc)} onClick={()=>onNavigate?.('artifacts',f.id)}>{f.filename}</button></p>)}
  </div>;
}

export function WorkspaceDefaults({ws,t,btnS,inputS,models=[],personas=[],tools=[],onSaved}){
  const [draft,setDraft]=useState(ws.chat_defaults||{}),[instructions,setInstructions]=useState(ws.instructions||''),[enabled,setEnabled]=useState(ws.source_context_enabled!==false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const save=async()=>{if(busy)return;setBusy(true);setError('');try{const saved=await apiJson(`/api/workspaces/${ws.id}`,{method:'PATCH',body:{instructions,chat_defaults:draft,source_context_enabled:enabled}});onSaved(saved);}catch(e){setError(e.message);}finally{setBusy(false);}};
  const field=(key,value)=>setDraft(p=>({...p,[key]:value}));
  return <section style={{maxWidth:720,display:'grid',gap:14}}><h3>Workspace context and chat defaults</h3><p>Instructions apply whenever workspace context is active, even with memory off. Defaults apply to new chats only.</p>
    <label>Instructions<textarea aria-label="Workspace instructions" style={{...inputS,minHeight:120}} maxLength={20000} value={instructions} onChange={e=>setInstructions(e.target.value)}/></label>
    <label><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/> Retrieve relevant passages from enabled sources automatically</label>
    <label>Model<select aria-label="Workspace default model" style={inputS} value={draft.model||''} onChange={e=>field('model',e.target.value||null)}><option value="">Inherit persona / app model</option>{draft.model&&!models.includes(draft.model)&&<option value={draft.model}>{draft.model} (unavailable)</option>}{models.map(m=><option key={m}>{m}</option>)}</select></label>
    <label>Persona<select aria-label="Workspace default persona" style={inputS} value={draft.model_config_id||''} onChange={e=>field('model_config_id',e.target.value||null)}><option value="">No default persona</option>{draft.model_config_id&&!personas.some(p=>p.id===draft.model_config_id)&&<option value={draft.model_config_id}>Unavailable persona — choose another</option>}{personas.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <label>Optional tools<select aria-label="Workspace tool inheritance" style={inputS} value={draft.tool_ids==null?'inherit':'explicit'} onChange={e=>field('tool_ids',e.target.value==='inherit'?null:[])}><option value="inherit">Inherit persona / app tools</option><option value="explicit">Choose tools (none selected disables optional tools)</option></select></label>
    {draft.tool_ids!=null&&<div style={{display:'flex',gap:12,flexWrap:'wrap'}}>{tools.map(tool=><label key={tool.id}><input type="checkbox" checked={draft.tool_ids.includes(tool.id)} onChange={e=>field('tool_ids',e.target.checked?[...draft.tool_ids,tool.id]:draft.tool_ids.filter(id=>id!==tool.id))}/>{tool.name||tool.id}</label>)}</div>}
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}<button disabled={busy} style={btnS(t.acc)} onClick={save}>{busy?'Saving…':'Save workspace settings'}</button>
  </section>;
}

export function WorkspaceDefaultSummary({ws,t}){
  const [defaults,setDefaults]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();setDefaults(null);setError('');apiJson(`/api/workspaces/${ws.id}/defaults`,{signal:c.signal}).then(setDefaults).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,JSON.stringify(ws.chat_defaults)]);
  return <p style={{fontSize:12,color:error?t.err:t.mut,overflowWrap:'anywhere'}} role={error?'alert':undefined}>{error||(!defaults?'Loading chat defaults…':`New chats: ${defaults.model} · ${defaults.persona_name||'No persona'} · ${defaults.tool_ids.length?defaults.tool_ids.join(', '):'No optional tools'}`)}</p>;
}

export function WorkspaceContentPicker({ws,t,btnS,inputS,onLinked}){
  const [kind,setKind]=useState('chat'),[query,setQuery]=useState(''),[page,setPage]=useState(0),[data,setData]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);setError('');const timer=setTimeout(()=>apiJson(`/api/workspaces/${ws.id}/picker/${kind}?query=${encodeURIComponent(query)}&limit=30&offset=${page*30}`,{signal:c.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);}),200);return()=>{epoch.current++;clearTimeout(timer);c.abort();};},[ws.id,kind,query,page,revision]);
  const link=async id=>{if(pending.current)return;pending.current=true;setBusy(true);const v=epoch.current;try{await apiJson(`/api/workspaces/${ws.id}/sources/${kind}/${encodeURIComponent(id)}/link`,{method:'POST'});if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);setRevision(p=>p+1);}}finally{pending.current=false;setBusy(false);}};
  return <section><h3>Add existing content</h3><p>Link original content to this workspace.</p><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><select aria-label="Content type" style={{...inputS,width:'auto'}} value={kind} onChange={e=>{setKind(e.target.value);setPage(0);}}>{[['chat','Chats'],['note','Notes'],['research','Reports'],['artifact','Artifacts'],['knowledge','Knowledge bases']].map(([id,label])=><option value={id} key={id}>{label}</option>)}</select><input aria-label="Search existing content" placeholder="Search by title…" style={{...inputS,flex:'1 1 180px'}} value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/></div>
    {error&&<p role="alert">{error}</p>}{!data&&!error&&<p role="status">Loading…</p>}{data?.items.length===0&&<p>No matching content.</p>}{data?.items.map(item=><div key={item.id} style={{display:'flex',gap:8,alignItems:'center',marginTop:10}}><span style={{flex:1,overflowWrap:'anywhere'}}>{item.title||'Untitled'}</span><button style={btnS(t.acc)} disabled={busy||item.linked} onClick={()=>link(item.id)}>{item.linked?'Linked':'Add'}</button></div>)}
    <div style={{display:'flex',gap:8,marginTop:12}}><button style={btnS(t.acc)} disabled={busy||page===0} onClick={()=>setPage(p=>p-1)}>Previous</button><button style={btnS(t.acc)} disabled={busy||!data?.has_more} onClick={()=>setPage(p=>p+1)}>Next</button></div>
  </section>;
}

export function WorkspaceSources({ws,t,btnS,inputS,onLinked,...snapshotProps}){
  const [data,setData]=useState(null),[page,setPage]=useState(0),[query,setQuery]=useState(''),[revision,setRevision]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState(''),[uploadProgress,setUploadProgress]=useState('');
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);let loading=false;const load=async()=>{if(loading)return;loading=true;try{setData(await apiJson(`/api/workspaces/${ws.id}/sources?offset=${page*30}&limit=30&query=${encodeURIComponent(query)}`,{signal:c.signal}));}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{loading=false;}};load();const timer=setInterval(load,5000);return()=>{epoch.current++;c.abort();clearInterval(timer);};},[ws.id,page,query,revision]);
  const action=async fn=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');const v=epoch.current;try{await fn();if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);setRevision(p=>p+1);}}finally{pending.current=false;setBusy(false);setUploadProgress('');}};
  const update=(s,body)=>{setData(p=>({...p,items:p.items.map(item=>item.kind===s.kind&&item.source_id===s.source_id?{...item,...body}:item)}));return action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${encodeURIComponent(s.source_id)}`,{method:'PATCH',body}));};
  return <section><p>Relevant passages from enabled sources are retrieved automatically. Pinning adds quick access; it does not insert entire sources into every prompt.</p>{ws.source_context_enabled===false&&<p>Automatic source context is off in workspace settings.</p>}
    <label style={{display:'block',margin:'12px 0'}}>Upload project documents <input aria-label="Upload workspace documents" type="file" multiple disabled={busy} onChange={e=>{const files=[...e.target.files];e.target.value='';action(async()=>{for(let i=0;i<files.length;i++){setUploadProgress(`Uploading ${i+1} of ${files.length}: ${files[i].name}`);const body=new FormData();body.append('file',files[i]);await apiJson(`/api/workspaces/${ws.id}/upload`,{method:'POST',body});}});}}/></label>
    {uploadProgress&&<p role="status">{uploadProgress}</p>}<input aria-label="Filter workspace sources" style={inputS} value={query} placeholder="Filter sources…" onChange={e=>{setQuery(e.target.value);setPage(0);}}/>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}{!data&&!error&&<p role="status">Loading sources…</p>}{data?.items.length===0&&<p>No sources yet. Upload a file or link existing content below.</p>}
    {data?.items.map(s=><article key={`${s.kind}:${s.source_id}`} style={{border:`1px solid ${t.brd}`,borderRadius:8,padding:12,marginTop:10,overflowWrap:'anywhere'}}><a href={s.url} style={{color:t.acc}}>{s.title||'Untitled'}</a><p style={{fontSize:12}}>{s.kind} · {s.can_use?'Chat can retrieve':!s.enabled||ws.source_context_enabled===false?'Excluded from chat':'Not ready for retrieval'} · {s.readiness}</p>
      {s.indexing?.phase&&<p role="status">{s.indexing.phase}{s.indexing.total?` · ${s.indexing.completed} / ${s.indexing.total} passages`:''}</p>}{s.indexing?.error&&<p role="alert">{s.indexing.error}</p>}{s.indexing?.warnings?.map(w=><p key={w}>{w}</p>)}
      <div style={{display:'flex',flexWrap:'wrap',gap:12}}><label><input type="checkbox" disabled={busy} checked={!!s.enabled} onChange={e=>update(s,{enabled:e.target.checked})}/>Use in chat</label><label><input type="checkbox" disabled={busy} checked={!!s.pinned} onChange={e=>update(s,{pinned:e.target.checked})}/>Pin</label>
        {s.indexing?.job_id&&['failed','partial','interrupted'].includes(s.indexing.state)&&<button disabled={busy} style={btnS(t.acc)} onClick={()=>action(()=>apiJson(`/api/jobs/indexing:${s.indexing.job_id}/actions/retry`,{method:'POST'}))}>Retry indexing</button>}
        {s.kind!=='document'&&<button disabled={busy} style={btnS(t.mut)} onClick={()=>action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${s.source_id}/link`,{method:'DELETE'}))}>Unlink</button>}
      </div>{s.kind==='knowledge'&&<KnowledgeReadiness kbId={s.source_id} {...{t,btnS,inputS}}/>}
    </article>)}
    <div style={{display:'flex',gap:8,margin:'12px 0'}}><button style={btnS(t.acc)} disabled={!page||busy} onClick={()=>setPage(p=>p-1)}>Previous</button><button style={btnS(t.acc)} disabled={!data?.has_more||busy} onClick={()=>setPage(p=>p+1)}>Next</button></div>
    <WorkspaceContentPicker {...{ws,t,btnS,inputS}} onLinked={async()=>{setRevision(p=>p+1);await onLinked?.();}}/>
    <details style={{marginTop:24}}><summary>Knowledge snapshots (explicit copies)</summary><WorkspaceKnowledge {...{ws,t,btnS,inputS,...snapshotProps}}/></details>
  </section>;
}
