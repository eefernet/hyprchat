import React,{useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import PanelHeader from './PanelHeader.jsx';
import SelectionControl from './SelectionControl.jsx';
import ActionMenu from './ActionMenu.jsx';
import ModelPicker from '../ModelPicker.jsx';
import useFocusScope from '../useFocusScope.js';
import {IC} from './icons.jsx';
import {apiJson} from '../api.js';
import NotesPanel from '../panels/NotesPanel.jsx';
import TasksPanel from '../panels/TasksPanel.jsx';
import JobsPanel from '../panels/JobsPanel.jsx';

export function WorkspaceHome({t,btnS,inputS,workspaces,onOpen,onCreated,onNewChat,notify}){
  const [name,setName]=useState(''),[busy,setBusy]=useState(false),[creating,setCreating]=useState(false);
  const create=async()=>{if(!name.trim()||busy)return;setBusy(true);try{const ws=await apiJson('/api/workspaces',{method:'POST',body:{name}});setName('');setCreating(false);onCreated(ws);}catch(e){notify({type:'error',text:'Workspace not created',detail:e.message});}finally{setBusy(false);}};
  return <section className="hc-workspace-home" style={{color:t.text}}>
    <PanelHeader t={t} icon={<IC.Layers/>} title="Workspaces" subtitle="Chats, sources and ongoing work for each project."><button className="hc-button is-primary" onClick={()=>setCreating(true)}><IC.Plus/> New workspace</button></PanelHeader>
    <div className="hc-workspace-body">
    {creating&&<form className="hc-workspace-card hc-toolbar" onSubmit={e=>{e.preventDefault();create();}}><input autoFocus aria-label="Workspace name" value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder="Project name" style={{...inputS,flex:'1 1 180px'}}/><button disabled={busy||!name.trim()} style={btnS(t.acc)}>{busy?'Creating…':'Create workspace'}</button><button type="button" style={btnS(t.mut)} onClick={()=>setCreating(false)}>Cancel</button></form>}
    {!workspaces.length&&<div className="hc-empty"><IC.Layers/><strong>Your projects start here</strong><p>Create a workspace to keep related chats, sources and notes together.</p></div>}
    <div className="hc-workspace-grid">{workspaces.map(w=><article key={w.id} className="hc-workspace-card"><button className="hc-workspace-title" onClick={()=>onOpen(w.id)}><strong>{w.name}</strong></button><p className="hc-secondary">{w.description||'Add sources and start a project chat.'}</p><p className="hc-secondary">{w.conv_count||0} chats · {w.report_count||0} reports · {w.file_count||0} artifacts</p><button style={btnS(t.acc)} onClick={()=>onNewChat(w.id)}><IC.Plus/> New chat in this workspace</button></article>)}</div>
    </div>
  </section>;
}

export function KnowledgeReadiness({kbId,t,btnS,inputS,onReady}){
  const [kb,setKb]=useState(null),[error,setError]=useState(''),[query,setQuery]=useState(''),[chunks,setChunks]=useState(null),[busy,setBusy]=useState(false);
  const epoch=useRef(0);
  useEffect(()=>{const version=++epoch.current,c=new AbortController();let pending=false;const load=async()=>{if(pending)return;pending=true;try{const d=await apiJson(`/api/knowledge-bases/${kbId}/readiness`,{signal:c.signal});if(version===epoch.current){setKb(d);setError('');onReady?.(d.ready);}}catch(e){if(e.name!=='AbortError'&&version===epoch.current)setError(e.message);}finally{pending=false;}};load();const timer=setInterval(load,5000);return()=>{epoch.current++;c.abort();clearInterval(timer);};},[kbId]);
  const index=async f=>{setError('');try{await apiJson(`/api/knowledge-bases/${kbId}/files/${f.id}/index`,{method:'POST'});setKb(p=>({...p,ready:false,files:p.files.map(x=>x.id===f.id?{...x,indexing:{status:'indexing'}}:x)}));onReady?.(false);}catch(e){setError(e.message);}};
  const probe=async()=>{setBusy(true);setError('');try{setChunks((await apiJson('/api/knowledge-bases/query',{method:'POST',body:{kb_ids:[kbId],query}})).chunks);}catch(e){setError(e.message);}finally{setBusy(false);}};
  return <div style={{fontSize:"var(--hc-text-secondary)",marginTop:8}}>{error&&<p role="alert" style={{color:t.err}}>{error}</p>}
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
    <div style={{display:'flex',gap:8,flexWrap:'wrap'}}><SelectionControl label="Link knowledge base" value="" disabled={busy} onChange={id=>{if(id)action(()=>apiJson(`/api/workspaces/${ws.id}/links/knowledge/${id}`,{method:'POST'}));}} options={[{value:'',label:'Link an existing knowledge base…'},...available.filter(k=>!items.some(i=>i.id===k.id)).map(k=>({value:k.id,label:k.name}))]}/>
      <button disabled={busy} style={btnS(t.acc)} onClick={()=>action(()=>apiJson(`/api/workspaces/${ws.id}/create-kb`,{method:'POST',body:{name:`${ws.name} snapshot`}}))}>Create knowledge snapshot</button></div>
    <p style={{color:t.mut,fontSize:"var(--hc-text-secondary)"}}>Snapshots include bounded excerpts from linked chats and reports. They are not automatically refreshed.</p>{error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {items.map(k=><article key={k.id} style={{border:`1px solid ${t.brd}`,borderRadius:"var(--hc-radius)",padding:14,marginTop:12}}><strong>{k.name}</strong>
      {k.snapshot?.created_at&&<p style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>Snapshot {k.snapshot.created_at} · {k.snapshot.conversation_ids.length} chats · {k.snapshot.report_ids.length} reports{k.snapshot.truncated?' · Excerpts truncated':''}{k.snapshot.omitted_conversations||k.snapshot.omitted_reports?` · Omitted: ${k.snapshot.omitted_conversations} chats, ${k.snapshot.omitted_reports} reports`:''}</p>}
      <KnowledgeReadiness kbId={k.id} t={t} btnS={btnS} inputS={inputS} onReady={value=>setReady(p=>({...p,[k.id]:value}))}/>
      <div style={{display:'flex',gap:8,flexWrap:'wrap',marginTop:10}}>{k.snapshot?.created_at&&<button disabled={busy||!ready[k.id]} onClick={()=>agent(k)} style={btnS(t.acc)}>Create agent from snapshot</button>}<button disabled={busy} style={btnS(t.mut)} onClick={()=>action(()=>apiJson(`/api/workspaces/${ws.id}/links/knowledge/${k.id}`,{method:'DELETE'}))}>Unlink</button></div></article>)}
    <div style={{display:'flex',gap:8,marginTop:10}}><button disabled={busy||!page} onClick={()=>setPage(p=>p-1)} style={btnS(t.acc)}>Previous</button><button disabled={busy||!more} onClick={()=>setPage(p=>p+1)} style={btnS(t.acc)}>Next</button></div>
  </div>;
}

export function WorkspaceDaily({tab,ws,t,btnS,inputS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate,onAddContent,refreshKey}){
  const [taskTab,setTaskTab]=useState('tasks');
  const cardS={padding:14,borderRadius:"var(--hc-radius)",border:`1px solid ${t.brd}`,background:t.surface};
  return tab==='notes'?<NotesPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,workspaces,initialCreate,onAddContent,refreshKey}} embedded workspaceId={ws.id}/>:tab==='tasks'?<TasksPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate}} embedded workspaceId={ws.id} tab={taskTab} setTab={setTaskTab}/>:<JobsPanel key={ws.id} {...{t,btnS,inputS,onNavigate,workspaces}} embedded workspaceId={ws.id}/>;
}

export function WorkspaceOverview({ws,t,btnS,onTab,onOpenConv,onOpenReport,onNavigate}){
  const [data,setData]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();apiJson(`/api/workspaces/${ws.id}/overview`,{signal:c.signal}).then(d=>{setData(d);setError('');}).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,ws.updated_at]);
  const card=(title,action,body)=><section className="hc-workspace-card"><div className="hc-workspace-section-heading"><h2>{title}</h2>{action}</div>{body}</section>;
  const link=(label,fn)=><button className="hc-workspace-link" onClick={fn}>{label}</button>;
  return <div>
    <div className="hc-toolbar hc-workspace-shortcuts"><button style={btnS(t.acc)} onClick={()=>onTab('notes',true)}><IC.Plus/> New note / todo</button><button style={btnS(t.acc)} onClick={()=>onTab('tasks',true)}>New scheduled task</button><button style={btnS(t.acc)} onClick={event=>onTab('add',false,event)}>Add existing content</button></div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}{!data&&!error&&<p role="status">Loading project overview…</p>}
    <div className="hc-workspace-overview"><div>
      {card('Recent chats',link('View all',()=>onTab('convs')),<>{(ws.conversations||[]).slice(0,5).map(c=><div className="hc-workspace-list-row" key={c.id}>{link(c.title||'New chat',()=>onOpenConv(c.id))}</div>)}{!ws.conversations?.length&&<p className="hc-secondary">Start a project chat using the button above, or add an existing conversation.</p>}</>)}
      {card('Pinned content',link('Manage sources',()=>onTab('knowledge')),<>{data?.pinned.map(s=><div className="hc-workspace-list-row" key={s.kind+s.source_id}><a href={s.url}>{s.title}</a></div>)}{data&&!data.pinned.length&&<p className="hc-secondary">Pin useful sources to keep them close at hand.</p>}</>)}
      {card('Recent reports and artifacts',link('View sources',()=>onTab('reports')),<>{(ws.reports||[]).slice(0,3).map(r=><div className="hc-workspace-list-row" key={r.id}>{link(r.title||r.query,()=>onOpenReport?.(r.id))}</div>)}{(ws.files||[]).slice(0,3).map(f=><div className="hc-workspace-list-row" key={f.id}>{link(f.filename,()=>onNavigate?.('artifacts',f.id))}</div>)}{!ws.reports?.length&&!ws.files?.length&&<p className="hc-secondary">Reports and files linked to this project will appear here.</p>}</>)}
    </div><aside>
      {card('Open todos',link('View notes',()=>onTab('notes')),<>{data?.todos.map(n=><div className="hc-workspace-list-row" key={n.id}>{link(n.title,()=>onNavigate('notes',n.id))}{n.due_at&&<small>Due {n.due_at}</small>}</div>)}{data&&!data.todos.length&&<p className="hc-secondary">No open todos. Add one to track your next step.</p>}</>)}
      {card('Active work',link('View activity',()=>onTab('jobs')),<>{data?.jobs.map(j=><div className="hc-workspace-list-row" key={j.id}>{link(j.title,()=>onNavigate('jobs',j.id))}<small>{j.phase}</small></div>)}{data&&!data.jobs.length&&<p className="hc-secondary">No active work.</p>}</>)}
      {card('Needs attention',null,<>{!!data?.pending_memories&&link(`${data.pending_memories} memory suggestions to review`,()=>onTab('memory'))}{data?.failures.map(j=><div className="hc-workspace-list-row" key={j.id}>{link(j.title,()=>onNavigate('jobs',j.id))}<small>{j.status}</small></div>)}{data&&!data.pending_memories&&!data.failures.length&&<p className="hc-secondary">Nothing needs attention.</p>}</>)}
    </aside></div>
  </div>;
}

export function WorkspaceDefaults({ws,t,btnS,inputS,models=[],personas=[],tools=[],onSaved}){
  const [draft,setDraft]=useState(ws.chat_defaults||{}),[instructions,setInstructions]=useState(ws.instructions||''),[enabled,setEnabled]=useState(ws.source_context_enabled!==false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false);
  const save=async()=>{if(busy)return;setBusy(true);setError('');setSaved(false);try{const saved=await apiJson(`/api/workspaces/${ws.id}`,{method:'PATCH',body:{instructions,chat_defaults:draft,source_context_enabled:enabled}});onSaved(saved);setSaved(true);}catch(e){setError(e.message);}finally{setBusy(false);}};
  const field=(key,value)=>{setSaved(false);setDraft(p=>({...p,[key]:value}));};
  return <section className="hc-workspace-card hc-workspace-settings"><h2>Instructions</h2><p>Instructions apply whenever workspace context is active, even with memory off. Defaults apply to new chats only.</p>
    <label>Instructions<textarea aria-label="Workspace instructions" style={{...inputS,minHeight:120}} maxLength={20000} value={instructions} onChange={e=>{setSaved(false);setInstructions(e.target.value);}}/></label>
    <h2>Automatic source context</h2><label className="hc-check-row"><input type="checkbox" checked={enabled} onChange={e=>{setSaved(false);setEnabled(e.target.checked);}}/> Retrieve relevant passages from enabled sources automatically</label>
    <h2>New-chat defaults</h2><div><span className="hc-field-label">Model</span><div className="hc-toolbar"><ModelPicker label="Workspace default model" placeholder="Inherit persona / app model" value={draft.model||''} onChange={value=>field('model',value)} models={[...new Set([...models,...(draft.model?[draft.model]:[])])]} t={t} font="inherit"/><button style={btnS(t.mut)} onClick={()=>field('model',null)}>Inherit persona / app model</button></div></div>
    <div><span className="hc-field-label">Persona</span><SelectionControl label="Workspace default persona" value={draft.model_config_id||''} onChange={v=>field('model_config_id',v||null)} options={[{value:'',label:'No default persona'},...(draft.model_config_id&&!personas.some(p=>p.id===draft.model_config_id)?[{value:draft.model_config_id,label:'Unavailable persona — choose another'}]:[]),...personas.map(p=>({value:p.id,label:p.name}))]}/></div>
    <div><span className="hc-field-label">Optional tools</span><SelectionControl label="Workspace tool inheritance" value={draft.tool_ids==null?'inherit':'explicit'} onChange={v=>field('tool_ids',v==='inherit'?null:[])} options={[{value:'inherit',label:'Inherit persona / app tools'},{value:'explicit',label:'Choose tools (none disables optional tools)'}]}/></div>
    {draft.tool_ids!=null&&<div style={{display:'flex',gap:12,flexWrap:'wrap'}}>{tools.map(tool=><label className="hc-check-row" key={tool.id}><input type="checkbox" checked={draft.tool_ids.includes(tool.id)} onChange={e=>field('tool_ids',e.target.checked?[...draft.tool_ids,tool.id]:draft.tool_ids.filter(id=>id!==tool.id))}/>{tool.name||tool.id}</label>)}</div>}
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}<button disabled={busy} style={btnS(t.acc)} onClick={save}>{busy?'Saving…':'Save workspace settings'}</button>{saved&&<p role="status" style={{color:t.ok}}>Workspace settings saved.</p>}
  </section>;
}

export function WorkspaceDefaultSummary({ws,t}){
  const [defaults,setDefaults]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();setDefaults(null);setError('');apiJson(`/api/workspaces/${ws.id}/defaults`,{signal:c.signal}).then(setDefaults).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,JSON.stringify(ws.chat_defaults)]);
  return <p style={{fontSize:"var(--hc-text-secondary)",color:error?t.err:t.mut,overflowWrap:'anywhere'}} role={error?'alert':undefined}>{error||(!defaults?'Loading chat defaults…':`New chats: ${defaults.model} · ${defaults.persona_name||'No persona'} · ${defaults.tool_ids.length?defaults.tool_ids.join(', '):'No optional tools'}`)}</p>;
}

export function WorkspaceContentPicker({ws,t,btnS,inputS,onLinked}){
  const [kind,setKind]=useState('chat'),[query,setQuery]=useState(''),[page,setPage]=useState(0),[data,setData]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);setError('');const timer=setTimeout(()=>apiJson(`/api/workspaces/${ws.id}/picker/${kind}?query=${encodeURIComponent(query)}&limit=30&offset=${page*30}`,{signal:c.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);}),200);return()=>{epoch.current++;clearTimeout(timer);c.abort();};},[ws.id,kind,query,page,revision]);
  const link=async id=>{if(pending.current)return;pending.current=true;setBusy(true);const v=epoch.current;try{await apiJson(`/api/workspaces/${ws.id}/sources/${kind}/${encodeURIComponent(id)}/link`,{method:'POST'});if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);setRevision(p=>p+1);}}finally{pending.current=false;setBusy(false);}};
  return <section><h3>Add existing content</h3><p>Link original content to this workspace.</p><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><SelectionControl label="Content type" value={kind} onChange={v=>{setKind(v);setPage(0);}} options={[['chat','Chats'],['note','Notes'],['research','Reports'],['artifact','Artifacts'],['knowledge','Knowledge bases']].map(([value,label])=>({value,label}))}/><input aria-label="Search existing content" placeholder="Search by title…" style={{...inputS,flex:'1 1 180px'}} value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/></div>
    {error&&<p role="alert">{error}</p>}{!data&&!error&&<p role="status">Loading…</p>}{data?.items.length===0&&<p>No matching content.</p>}{data?.items.map(item=><div key={item.id} style={{display:'flex',gap:8,alignItems:'center',marginTop:10}}><span style={{flex:1,overflowWrap:'anywhere'}}>{item.title||'Untitled'}</span><button style={btnS(t.acc)} disabled={busy||item.linked} onClick={()=>link(item.id)}>{item.linked?'Linked':'Add'}</button></div>)}
    {(page>0||data?.has_more)&&<div style={{display:'flex',gap:8,marginTop:12}}><button style={btnS(t.acc)} disabled={busy||page===0} onClick={()=>setPage(p=>p-1)}>Previous</button><button style={btnS(t.acc)} disabled={busy||!data?.has_more} onClick={()=>setPage(p=>p+1)}>Next</button></div>}
  </section>;
}

export function WorkspaceSources({ws,t,btnS,inputS,onLinked,onAdd,refreshKey,...snapshotProps}){
  const [data,setData]=useState(null),[page,setPage]=useState(0),[query,setQuery]=useState(''),[revision,setRevision]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState(''),[uploadProgress,setUploadProgress]=useState('');
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);let loading=false;const load=async()=>{if(loading)return;loading=true;try{setData(await apiJson(`/api/workspaces/${ws.id}/sources?offset=${page*30}&limit=30&query=${encodeURIComponent(query)}`,{signal:c.signal}));setError('');}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{loading=false;}};load();const timer=setInterval(load,5000);return()=>{epoch.current++;c.abort();clearInterval(timer);};},[ws.id,page,query,revision,refreshKey]);
  const action=async (fn,onFailure)=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');const v=epoch.current;try{await fn();if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);onFailure?.();}}finally{pending.current=false;setBusy(false);setUploadProgress('');}};
  const update=(s,body)=>{if(pending.current)return;const previous=data;setData(p=>({...p,items:p.items.map(item=>item.kind===s.kind&&item.source_id===s.source_id?{...item,...body}:item)}));return action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${encodeURIComponent(s.source_id)}`,{method:'PATCH',body}),()=>setData(previous));};
  return <section><p>Relevant passages from enabled sources are retrieved automatically. Pinning adds quick access; it does not insert entire sources into every prompt.</p>{ws.source_context_enabled===false&&<p>Automatic source context is off in workspace settings.</p>}
    <div className="hc-toolbar hc-workspace-source-toolbar"><input aria-label="Filter workspace sources" style={{...inputS,flex:'1 1 180px'}} value={query} placeholder="Filter sources…" onChange={e=>{setQuery(e.target.value);setPage(0);}}/>
      <label className="hc-button hc-upload-button">Upload documents<input aria-label="Upload workspace documents" type="file" multiple disabled={busy} onChange={e=>{const files=[...e.target.files];e.target.value='';action(async()=>{for(let i=0;i<files.length;i++){setUploadProgress(`Uploading ${i+1} of ${files.length}: ${files[i].name}`);const body=new FormData();body.append('file',files[i]);await apiJson(`/api/workspaces/${ws.id}/upload`,{method:'POST',body});}});}}/></label>
      <button style={btnS(t.acc)} onClick={onAdd}>Add existing content</button></div>
    {uploadProgress&&<p role="status">{uploadProgress}</p>}
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}{!data&&!error&&<p role="status">Loading sources…</p>}{data?.items.length===0&&<div className="hc-empty">{query?'No sources match this filter.':'Upload documents or add existing content to give this project useful context.'}</div>}
    {data?.items.map(s=><article key={`${s.kind}:${s.source_id}`} className="hc-workspace-card hc-source-row"><div className="hc-source-main"><a href={s.url}>{s.title||'Untitled'}</a><div className="hc-source-meta"><span>{s.kind}</span><span>{s.readiness}</span><span>{!s.enabled||ws.source_context_enabled===false?'Excluded from chat':s.can_use?'Chat can retrieve':'Not ready for retrieval'}</span></div></div>
      <div className="hc-toolbar"><label className="hc-check-row"><input type="checkbox" disabled={busy} checked={!!s.enabled} onChange={e=>update(s,{enabled:e.target.checked})}/>Use in chat</label><label className="hc-check-row"><input type="checkbox" disabled={busy} checked={!!s.pinned} onChange={e=>update(s,{pinned:e.target.checked})}/>Pin</label>
      <ActionMenu compact label={`Actions for ${s.title||'source'}`} items={[
        s.indexing?.job_id&&['failed','partial','interrupted'].includes(s.indexing.state)&&{label:'Retry indexing',disabled:busy,onClick:()=>action(()=>apiJson(`/api/jobs/indexing:${s.indexing.job_id}/actions/retry`,{method:'POST'}))},
        s.kind!=='document'&&{label:'Unlink',disabled:busy,onClick:()=>action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${encodeURIComponent(s.source_id)}/link`,{method:'DELETE'}))},
        {label:'View source',onClick:()=>{window.location.hash=s.url.startsWith('#')?s.url:window.location.hash; if(!s.url.startsWith('#'))window.open(s.url,'_blank','noopener');}}
      ]}/></div>
      {(s.indexing||s.kind==='knowledge')&&<details className="hc-source-details"><summary>Indexing details{s.indexing?.error?' · Needs attention':''}</summary>{s.indexing?.phase&&<p role="status">{s.indexing.phase}{s.indexing.total?` · ${s.indexing.completed} / ${s.indexing.total} passages`:''}</p>}{s.indexing?.error&&<p role="alert">{s.indexing.error}</p>}{s.indexing?.warnings?.map(w=><p key={w}>{w}</p>)}{s.kind==='knowledge'&&<KnowledgeReadiness kbId={s.source_id} {...{t,btnS,inputS}}/>}</details>}
    </article>)}
    {(page>0||data?.has_more)&&<div className="hc-toolbar"><button style={btnS(t.acc)} disabled={!page||busy} onClick={()=>setPage(p=>p-1)}>Previous</button><button style={btnS(t.acc)} disabled={!data?.has_more||busy} onClick={()=>setPage(p=>p+1)}>Next</button></div>}
    <details style={{marginTop:24}}><summary>Knowledge snapshots (explicit copies)</summary><WorkspaceKnowledge {...{ws,t,btnS,inputS,...snapshotProps}}/></details>
  </section>;
}

export function WorkspaceContentDialog({onClose,...props}){
  const focus=useFocusScope(true,onClose);
  return createPortal(<div className="hc-workspace-modal" onClick={e=>{if(e.target===e.currentTarget)onClose();}}><div ref={focus} role="dialog" aria-modal="true" aria-label="Add existing content" tabIndex={-1} className="hc-workspace-dialog"><button className="hc-button" onClick={onClose} style={{float:'right'}}>Close</button><WorkspaceContentPicker {...props}/></div></div>,document.body);
}
