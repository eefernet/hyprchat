import React,{useState,useEffect,useCallback} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtLocalMinute } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';

const EMPTY={kind:"todo",title:"",content:"",due_local:"",remind_local:""};

export default function NotesPanel({t,btnS,cardS,inputS,confirmAction}){
  const [notes,setNotes]=useState([]);
  const [tab,setTab]=useState("todo");
  const [form,setForm]=useState(null);
  const [showDone,setShowDone]=useState(false);
  const [err,setErr]=useState("");

  const load=useCallback(async()=>{
    try{setNotes(await apiJson("/api/notes"));}catch(e){setErr(String(e.message||e));}
  },[]);
  useEffect(()=>{load();},[load]);

  const save=async()=>{
    if(!form?.title?.trim())return;
    setErr("");
    try{
      const r=await fetch(`${API}/api/notes${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({kind:form.kind,title:form.title,content:form.content,
          due_local:form.due_local||"",remind_local:form.remind_local||""})});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);load();
    }catch(e){setErr(String(e.message||e));}
  };
  const toggleDone=async n=>{
    await fetch(`${API}/api/notes/${n.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({done:!n.done})});
    load();
  };
  const remove=async id=>{
    if(!await confirmAction({title:"Delete item",body:"Delete this item?",confirmLabel:"Delete",tone:"danger"}))return;
    await fetch(`${API}/api/notes/${id}`,{method:"DELETE"});load();
  };

  const shown=notes.filter(n=>n.kind===tab&&(showDone||!n.done));
  const fmtLocal=fmtLocalMinute;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.ok} icon={<IC.Pencil/>} title="Notes & Todos"
      subtitle="Reminders fire as notifications; the assistant reads these in check-ins."
      nav={<>
        <button onClick={()=>setTab("todo")} style={{...btnS(tab==="todo"?t.ok:t.mut),padding:"5px 12px"}}>Todos</button>
        <button onClick={()=>setTab("note")} style={{...btnS(tab==="note"?t.ok:t.mut),padding:"5px 12px"}}>Notes</button>
        <label style={{fontSize:10,color:t.mut,display:"flex",gap:5,alignItems:"center",marginLeft:6}}>
          <input type="checkbox" checked={showDone} onChange={e=>setShowDone(e.target.checked)}/> show done
        </label>
      </>}>
      <button onClick={load} style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      <button onClick={()=>setForm({...EMPTY,kind:tab})} style={btnS(t.warm)}><IC.Plus/> New</button>
    </PanelHeader>
    <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
      <div style={{maxWidth:760}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}
        {form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.ok}55`}}>
          <div style={{display:"flex",gap:8,marginBottom:8}}>
            <select value={form.kind} onChange={e=>setForm(f=>({...f,kind:e.target.value}))} style={{...inputS,width:"auto"}}>
              <option value="todo">Todo</option><option value="note">Note</option>
            </select>
            <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,flex:1}} autoFocus/>
          </div>
          <textarea value={form.content} onChange={e=>setForm(f=>({...f,content:e.target.value}))} placeholder="Details (optional)" rows={3} style={{...inputS,marginBottom:8,resize:"vertical"}}/>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:10,alignItems:"center"}}>
            <div><div style={{fontSize:9,color:t.mut,marginBottom:3}}>Due (local)</div>
              <input type="datetime-local" value={form.due_local||""} onChange={e=>setForm(f=>({...f,due_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <div><div style={{fontSize:9,color:t.mut,marginBottom:3}}>Remind me at</div>
              <input type="datetime-local" value={form.remind_local||""} onChange={e=>setForm(f=>({...f,remind_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
          </div>
          <div style={{display:"flex",gap:8}}>
            <button onClick={save} style={btnS(t.ok)}>{form.id?"Save":"Add"}</button>
            <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
          </div>
        </div>}
        {shown.length===0&&!form?<EmptyState t={t} icon={<IC.Pencil/>} title={`No ${tab==="todo"?"todos":"notes"} yet`} hint='Add one here, or tell the assistant: "add buy milk to my todo list".'/>
        :shown.map(n=><div key={n.id} style={{...cardS,marginBottom:8,display:"flex",gap:10,alignItems:"flex-start",opacity:n.done?.55:1}}>
          {n.kind==="todo"&&<input type="checkbox" checked={n.done} onChange={()=>toggleDone(n)} style={{marginTop:3,cursor:"pointer"}}/>}
          <div style={{flex:1,minWidth:0}}>
            <div style={{fontSize:13,fontWeight:700,color:t.text,textDecoration:n.done?"line-through":"none"}}>{n.title}</div>
            {n.content&&<div style={{fontSize:11,color:t.dim,marginTop:3,whiteSpace:"pre-wrap"}}>{n.content.slice(0,400)}</div>}
            <div style={{fontSize:9,color:t.mut,marginTop:4}}>
              {n.due_local?`due ${fmtLocal(n.due_local)}`:""}
              {n.remind_local?` · remind ${fmtLocal(n.remind_local)}${n.reminded?" (sent)":""}`:""}
            </div>
          </div>
          <button onClick={()=>setForm({id:n.id,kind:n.kind,title:n.title,content:n.content||"",due_local:n.due_local||"",remind_local:n.remind_local||""})} style={{...btnS(t.acc),padding:"3px 8px",fontSize:10}}><IC.Pencil/></button>
          <button onClick={()=>remove(n.id)} style={{...btnS(t.err),padding:"3px 8px",fontSize:10}}><IC.Trash/></button>
        </div>)}
      </div>
    </div>
  </div>;
}
