import React,{useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import PanelHeader from './PanelHeader.jsx';
import SelectionControl from './SelectionControl.jsx';
import ActionMenu from './ActionMenu.jsx';
import ModelPicker from '../ModelPicker.jsx';
import useFocusScope from '../useFocusScope.js';
import {IC} from './icons.jsx';
import {apiJson} from '../api.js';
import {parseUtcishMs} from '../datetime.js';
import {humanize} from '../workspaceLabels.js';
import EmptyState from './EmptyState.jsx';
import Switch from './Switch.jsx';
import Pager from './Pager.jsx';
import {SkeletonText,SkeletonList} from './Skeleton.jsx';
import {Dots} from './Spinner.jsx';
import {WorkspaceListRow,StatusChip,DefaultsChips,kindIcon} from './workspaceRows.jsx';
import NotesPanel from '../panels/NotesPanel.jsx';
import TasksPanel from '../panels/TasksPanel.jsx';
import JobsPanel from '../panels/JobsPanel.jsx';

export function WorkspaceHome({t,btnS,inputS,workspaces,onOpen,onCreated,onNewChat,notify}){
  const [name,setName]=useState(''),[busy,setBusy]=useState(false),[creating,setCreating]=useState(false);
  const create=async()=>{if(!name.trim()||busy)return;setBusy(true);try{const ws=await apiJson('/api/workspaces',{method:'POST',body:{name}});setName('');setCreating(false);onCreated(ws);}catch(e){notify({type:'error',text:'Workspace not created',detail:e.message});}finally{setBusy(false);}};
  return <section className="hc-workspace-home" style={{color:t.text}}>
    <PanelHeader t={t} icon={<IC.Folder/>} title="Workspaces" subtitle="Chats, sources and ongoing work for each project."><button className="hc-button is-primary" onClick={()=>setCreating(true)}><IC.Plus/> New workspace</button></PanelHeader>
    <div className="hc-workspace-body">
    {creating&&<form className="hc-workspace-card hc-toolbar" onSubmit={e=>{e.preventDefault();create();}}><input autoFocus aria-label="Workspace name" value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder="Project name" style={{...inputS,flex:'1 1 180px'}}/><button className="hc-button is-primary" disabled={busy||!name.trim()}>{busy?'Creating…':'Create workspace'}</button><button type="button" className="hc-button" onClick={()=>setCreating(false)}>Cancel</button></form>}
    {!workspaces.length&&!creating&&<EmptyState icon={<IC.Folder/>} title="Your projects start here" hint="Create a workspace to keep related chats, sources and notes together." action={<button className="hc-button is-primary" onClick={()=>setCreating(true)}><IC.Plus/> New workspace</button>}/>}
    <div className="hc-workspace-grid">{workspaces.map((w,i)=><article key={w.id} className="hc-workspace-card hc-ws-home-card" style={{'--i':i}}>
      <button className="hc-workspace-title" onClick={()=>onOpen(w.id)}><strong>{w.name}</strong></button>
      <p className={`hc-secondary${w.description?'':' is-placeholder'}`}>{w.description||'Add sources and start a project chat.'}</p>
      <div className="hc-ws-meta is-plain"><span><IC.Chat/>{w.conv_count||0} chats</span><span><IC.Research/>{w.report_count||0} reports</span><span><IC.File/>{w.file_count||0} artifacts</span></div>
      <button className="hc-button" onClick={()=>onNewChat(w.id)} title="New chat in this workspace"><IC.Plus/> New chat</button>
    </article>)}</div>
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
    {!kb?<p className="hc-secondary" style={{display:'flex',gap:8,alignItems:'center'}}><Dots/> Checking knowledge readiness…</p>:kb.files.map(f=><div key={f.id} style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center',margin:'8px 0'}}><span style={{overflowWrap:'anywhere'}}>{f.filename}: <strong>{({done:'Ready',indexing:'Indexing',partial:'Partial — review warnings',error:'Needs attention',unknown:'Not verified'})[f.indexing.status]||f.indexing.status}</strong>{f.indexing.chunks!=null&&` · ${f.indexing.chunks} passages`}</span>
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
    <Pager page={page} pageSize={50} count={items.length} hasMore={!!more} busy={busy} onPage={setPage} label="Snapshot pages"/>
  </div>;
}

export function WorkspaceDaily({tab,ws,t,btnS,inputS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate,onAddContent,refreshKey}){
  const [taskTab,setTaskTab]=useState('tasks');
  const cardS={padding:14,borderRadius:"var(--hc-radius)",border:`1px solid ${t.brd}`,background:t.surface};
  return tab==='notes'?<NotesPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,workspaces,initialCreate,onAddContent,refreshKey}} embedded workspaceId={ws.id}/>:tab==='tasks'?<TasksPanel key={ws.id} {...{t,btnS,inputS,cardS,confirmAction,notify,models,workspaces,convs,onNavigate,initialCreate}} embedded workspaceId={ws.id} tab={taskTab} setTab={setTaskTab}/>:<JobsPanel key={ws.id} {...{t,btnS,inputS,onNavigate,workspaces}} embedded workspaceId={ws.id}/>;
}

export function WorkspaceOverview({ws,t,btnS,onTab,onOpenConv,onOpenReport,onNavigate,onNewChat}){
  const [data,setData]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();apiJson(`/api/workspaces/${ws.id}/overview`,{signal:c.signal}).then(d=>{setData(d);setError('');}).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,ws.updated_at]);
  const loading=!data&&!error;
  const dateStr=s=>{if(!s)return null;const ms=parseUtcishMs(s);return Number.isFinite(ms)?new Date(ms).toLocaleDateString():String(s);};
  const link=(label,fn)=><button className="hc-workspace-link" onClick={fn}>{label}</button>;
  // Card: heading with a count badge, skeleton while the overview loads, a compact empty state with a CTA, else a list of rows.
  const card=(title,{count,action,empty,wait=loading},rows,hasRows)=><section className="hc-workspace-card"><div className="hc-workspace-section-heading"><h2>{title}{count>0&&<span className="hc-count">{count}</span>}</h2>{action}</div>{wait&&!hasRows?<SkeletonText t={t} lines={2}/>:hasRows?<div className="hc-ws-list">{rows}</div>:empty}</section>;
  const convs=(ws.conversations||[]).slice(0,5), reports=(ws.reports||[]).slice(0,3), files=(ws.files||[]).slice(0,3);
  const pinned=data?.pinned||[], todos=data?.todos||[], jobs=data?.jobs||[], failures=data?.failures||[], pending=data?.pending_memories||0;
  const statusCount=jobs.length+failures.length+(pending?1:0);
  return <div>
    <div className="hc-toolbar hc-workspace-shortcuts"><button className="hc-button" onClick={()=>onTab('notes',true)}><IC.Plus/> New note or todo</button><button className="hc-button" onClick={()=>onTab('tasks',true)}><IC.Clock/> New scheduled task</button><button className="hc-button" onClick={event=>onTab('add',false,event)}><IC.Paperclip/> Add existing content</button></div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    <div className="hc-workspace-overview"><div>
      {card('Recent chats',{count:(ws.conversations||[]).length,action:link('View all',()=>onTab('convs')),wait:false,empty:<EmptyState compact icon={<IC.Chat/>} title="No chats yet" hint="Start a project chat or add an existing conversation." action={<button className="hc-button" onClick={()=>onNewChat?.()}><IC.Plus/> New chat</button>}/>},
        convs.map((c,i)=><WorkspaceListRow key={c.id} index={i} icon={<IC.Chat/>} title={c.title||'New chat'} onOpen={()=>onOpenConv(c.id)} meta={[c.model,dateStr(c.updated_at)]}/>),convs.length>0)}
      {card('Pinned sources',{count:pinned.length,action:link('Manage sources',()=>onTab('knowledge')),empty:<EmptyState compact icon={<IC.Pin/>} title="Nothing pinned" hint="Pin useful sources to keep them close at hand." action={<button className="hc-button" onClick={()=>onTab('knowledge')}>Manage sources</button>}/>},
        pinned.map((p,i)=><WorkspaceListRow key={p.kind+p.source_id} index={i} icon={kindIcon(p.kind)} title={p.title||'Untitled'} href={p.url} chips={<StatusChip t={t} kind="source-kind" value={p.kind}/>}/>),pinned.length>0)}
      {card('Recent reports and artifacts',{count:(ws.reports||[]).length+(ws.files||[]).length,action:link('View sources',()=>onTab('knowledge')),wait:false,empty:<EmptyState compact icon={<IC.Research/>} title="No reports or files yet" hint="Reports and files linked to this project will appear here." action={<button className="hc-button" onClick={event=>onTab('add',false,event)}>Add existing content</button>}/>},
        <>{reports.map((r,i)=><WorkspaceListRow key={r.id} index={i} icon={<IC.Research/>} title={r.title||r.query||'Research report'} onOpen={()=>onOpenReport?.(r.id)} chips={<StatusChip t={t} kind="report-status" value={String(r.status||'queued').toLowerCase()}/>} meta={[humanize(r.report_type||'report')]}/>)}
          {files.map((f,i)=><WorkspaceListRow key={f.id} index={reports.length+i} icon={<IC.File/>} title={f.filename} onOpen={()=>onNavigate?.('artifacts',f.id)} meta={[dateStr(f.created_at)]}/>)}</>,reports.length+files.length>0)}
    </div><aside>
      {card('Open todos',{count:todos.length,action:link('View notes',()=>onTab('notes')),empty:<EmptyState compact icon={<IC.Check/>} title="No open todos" hint="Add one to track your next step." action={<button className="hc-button" onClick={()=>onTab('notes',true)}><IC.Plus/> Add todo</button>}/>},
        todos.map((n,i)=><WorkspaceListRow key={n.id} index={i} icon={<IC.Pencil/>} title={n.title} onOpen={()=>onNavigate('notes',n.id)} meta={[n.due_at?`Due ${dateStr(n.due_at)}`:null]}/>),todos.length>0)}
      {card('Status',{count:statusCount,action:link('View activity',()=>onTab('jobs')),empty:<div className="hc-ws-allclear"><IC.Check/> All clear — nothing needs attention.</div>},
        <>{!!pending&&<WorkspaceListRow index={0} icon={<IC.Brain/>} title={`${pending} memory suggestion${pending===1?'':'s'} to review`} onOpen={()=>onTab('memory')} chips={<StatusChip t={t} kind="job-status" value="queued" label="Review"/>}/>}
          {jobs.map((j,i)=><WorkspaceListRow key={j.id} index={i+1} icon={<IC.Activity/>} title={j.title} onOpen={()=>onNavigate('jobs',j.id)} chips={<StatusChip t={t} kind="job-status" value="running" label={humanize(j.phase||'running')}/>}/>)}
          {failures.map((j,i)=><WorkspaceListRow key={j.id} index={i+1+jobs.length} icon={<IC.Alert/>} title={j.title} onOpen={()=>onNavigate('jobs',j.id)} chips={<StatusChip t={t} kind="job-status" value={j.status}/>}/>)}</>,statusCount>0)}
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
    <h2>New-chat defaults</h2><div><span className="hc-field-label">Model</span><div className="hc-toolbar"><ModelPicker label="Workspace default model" placeholder="Inherit persona / app model" value={draft.model||''} onChange={value=>field('model',value)} models={[...new Set([...models,...(draft.model?[draft.model]:[])])]} t={t} font="inherit"/><button className="hc-button" onClick={()=>field('model',null)}>Inherit persona / app model</button></div></div>
    <div><span className="hc-field-label">Persona</span><SelectionControl label="Workspace default persona" value={draft.model_config_id||''} onChange={v=>field('model_config_id',v||null)} options={[{value:'',label:'No default persona'},...(draft.model_config_id&&!personas.some(p=>p.id===draft.model_config_id)?[{value:draft.model_config_id,label:'Unavailable persona — choose another'}]:[]),...personas.map(p=>({value:p.id,label:p.name}))]}/></div>
    <div><span className="hc-field-label">Optional tools</span><SelectionControl label="Workspace tool inheritance" value={draft.tool_ids==null?'inherit':'explicit'} onChange={v=>field('tool_ids',v==='inherit'?null:[])} options={[{value:'inherit',label:'Inherit persona / app tools'},{value:'explicit',label:'Choose tools (none disables optional tools)'}]}/></div>
    {draft.tool_ids!=null&&<div style={{display:'flex',gap:12,flexWrap:'wrap'}}>{tools.map(tool=><label className="hc-check-row" key={tool.id}><input type="checkbox" checked={draft.tool_ids.includes(tool.id)} onChange={e=>field('tool_ids',e.target.checked?[...draft.tool_ids,tool.id]:draft.tool_ids.filter(id=>id!==tool.id))}/>{tool.name||tool.id}</label>)}</div>}
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}<button disabled={busy} className="hc-button is-primary" onClick={save}>{busy?'Saving…':'Save workspace settings'}</button>{saved&&<p role="status" style={{color:t.ok}}>Workspace settings saved.</p>}
  </section>;
}

export function WorkspaceDefaultSummary({ws,t,tools=[]}){
  const [defaults,setDefaults]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();setDefaults(null);setError('');apiJson(`/api/workspaces/${ws.id}/defaults`,{signal:c.signal}).then(setDefaults).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[ws.id,JSON.stringify(ws.chat_defaults)]);
  if(error)return <p role="alert" className="hc-secondary" style={{margin:0,color:t.err,overflowWrap:'anywhere'}}>{error}</p>;
  return <DefaultsChips defaults={defaults} tools={tools} t={t}/>;
}

export function WorkspaceContentPicker({ws,t,btnS,inputS,onLinked}){
  const [kind,setKind]=useState('chat'),[query,setQuery]=useState(''),[page,setPage]=useState(0),[data,setData]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);setError('');const timer=setTimeout(()=>apiJson(`/api/workspaces/${ws.id}/picker/${kind}?query=${encodeURIComponent(query)}&limit=30&offset=${page*30}`,{signal:c.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);}),200);return()=>{epoch.current++;clearTimeout(timer);c.abort();};},[ws.id,kind,query,page,revision]);
  const link=async id=>{if(pending.current)return;pending.current=true;setBusy(true);const v=epoch.current;try{await apiJson(`/api/workspaces/${ws.id}/sources/${kind}/${encodeURIComponent(id)}/link`,{method:'POST'});if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);setRevision(p=>p+1);}}finally{pending.current=false;setBusy(false);}};
  return <section><p className="hc-secondary">Link original content to this workspace.</p><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><SelectionControl label="Content type" value={kind} onChange={v=>{setKind(v);setPage(0);}} options={[['chat','Chats'],['note','Notes'],['research','Reports'],['artifact','Artifacts'],['knowledge','Knowledge bases']].map(([value,label])=>({value,label}))}/><input aria-label="Search existing content" placeholder="Search by title…" style={{...inputS,flex:'1 1 180px'}} value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/></div>
    {error&&<p role="alert">{error}</p>}{!data&&!error&&<SkeletonList t={t} rows={3}/>}{data?.items.length===0&&<p className="hc-secondary">No matching content.</p>}{data?.items.map(item=><div key={item.id} style={{display:'flex',gap:8,alignItems:'center',marginTop:10}}><span style={{flex:1,overflowWrap:'anywhere'}}>{item.title||'Untitled'}</span><button style={btnS(t.acc)} disabled={busy||item.linked} onClick={()=>link(item.id)}>{item.linked?'Linked':'Add'}</button></div>)}
    <Pager page={page} pageSize={30} count={data?.items.length} hasMore={!!data?.has_more} busy={busy} onPage={setPage} label="Content pages"/>
  </section>;
}

export function WorkspaceSources({ws,t,btnS,inputS,onLinked,onAdd,refreshKey,...snapshotProps}){
  const [data,setData]=useState(null),[page,setPage]=useState(0),[query,setQuery]=useState(''),[revision,setRevision]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState(''),[uploadProgress,setUploadProgress]=useState('');
  const pending=useRef(false),epoch=useRef(0);
  useEffect(()=>{const c=new AbortController();epoch.current++;setData(null);let loading=false;const load=async()=>{if(loading)return;loading=true;try{setData(await apiJson(`/api/workspaces/${ws.id}/sources?offset=${page*30}&limit=30&query=${encodeURIComponent(query)}`,{signal:c.signal}));setError('');}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{loading=false;}};load();const timer=setInterval(load,5000);return()=>{epoch.current++;c.abort();clearInterval(timer);};},[ws.id,page,query,revision,refreshKey]);
  const action=async (fn,onFailure)=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');const v=epoch.current;try{await fn();if(v===epoch.current){setRevision(p=>p+1);await onLinked?.();}}catch(e){if(v===epoch.current){setError(e.message);onFailure?.();}}finally{pending.current=false;setBusy(false);setUploadProgress('');}};
  const update=(s,body)=>{if(pending.current)return;const previous=data;setData(p=>({...p,items:p.items.map(item=>item.kind===s.kind&&item.source_id===s.source_id?{...item,...body}:item)}));return action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${encodeURIComponent(s.source_id)}`,{method:'PATCH',body}),()=>setData(previous));};
  const retrieval=s=>!s.enabled||ws.source_context_enabled===false?'Excluded from chat':s.can_use?'Chat can retrieve':'Not ready for retrieval';
  return <section className="hc-ws-section"><p className="hc-secondary" style={{margin:0}}>Relevant passages from enabled sources are retrieved automatically. Pinning adds quick access; it does not insert entire sources into every prompt.</p>{ws.source_context_enabled===false&&<p role="status" className="hc-secondary" style={{margin:0,color:t.warm}}>Automatic source context is off in workspace settings.</p>}
    <div className="hc-toolbar hc-workspace-source-toolbar"><input aria-label="Filter workspace sources" style={{...inputS,flex:'1 1 180px'}} value={query} placeholder="Filter sources…" onChange={e=>{setQuery(e.target.value);setPage(0);}}/>
      <label className="hc-button hc-upload-button"><IC.Upload/> Upload documents<input aria-label="Upload workspace documents" type="file" multiple disabled={busy} onChange={e=>{const files=[...e.target.files];e.target.value='';action(async()=>{for(let i=0;i<files.length;i++){setUploadProgress(`Uploading ${i+1} of ${files.length}: ${files[i].name}`);const body=new FormData();body.append('file',files[i]);await apiJson(`/api/workspaces/${ws.id}/upload`,{method:'POST',body});}});}}/></label>
      <button className="hc-button" onClick={onAdd}><IC.Paperclip/> Add existing content</button></div>
    {uploadProgress&&<p role="status" className="hc-secondary" style={{margin:0}}>{uploadProgress}</p>}
    {error&&<p role="alert" style={{margin:0,color:t.err}}>{error}</p>}{!data&&!error&&<SkeletonList t={t} rows={3}/>}
    {data?.items.length===0&&<EmptyState compact icon={<IC.Database/>} title={query?'No sources match this filter':'No sources yet'} hint={query?'Try a different filter.':'Upload documents or link chats, notes and reports so chat can retrieve from them.'} action={!query&&<button className="hc-button" onClick={onAdd}>Add existing content</button>}/>}
    {data?.items.length>0&&<div className="hc-ws-list hc-elev-1">{data.items.map((s,i)=><WorkspaceListRow key={`${s.kind}:${s.source_id}`} index={i} icon={kindIcon(s.kind)} title={s.title||'Untitled'} href={s.url}
      chips={<><StatusChip t={t} kind="source-kind" value={s.kind}/><StatusChip t={t} kind="readiness" value={s.readiness}/></>} meta={[retrieval(s)]}
      actions={<><Switch on={!!s.enabled} disabled={busy} label="Use in chat" onChange={()=>update(s,{enabled:!s.enabled})}><span className="hc-switch-text">Use in chat</span></Switch><Switch on={!!s.pinned} disabled={busy} label="Pin" color={t.warm} onChange={()=>update(s,{pinned:!s.pinned})}><span className="hc-switch-text">Pin</span></Switch>
      <ActionMenu compact label={`Actions for ${s.title||'source'}`} items={[
        s.indexing?.job_id&&['failed','partial','interrupted'].includes(s.indexing.state)&&{label:'Retry indexing',disabled:busy,onClick:()=>action(()=>apiJson(`/api/jobs/indexing:${s.indexing.job_id}/actions/retry`,{method:'POST'}))},
        s.kind!=='document'&&{label:'Unlink',disabled:busy,onClick:()=>action(()=>apiJson(`/api/workspaces/${ws.id}/sources/${s.kind}/${encodeURIComponent(s.source_id)}/link`,{method:'DELETE'}))},
        {label:'View source',onClick:()=>{window.location.hash=s.url.startsWith('#')?s.url:window.location.hash; if(!s.url.startsWith('#'))window.open(s.url,'_blank','noopener');}}
      ]}/></>}
      footer={(s.indexing||s.kind==='knowledge')&&<details className="hc-source-details"><summary>Indexing details{s.indexing?.error?' · Needs attention':''}</summary>{s.indexing?.phase&&<p role="status">{s.indexing.phase}{s.indexing.total?` · ${s.indexing.completed} / ${s.indexing.total} passages`:''}</p>}{s.indexing?.error&&<p role="alert">{s.indexing.error}</p>}{s.indexing?.warnings?.map(w=><p key={w}>{w}</p>)}{s.kind==='knowledge'&&<KnowledgeReadiness kbId={s.source_id} {...{t,btnS,inputS}}/>}</details>}/>)}</div>}
    <Pager page={page} pageSize={30} count={data?.items.length} hasMore={!!data?.has_more} busy={busy} onPage={setPage} label="Sources pages"/>
    <details style={{marginTop:12}}><summary style={{cursor:'pointer'}}>Knowledge snapshots (explicit copies)</summary><WorkspaceKnowledge {...{ws,t,btnS,inputS,...snapshotProps}}/></details>
  </section>;
}

export function WorkspaceContentDialog({onClose,...props}){
  const focus=useFocusScope(true,onClose);
  return createPortal(<div className="hc-workspace-modal" onClick={e=>{if(e.target===e.currentTarget)onClose();}}><div ref={focus} role="dialog" aria-modal="true" aria-label="Add existing content" tabIndex={-1} className="hc-workspace-dialog"><div className="hc-dialog-head"><h3>Add existing content</h3><button aria-label="Close" data-tip="Close" className="hc-icon-btn is-lg tip-below" onClick={onClose}><IC.X/></button></div><div className="hc-dialog-body"><WorkspaceContentPicker {...props}/></div></div></div>,document.body);
}
