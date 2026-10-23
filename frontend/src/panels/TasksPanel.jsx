import SelectionControl from '../components/SelectionControl.jsx';
import ModelPicker from '../ModelPicker.jsx';
import {taskBody} from '../taskForm.js';
import React,{useState,useEffect,useCallback,useRef} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtUtcToLocal } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';
import { SkeletonCard } from '../components/Skeleton.jsx';
import useIsMobile from '../useIsMobile.js';

const KIND_FIELDS={once:["run_at"],daily:["time"],weekly:["time","weekday"],monthly:["time","day"],cron:["cron"],event:[],webhook:[]};
const WEEKDAYS=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const EMPTY_FORM={title:"",prompt:"",task_type:"llm",schedule_kind:"daily",time:"08:30",weekday:0,day:1,cron:"",run_at:"",event:"",every:1,notify:true,delivery:{},timezone:"",model:"",tool_ids:[],workspace_id:"",conversation_id:""};

export default function TasksPanel({t,btnS,cardS,inputS,confirmAction,tab,setTab,onUnseenChange,notify,models=[],workspaces=[],convs=[],workspaceId="",embedded=false,onAddContent,onNavigate,initialCreate=false}){
  const isMobile=useIsMobile();
  const [tasks,setTasks]=useState([]);
  const [workspace,setWorkspace]=useState(workspaceId),[toolOptions,setToolOptions]=useState([]),[preview,setPreview]=useState(null),[saving,setSaving]=useState(false);
  const [loaded,setLoaded]=useState(false);
  const [notifs,setNotifs]=useState([]);
  const [unseen,setUnseen]=useState(0);
  const [form,setForm]=useState(()=>initialCreate?{...EMPTY_FORM,workspace_id:workspaceId}:null);          // null = closed, {...EMPTY_FORM, id?} = editor open
  const [nlText,setNlText]=useState("");
  const [nlBusy,setNlBusy]=useState(false);
  const [runsFor,setRunsFor]=useState(null);    // task id whose run history is expanded
  const [runs,setRuns]=useState([]);
  const [err,setErr]=useState("");
  const [ntfy,setNtfy]=useState({url:"",topic:"",token:"",allow_private:false,token_set:false});
  const [eventNames,setEventNames]=useState([]);

  const loadEpoch=useRef(0),runsEpoch=useRef(0);
  const loadTasks=useCallback(async()=>{
    const version=++loadEpoch.current;
    try{const data=await apiJson(`/api/tasks?workspace_id=${workspace}`);if(version===loadEpoch.current)setTasks(data);}catch(e){if(version===loadEpoch.current&&e.name!=="AbortError")setErr(String(e.message||e));}
    finally{if(version===loadEpoch.current)setLoaded(true);}
  },[workspace]);
  const loadLatest=useRef(loadTasks);loadLatest.current=loadTasks;
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
  useEffect(()=>{loadTasks();loadNotifs();loadNtfy();
    apiJson("/api/tasks/events").then(d=>setEventNames(d.events||[])).catch(()=>{});
  },[loadTasks,loadNotifs,loadNtfy]);

  const saveTask=async()=>{
    if(!form)return;
    setErr("");
    if(saving)return;
    const body=taskBody(form);
    if(form.schedule_kind==='event'&&!form.event){setErr('Pick an event for the trigger.');return;}
    setSaving(true);
    try{
      const r=await fetch(`${API}/api/tasks${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);loadLatest.current();
    }catch(e){setErr(String(e.message||e));}finally{setSaving(false);}
  };
  const taskAction=async(id,action)=>{
    try{
      if(action==="delete"){if(!await confirmAction({title:"Delete task",body:"Delete this scheduled task and its run history?",confirmLabel:"Delete",tone:"danger"}))return;await apiJson(`/api/tasks/${id}`,{method:"DELETE"});}
      else await apiJson(`/api/tasks/${id}/${action}`,{method:"POST"});
    }catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:`Task ${action} failed`,detail:String(e.message||e)});}
    finally{loadLatest.current();} // reload either way so buttons reflect server truth
  };
  const refreshRuns=useCallback(async id=>{
    const version=++runsEpoch.current;
    try{const data=await apiJson(`/api/tasks/${id}/runs`);if(version===runsEpoch.current)setRuns(data);}catch(e){if(version===runsEpoch.current&&e.name!=="AbortError")setErr(e.message);}
  },[]);
  useEffect(()=>{if(!runsFor)return;const timer=setInterval(()=>{refreshRuns(runsFor);loadTasks();},4000);return()=>clearInterval(timer);},[runsFor,refreshRuns,loadTasks]);
  useEffect(()=>{const c=new AbortController();Promise.all([apiJson('/api/builtin-tools',{signal:c.signal}),apiJson('/api/tools',{signal:c.signal}),apiJson('/api/connector-tools',{signal:c.signal})]).then(groups=>setToolOptions(groups.flatMap(g=>Array.isArray(g)?g:g.tools||[]))).catch(e=>{if(e.name!=='AbortError')setErr(e.message);});return()=>c.abort();},[]);
  useEffect(()=>{setPreview(null);if(!form)return;const c=new AbortController();const timer=setTimeout(()=>apiJson('/api/tasks/preview',{method:'POST',body:taskBody(form),signal:c.signal}).then(setPreview).catch(e=>{if(e.name!=='AbortError')setPreview({error:e.message});}),350);return()=>{clearTimeout(timer);c.abort();};},[form]);
  const showRuns=async id=>{
    if(runsFor===id){runsEpoch.current++;setRunsFor(null);return;}
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
      setForm({...EMPTY_FORM,workspace_id:workspace,title:d.title||"",prompt:d.prompt||"",task_type:d.task_type||"llm",
        schedule_kind:d.schedule_kind||"once",time:sj.time||"08:30",weekday:sj.weekday??0,day:sj.day??1,
        cron:sj.cron||"",run_at:sj.run_at||""});
      setNlText("");
    }catch(e){setErr(String(e.message||e));}
    finally{setNlBusy(false);}
  };
  const markAllSeen=async()=>{
    try{await apiJson("/api/notifications/seen",{method:"POST",body:{}});}
    catch(e){setErr(String(e.message||e));}
    finally{loadNotifs();}
  };
  const saveNtfy=async()=>{
    try{
      await apiJson("/api/notifications/channels/ntfy",{method:"PUT",
        body:{url:ntfy.url,topic:ntfy.topic,token:ntfy.token,allow_private:!!ntfy.allow_private}});
      notify&&notify({type:"success",text:"Channel saved",duration:2000});
    }catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Channel save failed",detail:String(e.message||e)});}
    finally{loadNtfy();}
  };

  const kindLabel=k=>({once:"once",daily:"daily",weekly:"weekly",monthly:"monthly",cron:"cron",event:"on event",webhook:"webhook"})[k]||k;
  const fmtTs=fmtUtcToLocal;
  const statusColor=s=>s==="succeeded"?t.ok:s==="failed"?t.err:s==="running"?t.warm:t.mut;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:embedded?"visible":"hidden"}}>
    <PanelHeader embedded={embedded} t={t} icon={<IC.Clock/>} title="Scheduled Tasks"
      subtitle="Recurring agent work, reminders, and the notification queue."
      nav={embedded?null:<>
        <button onClick={()=>setTab("tasks")} style={{...btnS(tab==="tasks"?t.acc:t.mut),padding:"5px 12px"}}>Tasks</button>
        <button onClick={()=>setTab("notifications")} style={{...btnS(tab==="notifications"?t.acc:t.mut),padding:"5px 12px"}}>
          Notifications{unseen>0?` (${unseen})`:""}
        </button>
      </>}>
      <button aria-label="Refresh" onClick={()=>{loadTasks();loadNotifs();}} style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      {tab==="tasks"&&<button onClick={()=>setForm({...EMPTY_FORM,workspace_id:workspace})} style={btnS(t.warm)}><IC.Plus/> New Task</button>}
    </PanelHeader>
    <div style={{overflowY:embedded?"visible":"auto",padding:embedded?0:isMobile?"14px 12px":"20px 28px",flex:1}}>
      <div style={{maxWidth:900}}>
        {err&&<div style={{color:t.err,fontSize:"var(--hc-text-secondary)",marginBottom:12}}>{err}</div>}

        {tab==="tasks"&&<>
          {!embedded&&<select aria-label="Tasks workspace" disabled={!!workspaceId} value={workspace} onChange={e=>setWorkspace(e.target.value)} style={{...inputS,width:'auto',marginBottom:12}}><option value="">All workspaces</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select>}

          <div style={{...cardS,display:"flex",gap:8,alignItems:"center",marginBottom:16}}>
            <input value={nlText} onChange={e=>setNlText(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")parseNl();}}
              placeholder='Describe it: "every weekday at 8am summarize AI news"...' style={{...inputS,flex:1}}/>
            <button onClick={parseNl} disabled={nlBusy} style={btnS(t.acc)}>{nlBusy?"Parsing...":"Draft Task"}</button>
          </div>

          {form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.acc}55`}}>
            <div style={{fontSize:"var(--hc-text-secondary)",fontWeight:600,color:t.acc,marginBottom:10}}>{form.id?"Edit task":"New task (review before saving)"}</div>
            <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,marginBottom:8}}/>
            <textarea value={form.prompt} onChange={e=>setForm(f=>({...f,prompt:e.target.value}))} placeholder="What should the agent do each run?" rows={3} style={{...inputS,marginBottom:8,resize:"vertical"}}/>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8,alignItems:"center"}}>
              {form.task_type==="llm"||form.task_type==="research"
                ?<SelectionControl label="Task task type" value={form.task_type} onChange={value=>setForm(f=>({...f,task_type:value}))} options={[{value:'llm',label:'Agent task'},{value:'research',label:'Deep research'}]}/>
                :<span style={{fontSize:"var(--hc-text-secondary)",color:t.warm,border:`1px solid ${t.warm}44`,borderRadius:"var(--hc-radius)",padding:"5px 10px"}}>
                  {form.task_type==="check_in"?"check-in — managed in the Assistant panel":form.task_type}
                </span>}
              <SelectionControl label="Task schedule kind" value={form.schedule_kind} onChange={value=>setForm(f=>({...f,schedule_kind:value}))} options={Object.keys(KIND_FIELDS).map(value=>({value,label:value}))}/>
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("time")&&<input type="time" value={form.time} onChange={e=>setForm(f=>({...f,time:e.target.value}))} style={{...inputS,width:"auto"}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("weekday")&&<SelectionControl label="Task weekday" value={form.weekday} onChange={value=>setForm(f=>({...f,weekday:value}))} options={WEEKDAYS.map((label,value)=>({value,label}))}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("day")&&<input type="number" min={1} max={31} value={form.day} onChange={e=>setForm(f=>({...f,day:e.target.value}))} style={{...inputS,width:70}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("cron")&&<input value={form.cron} onChange={e=>setForm(f=>({...f,cron:e.target.value}))} placeholder="*/30 * * * *" style={{...inputS,width:160}}/>}
              {(KIND_FIELDS[form.schedule_kind]||[]).includes("run_at")&&<input type="datetime-local" value={form.run_at} onChange={e=>setForm(f=>({...f,run_at:e.target.value}))} style={{...inputS,width:"auto"}}/>}
              {form.schedule_kind==="event"&&<>
                <SelectionControl label="Task event" value={form.event} onChange={value=>setForm(f=>({...f,event:value}))} options={[{value:'',label:'When…'},...eventNames.map(value=>({value,label:value.replace(/_/g,' ')}))]}/>
                <label style={{fontSize:"var(--hc-text-secondary)",color:t.mut,display:"flex",gap:5,alignItems:"center"}}>
                  every <input type="number" min={1} value={form.every} onChange={e=>setForm(f=>({...f,every:e.target.value}))} style={{...inputS,width:56}}/> occurrence(s)
                </label>
              </>}
              <label style={{fontSize:"var(--hc-text-secondary)",color:t.mut,display:"flex",gap:5,alignItems:"center"}}>
                <input type="checkbox" checked={!!form.notify} onChange={e=>setForm(f=>({...f,notify:e.target.checked}))}/> notify when done
              </label>
            </div>
            <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(190px,100%),1fr))',gap:10,marginBottom:12}}>
              {!embedded&&<label>Workspace<select disabled={form.task_type==='check_in'} value={form.workspace_id||''} onChange={e=>setForm(f=>({...f,workspace_id:e.target.value}))} style={inputS}><option value="">No workspace</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select></label>}
              <div><span className="hc-field-label">Model</span><ModelPicker label="Task model" placeholder="Inherit existing defaults" value={form.model||''} onChange={value=>setForm(f=>({...f,model:value}))} models={[...new Set([...models,form.model].filter(Boolean))]} t={t} font="inherit"/><button style={btnS(t.mut)} onClick={()=>setForm(f=>({...f,model:''}))}>Inherit existing defaults</button></div>
              <label>Timezone<input value={form.timezone||''} onChange={e=>setForm(f=>({...f,timezone:e.target.value}))} placeholder="Assistant timezone (default)" style={inputS}/></label>
              <label>Conversation<select value={form.conversation_id||''} disabled={form.task_type==='check_in'} onChange={e=>setForm(f=>({...f,conversation_id:e.target.value}))} style={inputS}><option value="">{form.workspace_id?'Create a project task conversation':'Result in run history'}</option>{convs.map(c=><option key={c.id} value={c.id}>{c.title||'New chat'}</option>)}{form.conversation_id&&!convs.some(c=>c.id===form.conversation_id)&&<option value={form.conversation_id}>Current conversation</option>}</select></label>
            </div>
            <details style={{marginBottom:12}}><summary>Tools and delivery</summary><p style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>No selected tools inherits the conversation or persona tools. Workspace agent tasks save results to a project conversation. Tasks without a conversation use a single completion.</p>
              {toolOptions.map(tool=><label key={tool.id} style={{display:'block'}}><input type="checkbox" checked={(form.tool_ids||[]).includes(tool.id)} onChange={e=>setForm(f=>({...f,tool_ids:e.target.checked?[...(f.tool_ids||[]),tool.id]:(f.tool_ids||[]).filter(id=>id!==tool.id)}))}/>{tool.name||tool.tool_name}</label>)}
              {['ntfy','email'].map(channel=><label key={channel} style={{display:'block'}}><input type="checkbox" checked={!!form.delivery?.[channel]} onChange={e=>setForm(f=>({...f,delivery:{...f.delivery,[channel]:e.target.checked}}))}/>{channel} delivery (requires configured channel)</label>)}
            </details>
            {/^(openai|anthropic|custom):/.test(form.model||'')&&<p style={{color:t.warm}}>This explicitly selected provider can incur charges on every run.</p>}
            <div role="status" style={{marginBottom:12,color:preview?.error?t.err:t.mut}}>{!preview?'Checking schedule…':preview.error||`${preview.timezone}: ${preview.occurrences.length?preview.occurrences.map(fmtUtcToLocal).join(' · '):'Runs when triggered'}`}</div>
            <div style={{display:"flex",gap:8}}>
              <button disabled={saving||!preview||!!preview.error} onClick={saveTask} style={btnS(t.ok)}>{form.id?"Save":"Create"}</button>
              <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
            </div>
          </div>}

          {!loaded&&!form?Array.from({length:3},(_,i)=><SkeletonCard key={i} t={t} h={84} lines={2} style={{marginBottom:10}}/>)
          :tasks.length===0&&!form?<EmptyState t={t} icon={<IC.Clock/>} title="No scheduled tasks yet" hint='Create one above, or just ask in chat: "remind me every morning at 8 to stretch".'/>
          :tasks.map(task=><div key={task.id} style={{...cardS,marginBottom:10,opacity:task.enabled?1:.55}}>
            <div style={{display:"flex",alignItems:"center",gap:10,flexWrap:"wrap"}}>
              <div style={{flex:1,minWidth:200}}>
                <div style={{fontSize:"var(--hc-text-control)",fontWeight:600,color:t.text}}>{task.title}</div>
                <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:3}}>
                  {kindLabel(task.schedule_kind)}
                  {task.next_run?` · next ${fmtTs(task.next_run)}`:""}
                  {task.last_status?<span> · last <span style={{color:statusColor(task.last_status)}}>{task.last_status}</span></span>:null}
                  {task.task_type!=="llm"?` · ${task.task_type}`:""}
                </div>
              </div>
              <div style={{display:"flex",gap:6,flexWrap:"wrap",alignItems:"center"}}>
                <button onClick={()=>taskAction(task.id,"run")} title="Run now" style={{...btnS(t.ok),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}><IC.Play/> Run</button>
                <button onClick={()=>taskAction(task.id,task.enabled?"pause":"resume")} style={{...btnS(task.enabled?t.warm:t.ok),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}>{task.enabled?"Pause":"Resume"}</button>
                <button aria-label="Edit" onClick={()=>{const sj=task.schedule_json||{};const dj=task.delivery_json||{};const ej=task.event_trigger_json||{};setForm({...EMPTY_FORM,id:task.id,title:task.title,prompt:task.prompt,task_type:task.task_type,schedule_kind:task.schedule_kind,time:sj.time||"08:30",weekday:sj.weekday??0,day:sj.day??1,cron:sj.cron||"",run_at:sj.run_at||"",event:ej.event||"",every:ej.every||1,workspace_id:task.workspace_id||"",model:task.model||"",tool_ids:task.tool_ids||[],timezone:task.timezone||"",conversation_id:task.conversation_id,notify:dj.notify!==false,delivery:dj});}} style={{...btnS(t.acc),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}><IC.Pencil/></button>
                <button onClick={()=>showRuns(task.id)} style={{...btnS(t.mut),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}>History</button>
                <button aria-label="Delete" onClick={()=>taskAction(task.id,"delete")} style={{...btnS(t.err),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}><IC.Trash/></button>
              </div>
            </div>
            {task.schedule_kind==="webhook"&&task.webhook_token&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:6,wordBreak:"break-all"}}>
              Webhook: POST {API||window.location.origin}/api/hooks/{task.webhook_token}
            </div>}
            {task.prompt&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginTop:6,whiteSpace:"pre-wrap"}}>{task.prompt.slice(0,300)}{task.prompt.length>300?"…":""}</div>}
            {runsFor===task.id&&<div style={{marginTop:10,borderTop:"1px solid var(--hc-line)",paddingTop:8}}>
              {runs.length===0?<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>No runs yet.</div>
              :runs.map(r=><div key={r.id} style={{fontSize:"var(--hc-text-secondary)",color:t.dim,padding:"3px 0",display:"flex",gap:8,alignItems:"baseline"}}>
                <span style={{color:statusColor(r.status),fontWeight:700,minWidth:70}}>{r.status}</span>
                <span style={{color:t.mut}}>{fmtTs(r.started_at)}</span>
                <details style={{flex:1,minWidth:0,overflowWrap:"anywhere"}}><summary>Read result</summary><div style={{whiteSpace:"pre-wrap"}}>{r.error||r.result_text||"No output saved yet."}</div>{onNavigate&&r.conversation_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('chat',r.conversation_id)}>Open conversation</button>}{onNavigate&&r.report_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('research',r.report_id)}>Open report</button>}</details>
                {r.status==="running"&&<button onClick={async()=>{try{await apiJson(`/api/tasks/runs/${r.id}/cancel`,{method:"POST"});}catch(e){setErr(String(e.message||e));}finally{refreshRuns(task.id);loadTasks();}}} style={{...btnS(t.err),padding:"2px 6px",fontSize:"var(--hc-text-control)"}}>Stop</button>}
              </div>)}
            </div>}
          </div>)}
        </>}

        {tab==="notifications"&&<>
          <div style={{display:"flex",gap:8,marginBottom:14,alignItems:"center"}}>
            <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{unseen} unseen</div>
            <div style={{flex:1}}/>
            <button onClick={markAllSeen} style={btnS(t.acc)}>Mark all seen</button>
          </div>
          {notifs.length===0?<EmptyState t={t} icon={<IC.Flash/>} title="No notifications" hint="Scheduled task results and reminders will land here."/>
          :notifs.map(n=><div key={n.id} style={{...cardS,marginBottom:8,borderLeft:`3px solid ${n.seen?t.brd:t.warm}`,display:"flex",gap:10,alignItems:"flex-start"}}>
            <div style={{flex:1,minWidth:0}}>
              <div style={{fontSize:"var(--hc-text-secondary)",fontWeight:n.seen?600:800,color:t.text}}>{n.title}</div>
              {n.body&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginTop:3,whiteSpace:"pre-wrap"}}>{n.body}</div>}
              {(n.deliveries||[]).map(d=><div key={d.channel} style={{fontSize:"var(--hc-text-secondary)",color:d.status==='uncertain'||d.status==='failed'?t.err:t.dim,marginTop:4}}>{d.channel}: {d.status.replaceAll('_',' ')} · {d.attempts} attempt{d.attempts===1?'':'s'}{d.error?` · ${d.error}`:''}{d.status==='uncertain'?' — Check the destination before sending again.':''}</div>)}
              <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:4}}>{n.kind} · {fmtTs(n.created_at)}</div>
            </div>
            <button aria-label="Delete" onClick={async()=>{try{await apiJson(`/api/notifications/${n.id}`,{method:"DELETE"});}catch(e){setErr(String(e.message||e));}finally{loadNotifs();}}} style={{...btnS(t.mut),padding:"3px 7px",fontSize:"var(--hc-text-control)"}}><IC.Trash/></button>
          </div>)}

          <div style={{...cardS,marginTop:20}}>
            <div style={{fontSize:"var(--hc-text-secondary)",fontWeight:600,color:t.acc,marginBottom:8}}>ntfy push channel</div>
            <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:10}}>Optional push notifications via an ntfy server. Tasks with the ntfy delivery option send here; in-app notifications always work.</div>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
              <input value={ntfy.url} onChange={e=>setNtfy(n=>({...n,url:e.target.value}))} placeholder="https://ntfy.sh" style={{...inputS,flex:2,minWidth:180}}/>
              <input value={ntfy.topic} onChange={e=>setNtfy(n=>({...n,topic:e.target.value}))} placeholder="topic" style={{...inputS,flex:1,minWidth:120}}/>
              <input value={ntfy.token} onChange={e=>setNtfy(n=>({...n,token:e.target.value}))} placeholder={ntfy.token_set?"token saved — leave blank to keep":"access token (optional)"} type="password" style={{...inputS,flex:1,minWidth:140}}/>
            </div>
            <label style={{fontSize:"var(--hc-text-secondary)",color:t.dim,display:"flex",gap:6,alignItems:"center",marginBottom:10}}>
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
