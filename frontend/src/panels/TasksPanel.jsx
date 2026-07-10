import React,{useState,useEffect,useCallback} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtUtcMinute } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';

const KIND_FIELDS={once:["run_at"],daily:["time"],weekly:["time","weekday"],monthly:["time","day"],cron:["cron"]};
const WEEKDAYS=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const EMPTY_FORM={title:"",prompt:"",task_type:"llm",schedule_kind:"daily",time:"08:30",weekday:0,day:1,cron:"",run_at:"",notify:true,delivery:{}};

export default function TasksPanel({t,btnS,cardS,inputS,confirmAction,tab,setTab,onUnseenChange}){
  const [tasks,setTasks]=useState([]);
  const [notifs,setNotifs]=useState([]);
  const [unseen,setUnseen]=useState(0);
  const [form,setForm]=useState(null);          // null = closed, {...EMPTY_FORM, id?} = editor open
  const [nlText,setNlText]=useState("");
  const [nlBusy,setNlBusy]=useState(false);
  const [runsFor,setRunsFor]=useState(null);    // task id whose run history is expanded
  const [runs,setRuns]=useState([]);
  const [err,setErr]=useState("");
  const [ntfy,setNtfy]=useState({url:"",topic:"",token:"",allow_private:false,token_set:false});

  const loadTasks=useCallback(async()=>{
    try{setTasks(await apiJson("/api/tasks"));}catch(e){setErr(String(e.message||e));}
  },[]);
  const loadNotifs=useCallback(async()=>{
    try{
      const d=await apiJson("/api/notifications?limit=100");
      setNotifs(d.notifications||[]);setUnseen(d.unseen_count||0);
      onUnseenChange&&onUnseenChange(d.unseen_count||0);
    }catch(e){setErr(String(e.message||e));}
  },[onUnseenChange]);
  const loadNtfy=useCallback(async()=>{
    try{const d=await apiJson("/api/notifications/channels");setNtfy(n=>({...n,...(d.ntfy||{}),token:""}));}
    catch(e){setErr(String(e.message||e));}
  },[]);
  useEffect(()=>{loadTasks();loadNotifs();loadNtfy();},[loadTasks,loadNotifs,loadNtfy]);

  const saveTask=async()=>{
    if(!form)return;
    setErr("");
    const schedule_json={};
    for(const f of (KIND_FIELDS[form.schedule_kind]||[]))
      if(form[f]!==""&&form[f]!=null)schedule_json[f]=(f==="weekday"||f==="day")?Number(form[f]):form[f];
    // Preserve delivery options the form doesn't surface (ntfy/email flags) —
    // spreading form.delivery first keeps them across edits.
    const body={title:form.title,prompt:form.prompt,task_type:form.task_type,
      schedule_kind:form.schedule_kind,schedule_json,
      delivery_json:{...(form.delivery||{}),conversation:!!form.conversation_id,notify:!!form.notify},
      conversation_id:form.conversation_id||""};
    try{
      const r=await fetch(`${API}/api/tasks${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);loadTasks();
    }catch(e){setErr(String(e.message||e));}
  };
  const taskAction=async(id,action)=>{
    try{
      if(action==="delete"){if(!await confirmAction({title:"Delete task",body:"Delete this scheduled task and its run history?",confirmLabel:"Delete",tone:"danger"}))return;await fetch(`${API}/api/tasks/${id}`,{method:"DELETE"});}
      else await fetch(`${API}/api/tasks/${id}/${action}`,{method:"POST"});
      loadTasks();
    }catch(e){setErr(String(e));}
  };
  const refreshRuns=useCallback(async id=>{
    try{setRuns(await apiJson(`/api/tasks/${id}/runs`));}catch{}
  },[]);
  const showRuns=async id=>{
    if(runsFor===id){setRunsFor(null);return;}
    setRunsFor(id);setRuns([]);
    refreshRuns(id);
  };
  const parseNl=async()=>{
    if(!nlText.trim())return;
    setNlBusy(true);setErr("");
    try{
      const r=await fetch(`${API}/api/tasks/parse`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:nlText})});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||"Could not parse");
      const sj=d.schedule_json||{};
      setForm({...EMPTY_FORM,title:d.title||"",prompt:d.prompt||"",task_type:d.task_type||"llm",
        schedule_kind:d.schedule_kind||"once",time:sj.time||"08:30",weekday:sj.weekday??0,day:sj.day??1,
        cron:sj.cron||"",run_at:sj.run_at||""});
      setNlText("");
    }catch(e){setErr(String(e.message||e));}
    finally{setNlBusy(false);}
  };
  const markAllSeen=async()=>{
    await fetch(`${API}/api/notifications/seen`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
    loadNotifs();
  };
  const saveNtfy=async()=>{
    await fetch(`${API}/api/notifications/channels/ntfy`,{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url:ntfy.url,topic:ntfy.topic,token:ntfy.token,allow_private:!!ntfy.allow_private})});
    loadNtfy();
  };

  const kindLabel=k=>({once:"once",daily:"daily",weekly:"weekly",monthly:"monthly",cron:"cron",event:"on event",webhook:"webhook"})[k]||k;
  const fmtTs=fmtUtcMinute;
  const statusColor=s=>s==="succeeded"?t.ok:s==="failed"?t.err:s==="running"?t.warm:t.mut;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} icon={<IC.Clock/>} title="Scheduled Tasks"
      subtitle="Recurring agent work, reminders, and the notification queue."
      nav={<>
        <button onClick={()=>setTab("tasks")} style={{...btnS(tab==="tasks"?t.acc:t.mut),padding:"5px 12px"}}>Tasks</button>
        <button onClick={()=>setTab("notifications")} style={{...btnS(tab==="notifications"?t.acc:t.mut),padding:"5px 12px"}}>
          Notifications{unseen>0?` (${unseen})`:""}
        </button>
      </>}>
      <button onClick={()=>{loadTasks();loadNotifs();}} style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      {tab==="tasks"&&<button onClick={()=>setForm({...EMPTY_FORM})} style={btnS(t.warm)}><IC.Plus/> New Task</button>}
    </PanelHeader>
    <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
      <div style={{maxWidth:900}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}

        {tab==="tasks"&&<>
          <div style={{...cardS,display:"flex",gap:8,alignItems:"center",marginBottom:16}}>
            <input value={nlText} onChange={e=>setNlText(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")parseNl();}}
              placeholder='Describe it: "every weekday at 8am summarize AI news"...' style={{...inputS,flex:1}}/>
            <button onClick={parseNl} disabled={nlBusy} style={btnS(t.acc)}>{nlBusy?"Parsing...":"Draft Task"}</button>
          </div>

          {form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.acc}55`}}>
            <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:10}}>{form.id?"Edit task":"New task (review before saving)"}</div>
            <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,marginBottom:8}}/>
            <textarea value={form.prompt} onChange={e=>setForm(f=>({...f,prompt:e.target.value}))} placeholder="What should the agent do each run?" rows={3} style={{...inputS,marginBottom:8,resize:"vertical"}}/>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8,alignItems:"center"}}>
              <select value={form.task_type} onChange={e=>setForm(f=>({...f,task_type:e.target.value}))} style={{...inputS,width:"auto"}}>
                <option value="llm">Agent task</option>
                <option value="research">Deep research</option>
              </select>
              <select value={form.schedule_kind} onChange={e=>setForm(f=>({...f,schedule_kind:e.target.value}))} style={{...inputS,width:"auto"}}>
                {Object.keys(KIND_FIELDS).map(k=><option key={k} value={k}>{k}</option>)}
              </select>
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("time")&&<input type="time" value={form.time} onChange={e=>setForm(f=>({...f,time:e.target.value}))} style={{...inputS,width:"auto"}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("weekday")&&<select value={form.weekday} onChange={e=>setForm(f=>({...f,weekday:e.target.value}))} style={{...inputS,width:"auto"}}>
                {WEEKDAYS.map((w,i)=><option key={i} value={i}>{w}</option>)}
              </select>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("day")&&<input type="number" min={1} max={31} value={form.day} onChange={e=>setForm(f=>({...f,day:e.target.value}))} style={{...inputS,width:70}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("cron")&&<input value={form.cron} onChange={e=>setForm(f=>({...f,cron:e.target.value}))} placeholder="*/30 * * * *" style={{...inputS,width:160}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("run_at")&&<input type="datetime-local" value={form.run_at} onChange={e=>setForm(f=>({...f,run_at:e.target.value}))} style={{...inputS,width:"auto"}}/>}
              <label style={{fontSize:10,color:t.mut,display:"flex",gap:5,alignItems:"center"}}>
                <input type="checkbox" checked={!!form.notify} onChange={e=>setForm(f=>({...f,notify:e.target.checked}))}/> notify when done
              </label>
            </div>
            <div style={{display:"flex",gap:8}}>
              <button onClick={saveTask} style={btnS(t.ok)}>{form.id?"Save":"Create"}</button>
              <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
            </div>
          </div>}

          {tasks.length===0&&!form?<EmptyState t={t} icon={<IC.Clock/>} title="No scheduled tasks yet" hint='Create one above, or just ask in chat: "remind me every morning at 8 to stretch".'/>
          :tasks.map(task=><div key={task.id} style={{...cardS,marginBottom:10,opacity:task.enabled?1:.55}}>
            <div style={{display:"flex",alignItems:"center",gap:10,flexWrap:"wrap"}}>
              <div style={{flex:1,minWidth:200}}>
                <div style={{fontSize:13,fontWeight:800,color:t.text}}>{task.title}</div>
                <div style={{fontSize:10,color:t.mut,marginTop:3}}>
                  {kindLabel(task.schedule_kind)}
                  {task.next_run?` · next ${fmtTs(task.next_run)}`:""}
                  {task.last_status?<span> · last <span style={{color:statusColor(task.last_status)}}>{task.last_status}</span></span>:null}
                  {task.task_type!=="llm"?` · ${task.task_type}`:""}
                </div>
              </div>
              <button onClick={()=>taskAction(task.id,"run")} title="Run now" style={{...btnS(t.ok),padding:"4px 9px",fontSize:10}}>▶ Run</button>
              <button onClick={()=>taskAction(task.id,task.enabled?"pause":"resume")} style={{...btnS(task.enabled?t.warm:t.ok),padding:"4px 9px",fontSize:10}}>{task.enabled?"Pause":"Resume"}</button>
              <button onClick={()=>{const sj=task.schedule_json||{};const dj=task.delivery_json||{};setForm({...EMPTY_FORM,id:task.id,title:task.title,prompt:task.prompt,task_type:task.task_type,schedule_kind:task.schedule_kind,time:sj.time||"08:30",weekday:sj.weekday??0,day:sj.day??1,cron:sj.cron||"",run_at:sj.run_at||"",conversation_id:task.conversation_id,notify:dj.notify!==false,delivery:dj});}} style={{...btnS(t.acc),padding:"4px 9px",fontSize:10}}><IC.Pencil/></button>
              <button onClick={()=>showRuns(task.id)} style={{...btnS(t.mut),padding:"4px 9px",fontSize:10}}>History</button>
              <button onClick={()=>taskAction(task.id,"delete")} style={{...btnS(t.err),padding:"4px 9px",fontSize:10}}><IC.Trash/></button>
            </div>
            {task.schedule_kind==="webhook"&&task.webhook_token&&<div style={{fontSize:10,color:t.mut,marginTop:6,wordBreak:"break-all"}}>
              Webhook: POST {API||window.location.origin}/api/hooks/{task.webhook_token}
            </div>}
            {task.prompt&&<div style={{fontSize:11,color:t.dim,marginTop:6,whiteSpace:"pre-wrap"}}>{task.prompt.slice(0,300)}{task.prompt.length>300?"…":""}</div>}
            {runsFor===task.id&&<div style={{marginTop:10,borderTop:`1px solid ${t.brd}33`,paddingTop:8}}>
              {runs.length===0?<div style={{fontSize:11,color:t.mut}}>No runs yet.</div>
              :runs.map(r=><div key={r.id} style={{fontSize:11,color:t.dim,padding:"3px 0",display:"flex",gap:8,alignItems:"baseline"}}>
                <span style={{color:statusColor(r.status),fontWeight:700,minWidth:70}}>{r.status}</span>
                <span style={{color:t.mut}}>{fmtTs(r.started_at)}</span>
                <span style={{flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{r.error||r.result_text||""}</span>
                {r.status==="running"&&<button onClick={async()=>{await fetch(`${API}/api/tasks/runs/${r.id}/cancel`,{method:"POST"});refreshRuns(task.id);loadTasks();}} style={{...btnS(t.err),padding:"2px 6px",fontSize:9}}>Stop</button>}
              </div>)}
            </div>}
          </div>)}
        </>}

        {tab==="notifications"&&<>
          <div style={{display:"flex",gap:8,marginBottom:14,alignItems:"center"}}>
            <div style={{fontSize:12,color:t.mut}}>{unseen} unseen</div>
            <div style={{flex:1}}/>
            <button onClick={markAllSeen} style={btnS(t.acc)}>Mark all seen</button>
          </div>
          {notifs.length===0?<EmptyState t={t} icon={<IC.Flash/>} title="No notifications" hint="Scheduled task results and reminders will land here."/>
          :notifs.map(n=><div key={n.id} style={{...cardS,marginBottom:8,borderLeft:`3px solid ${n.seen?t.brd:t.warm}`,display:"flex",gap:10,alignItems:"flex-start"}}>
            <div style={{flex:1,minWidth:0}}>
              <div style={{fontSize:12,fontWeight:n.seen?600:800,color:t.text}}>{n.title}</div>
              {n.body&&<div style={{fontSize:11,color:t.dim,marginTop:3,whiteSpace:"pre-wrap"}}>{n.body}</div>}
              <div style={{fontSize:9,color:t.mut,marginTop:4}}>{n.kind} · {fmtTs(n.created_at)}</div>
            </div>
            <button onClick={async()=>{await fetch(`${API}/api/notifications/${n.id}`,{method:"DELETE"});loadNotifs();}} style={{...btnS(t.mut),padding:"3px 7px",fontSize:10}}><IC.Trash/></button>
          </div>)}

          <div style={{...cardS,marginTop:20}}>
            <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:8}}>ntfy push channel</div>
            <div style={{fontSize:10,color:t.mut,marginBottom:10}}>Optional push notifications via an ntfy server. Tasks with the ntfy delivery option send here; in-app notifications always work.</div>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
              <input value={ntfy.url} onChange={e=>setNtfy(n=>({...n,url:e.target.value}))} placeholder="https://ntfy.sh" style={{...inputS,flex:2,minWidth:180}}/>
              <input value={ntfy.topic} onChange={e=>setNtfy(n=>({...n,topic:e.target.value}))} placeholder="topic" style={{...inputS,flex:1,minWidth:120}}/>
              <input value={ntfy.token} onChange={e=>setNtfy(n=>({...n,token:e.target.value}))} placeholder={ntfy.token_set?"token saved — leave blank to keep":"access token (optional)"} type="password" style={{...inputS,flex:1,minWidth:140}}/>
            </div>
            <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center",marginBottom:10}}>
              <input type="checkbox" checked={!!ntfy.allow_private} onChange={e=>setNtfy(n=>({...n,allow_private:e.target.checked}))}/>
              Allow private/LAN ntfy server
            </label>
            <button onClick={saveNtfy} style={btnS(t.ok)}>Save channel</button>
          </div>
        </>}
      </div>
    </div>
  </div>;
}
