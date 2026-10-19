import React,{useEffect,useRef,useState} from 'react';
import {apiJson} from '../api.js';
import NotesPanel from '../panels/NotesPanel.jsx';
import TasksPanel from '../panels/TasksPanel.jsx';
import JobsPanel from '../panels/JobsPanel.jsx';

export function WorkspaceHome({t,btnS,inputS,workspaces,onOpen,onCreated,notify}){
  const [name,setName]=useState(''),[busy,setBusy]=useState(false);
  const create=async()=>{if(!name.trim()||busy)return;setBusy(true);try{const ws=await apiJson('/api/workspaces',{method:'POST',body:{name}});setName('');onCreated(ws);}catch(e){notify({type:'error',text:'Workspace not created',detail:e.message});}finally{setBusy(false);}};
  return <section style={{padding:20,overflowY:'auto',color:t.text}}><h2>Workspaces</h2><p style={{color:t.mut}}>Chats, knowledge, notes and ongoing work for each project.</p>
    <form onSubmit={e=>{e.preventDefault();create();}} style={{display:'flex',gap:8,flexWrap:'wrap',marginBottom:20}}><input aria-label="Workspace name" value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder="Project name" style={{...inputS,flex:'1 1 180px'}}/><button disabled={busy||!name.trim()} style={btnS(t.acc)}>New workspace</button></form>
    {!workspaces.length&&<p>Create a workspace, then start a chat or add existing content.</p>}
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(230px,1fr))',gap:12}}>{workspaces.map(w=><button key={w.id} style={{...btnS(t.acc),display:'block',textAlign:'left',padding:16}} onClick={()=>onOpen(w.id)}><strong>{w.name}</strong><p>{w.description}</p><span>{w.conv_count||0} chats · {w.report_count||0} reports · {w.file_count||0} artifacts</span></button>)}</div>
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
  return <div><p>Linked knowledge is retrieved in workspace chats. Organizing other content here does not automatically add it to the AI context.</p>
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
  const [notes,setNotes]=useState([]),[jobs,setJobs]=useState([]),[knowledge,setKnowledge]=useState([]),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();Promise.all([apiJson(`/api/notes?workspace_id=${ws.id}&kind=todo&include_done=false&limit=5`,{signal:c.signal}),apiJson(`/api/jobs?workspace_id=${ws.id}&status=active&limit=5`,{signal:c.signal}),apiJson(`/api/workspaces/${ws.id}/content/knowledge?limit=5`,{signal:c.signal})]).then(([n,j,k])=>{setNotes(n);setJobs(j.jobs);setKnowledge(k.items);}).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id]);
  return <div><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><button style={btnS(t.acc)} onClick={onNewChat}>New chat</button><button style={btnS(t.acc)} onClick={onSearch}>Search this workspace</button><button style={btnS(t.acc)} onClick={()=>onTab('notes',true)}>New note / todo</button><button style={btnS(t.acc)} onClick={()=>onTab('tasks',true)}>New scheduled task</button><button style={btnS(t.acc)} onClick={()=>onTab('convs')}>Add existing content</button></div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}<h3>Recent chats</h3>{ws.conversations.slice(0,5).map(c=><p key={c.id}><button style={btnS(t.acc)} onClick={()=>onOpenConv(c.id)}>{c.title||'New chat'}</button></p>)}{!ws.conversations.length&&<p>Start a project chat or add an existing conversation.</p>}
    <h3>Recent reports and files</h3>{(ws.reports||[]).slice(0,3).map(r=><p key={r.id}><button style={btnS(t.acc)} onClick={()=>onOpenReport?.(r.id)}>{r.title||r.query}</button></p>)}{(ws.files||[]).slice(0,3).map(f=><p key={f.id}><button style={btnS(t.acc)} onClick={()=>onNavigate?.('artifacts',f.id)}>{f.filename}</button></p>)}
    <h3>Open todos</h3>{notes.filter(n=>n.kind==='todo'&&!n.done).map(n=><p key={n.id}>{n.title}{n.due_at?` · due ${n.due_at}`:''}</p>)}<button style={btnS(t.acc)} onClick={()=>onTab('notes')}>View notes and todos</button>
    <h3>Active jobs</h3>{jobs.map(j=><p key={j.id}>{j.title} · {j.phase}</p>)}{!jobs.length&&<p>No active work.</p>}<button style={btnS(t.acc)} onClick={()=>onTab('jobs')}>View jobs</button>
    <h3>Knowledge</h3>{knowledge.map(k=><p key={k.id}>{k.name} · {k.ready?'Ready':'Needs indexing or review'}</p>)}<button style={btnS(t.acc)} onClick={()=>onTab('knowledge')}>Manage knowledge</button>
  </div>;
}
