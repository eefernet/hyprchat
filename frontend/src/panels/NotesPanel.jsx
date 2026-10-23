import SelectionControl from '../components/SelectionControl.jsx';
import React,{useState,useEffect,useCallback,useRef} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtLocalMinute } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';
import { SkeletonCard } from '../components/Skeleton.jsx';
import useIsMobile from '../useIsMobile.js';

const EMPTY={kind:"todo",title:"",content:"",due_local:"",remind_local:"",tags:[],workspace_ids:[]};

export default function NotesPanel({t,btnS,cardS,inputS,confirmAction,notify,initialNoteId,onInitialNoteConsumed,workspaces=[],workspaceId="",embedded=false,onAddContent,refreshKey=0,initialCreate=false}){
  const isMobile=useIsMobile();
  const [notes,setNotes]=useState([]);
  const [workspace,setWorkspace]=useState(workspaceId),[tag,setTag]=useState(""),[offset,setOffset]=useState(0),[allNotes,setAllNotes]=useState([]);
  const [loaded,setLoaded]=useState(false);
  const [tab,setTab]=useState("todo");
  const [form,setForm]=useState(()=>initialCreate?{...EMPTY,workspace_ids:workspaceId?[workspaceId]:[]}:null);
  const [showDone,setShowDone]=useState(false);
  const [err,setErr]=useState("");
  const [loadErr,setLoadErr]=useState("");
  const openedNote=useRef(null);
  useEffect(()=>{if(!initialNoteId||openedNote.current===initialNoteId)return;const c=new AbortController();apiJson(`/api/notes/${initialNoteId}`,{signal:c.signal}).then(note=>{openedNote.current=initialNoteId;setTab(note.kind);setForm({...EMPTY,...note});onInitialNoteConsumed?.();}).catch(e=>{if(e.name!=='AbortError')setErr(e.message);});return()=>c.abort();},[initialNoteId]);

  const loadEpoch=useRef(0);
  const load=useCallback(async()=>{
    const version=++loadEpoch.current;
    try{const data=await apiJson(`/api/notes?workspace_id=${workspace}&tag=${encodeURIComponent(tag)}&offset=${offset}&kind=${tab}&include_done=${showDone}`);if(version===loadEpoch.current){setNotes(data);setLoadErr("");}}catch(e){if(version===loadEpoch.current&&e.name!=="AbortError")setLoadErr(String(e.message||e));}
    finally{if(version===loadEpoch.current)setLoaded(true);}
  },[workspace,tag,offset,tab,showDone]);
  const loadLatest=useRef(load);loadLatest.current=load;
  useEffect(()=>{let alive=true;load();if(workspace)apiJson("/api/notes").then(d=>{if(alive)setAllNotes(d);}).catch(()=>{});return()=>{alive=false;};},[load,workspace,refreshKey]);

  const save=async()=>{
    if(!form?.title?.trim())return;
    setErr("");
    try{
      const r=await fetch(`${API}/api/notes${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({kind:form.kind,title:form.title,content:form.content,
          due_local:form.due_local||"",remind_local:form.remind_local||"",tags:form.tags||[],workspace_ids:form.workspace_ids||[]})});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);loadLatest.current();
    }catch(e){setErr(String(e.message||e));}
  };
  const toggleDone=async n=>{
    try{await apiJson(`/api/notes/${n.id}`,{method:"PATCH",body:{done:!n.done}});}
    catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Update failed",detail:String(e.message||e)});}
    finally{loadLatest.current();} // reload either way so the checkbox reflects server truth
  };
  const remove=async id=>{
    if(!await confirmAction({title:"Delete item",body:"Delete this item?",confirmLabel:"Delete",tone:"danger"}))return;
    try{await apiJson(`/api/notes/${id}`,{method:"DELETE"});}
    catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Delete failed",detail:String(e.message||e)});}
    finally{loadLatest.current();}
  };

  const shown=notes.filter(n=>n.kind===tab&&(showDone||!n.done));
  const fmtLocal=fmtLocalMinute;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:embedded?"visible":"hidden"}}>
    <PanelHeader embedded={embedded} t={t} color={t.ok} icon={<IC.Pencil/>} title="Notes & Todos"
      subtitle="Reminders fire as notifications; the assistant reads these in check-ins."
      nav={<>
        <button onClick={()=>{setTab("todo");setOffset(0);}} style={{...btnS(tab==="todo"?t.ok:t.mut),padding:"5px 12px"}}>Todos</button>
        <button onClick={()=>{setTab("note");setOffset(0);}} style={{...btnS(tab==="note"?t.ok:t.mut),padding:"5px 12px"}}>Notes</button>
        <label style={{fontSize:"var(--hc-text-secondary)",color:t.mut,display:"flex",gap:5,alignItems:"center",marginLeft:6}}>
          <input type="checkbox" checked={showDone} onChange={e=>{setShowDone(e.target.checked);setOffset(0);}}/> show done
        </label>
      </>}>
      {embedded&&<button style={btnS(t.acc)} onClick={onAddContent}>Add existing content</button>}<button onClick={load} title="Refresh notes" style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      <button onClick={()=>setForm({...EMPTY,kind:tab,workspace_ids:workspace?[workspace]:[]})} style={btnS(t.warm)}><IC.Plus/> New</button>
    </PanelHeader>
    <div style={{overflowY:embedded?"visible":"auto",padding:embedded?0:isMobile?"14px 12px":"20px 28px",flex:1}}>
      <div style={{maxWidth:760}}>
        <div style={{display:'flex',gap:8,flexWrap:'wrap',marginBottom:12}}>{!embedded&&<select aria-label="Notes workspace" disabled={!!workspaceId} value={workspace} onChange={e=>{setWorkspace(e.target.value);setOffset(0);}} style={{...inputS,width:'auto'}}><option value="">All workspaces</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select>}<input aria-label="Filter notes by tag" value={tag} onChange={e=>{setTag(e.target.value);setOffset(0);}} placeholder="Filter by tag" style={{...inputS,width:'auto'}}/>
        {workspace&&!embedded&&<select value="" aria-label="Link existing note" style={{...inputS,width:'auto'}} onChange={async e=>{const id=e.target.value;if(!id)return;try{await apiJson(`/api/workspaces/${workspace}/links/notes/${id}`,{method:'POST'});loadLatest.current();}catch(error){setErr(error.message);}}}><option value="">Link existing note / todo…</option>{allNotes.filter(n=>!(n.workspace_ids||[]).includes(workspace)).map(n=><option key={n.id} value={n.id}>{n.title}</option>)}</select>}</div>

        {(err||loadErr)&&<div role="alert" style={{color:t.err,fontSize:12,marginBottom:12}}>{err||loadErr}</div>}
        {form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.ok}55`}}>
          <div style={{display:"flex",gap:8,marginBottom:8}}>
            <SelectionControl label="Note type" value={form.kind} onChange={value=>setForm(f=>({...f,kind:value}))} options={[{value:'todo',label:'Todo'},{value:'note',label:'Note'}]}/>
            <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,flex:1,minWidth:0}} autoFocus/>
          </div>
          <textarea value={form.content} onChange={e=>setForm(f=>({...f,content:e.target.value}))} placeholder="Details (optional)" rows={3} style={{...inputS,marginBottom:8,resize:"vertical"}}/>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:10,alignItems:"center"}}>
            <div style={{minWidth:0,maxWidth:"100%"}}><div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:3}}>Due (local)</div>
              <input type="datetime-local" value={form.due_local||""} onChange={e=>setForm(f=>({...f,due_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <div style={{minWidth:0,maxWidth:"100%"}}><div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:3}}>Remind me at</div>
              <input type="datetime-local" value={form.remind_local||""} onChange={e=>setForm(f=>({...f,remind_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
          </div>
          <label style={{display:'block',marginBottom:8}}>Tags (comma separated)<input value={(form.tags||[]).join(', ')} onChange={e=>setForm(f=>({...f,tags:e.target.value.split(',').map(v=>v.trim())}))} style={inputS}/></label>
          {!embedded&&<fieldset style={{border:`1px solid ${t.brd}`,marginBottom:10}}><legend>Workspaces</legend>{workspaces.map(w=><label key={w.id} style={{display:'block'}}><input type="checkbox" checked={(form.workspace_ids||[]).includes(w.id)} onChange={e=>setForm(f=>({...f,workspace_ids:e.target.checked?[...(f.workspace_ids||[]),w.id]:(f.workspace_ids||[]).filter(id=>id!==w.id)}))}/>{w.name}</label>)}</fieldset>}
          <div style={{display:"flex",gap:8}}>
            <button onClick={save} style={btnS(t.ok)}>{form.id?"Save":"Add"}</button>
            <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
          </div>
        </div>}
        {!loaded&&!form?Array.from({length:3},(_,i)=><SkeletonCard key={i} t={t} h={72} lines={1} style={{marginBottom:8}}/>)
        :shown.length===0&&!form?<EmptyState t={t} icon={<IC.Pencil/>} title={`No ${tab==="todo"?"todos":"notes"} yet`} hint='Add one here, or tell the assistant: "add buy milk to my todo list".'/>
        :shown.map(n=><div key={n.id} style={{...cardS,marginBottom:8,display:"flex",gap:10,alignItems:"flex-start",opacity:n.done?.55:1}}>
          {n.kind==="todo"&&<input type="checkbox" checked={n.done} onChange={()=>toggleDone(n)} style={{marginTop:3,cursor:"pointer"}}/>}
          <div style={{flex:1,minWidth:0,overflowWrap:"anywhere"}}>
            <div style={{fontSize:13,fontWeight:700,color:t.text,textDecoration:n.done?"line-through":"none"}}>{n.title}</div>
            {n.content&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginTop:3,whiteSpace:"pre-wrap"}}>{n.content.slice(0,400)}</div>}
            <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:4}}>
              {n.due_local?`due ${fmtLocal(n.due_local)}`:""}
              {n.remind_local?` · remind ${fmtLocal(n.remind_local)}${n.reminded?" (sent)":""}`:""}
            </div>
          </div>
          <button aria-label="Edit" onClick={()=>setForm({...EMPTY,...n})} style={{...btnS(t.acc),padding:"3px 8px",fontSize:"var(--hc-text-control)"}}><IC.Pencil/></button>
          {workspace&&<button style={btnS(t.mut)} onClick={async()=>{try{await apiJson(`/api/workspaces/${workspace}/links/notes/${n.id}`,{method:'DELETE'});loadLatest.current();}catch(error){setErr(error.message);}}}>Unlink</button>}
          <button aria-label="Delete" onClick={()=>remove(n.id)} style={{...btnS(t.err),padding:"3px 8px",fontSize:"var(--hc-text-control)"}}><IC.Trash/></button>
        </div>)}
        <div style={{display:"flex",gap:8}}><button disabled={!offset} style={btnS(t.acc)} onClick={()=>setOffset(n=>Math.max(0,n-200))}>Previous</button><button disabled={notes.length<200} style={btnS(t.acc)} onClick={()=>setOffset(n=>n+200)}>Next</button></div>
      </div>
    </div>
  </div>;
}
