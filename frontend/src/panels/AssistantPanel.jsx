import React,{useState,useEffect,useCallback,useRef} from 'react';

import { API } from '../session.js';
import { fmtUtcToLocal } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { SkeletonCard } from '../components/Skeleton.jsx';

const WEEKDAYS=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const CADENCES=["daily","weekly","monthly","cron"];
const isCloudModel=m=>/^(openai|anthropic|custom):/.test(m||"");

function CheckInRow({t,btnS,inputS,ci,patch,confirmAction,runNow}){
  const sj=ci.schedule_json||{};
  const kind=CADENCES.includes(ci.schedule_kind)?ci.schedule_kind:"daily";
  return <div style={{borderTop:`1px solid ${t.brd}22`,paddingTop:10,marginBottom:10}}>
    <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginBottom:6}}>
      <input defaultValue={ci.title} onBlur={e=>{if(e.target.value!==ci.title)patch({check_ins:[{id:ci.id,name:e.target.value}]});}} style={{...inputS,flex:1,minWidth:140}}/>
      <select value={kind} onChange={e=>patch({check_ins:[{id:ci.id,schedule_kind:e.target.value,...(e.target.value==="cron"?{cron:sj.cron||"0 8 * * *"}:{})}]})} style={{...inputS,width:"auto"}}>
        {CADENCES.map(k=><option key={k} value={k}>{k}</option>)}
      </select>
      {kind!=="cron"&&<input type="time" defaultValue={sj.time||"08:30"} onBlur={e=>{if(e.target.value&&e.target.value!==(sj.time||"08:30"))patch({check_ins:[{id:ci.id,time:e.target.value}]});}} style={{...inputS,width:"auto"}}/>}
      {kind==="weekly"&&<select value={sj.weekday??0} onChange={e=>patch({check_ins:[{id:ci.id,weekday:Number(e.target.value)}]})} style={{...inputS,width:"auto"}}>
        {WEEKDAYS.map((w,i)=><option key={i} value={i}>{w}</option>)}
      </select>}
      {kind==="monthly"&&<input type="number" min={1} max={31} defaultValue={sj.day??1} onBlur={e=>{const v=Number(e.target.value)||1;if(v!==(sj.day??1))patch({check_ins:[{id:ci.id,day:v}]});}} style={{...inputS,width:64}}/>}
      {kind==="cron"&&<input defaultValue={sj.cron||""} placeholder="0 8 * * 1-5" onBlur={e=>{if(e.target.value&&e.target.value!==(sj.cron||""))patch({check_ins:[{id:ci.id,cron:e.target.value}]});}} style={{...inputS,width:130}}/>}
      <button onClick={()=>runNow(ci.id)} title="Run now" style={{...btnS(t.ok),padding:"5px 10px",fontSize:10}}>▶ Run</button>
      <button onClick={()=>patch({check_ins:[{id:ci.id,enabled:!ci.enabled}]})} style={{...btnS(ci.enabled?t.ok:t.mut),padding:"5px 10px",fontSize:10}}>{ci.enabled?"On":"Off"}</button>
      <button onClick={async()=>{if(await confirmAction({title:"Delete check-in",body:"Delete this check-in?",confirmLabel:"Delete",tone:"danger"}))patch({check_ins:[{id:ci.id,delete:true}]});}} style={{...btnS(t.err),padding:"5px 8px",fontSize:10}}><IC.Trash/></button>
    </div>
    <input defaultValue={ci.prompt||""} placeholder="Focus (optional — e.g. 'emphasize deadlines')" onBlur={e=>{if(e.target.value!==(ci.prompt||""))patch({check_ins:[{id:ci.id,prompt:e.target.value}]});}} style={{...inputS,width:"100%",marginBottom:4,fontSize:11}}/>
    <div style={{fontSize:9,color:t.mut}}>
      {ci.enabled&&ci.next_run?`next ${fmtUtcToLocal(ci.next_run)}`:!ci.enabled?"paused":""}
      {ci.last_status?` · last ${ci.last_status}`:""}
    </div>
  </div>;
}

export default function AssistantPanel({t,btnS,cardS,inputS,confirmAction,models,openAssistantChat}){
  const [data,setData]=useState(null);
  const [timezones,setTimezones]=useState([]);
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState("");
  const [draft,setDraft]=useState(null);   // editable copy of persona+profile
  const [brief,setBrief]=useState(null);   // latest assistant message
  const [speaking,setSpeaking]=useState(false);
  const [newCheckIn,setNewCheckIn]=useState({name:"Morning check-in",time:"08:30",prompt:""});
  const audioRef=useRef(null);

  const load=useCallback(async()=>{
    try{
      const r=await fetch(`${API}/api/assistant`);
      const d=await r.json();
      setData(d);
      setDraft({
        name:d.persona?.name||"",
        personality:d.persona?.personality||"",
        model:d.persona?.model||"",
        timezone:d.profile?.timezone||"UTC",
        location:d.profile?.location||"",
        enabled_gatherers:d.profile?.enabled_gatherers||[],
        tool_ids:d.persona?.tool_ids||[],
        codeagent:(d.persona?.tool_ids||[]).includes("codeagent"),
        allow_autonomous_email:!!d.profile?.allow_autonomous_email,
        quiet_hours:{enabled:false,start:"22:00",end:"07:00",urgent_override:true,...(d.profile?.quiet_hours||{})},
      });
    }catch(e){setErr(String(e));}
  },[]);
  const loadBrief=useCallback(async()=>{
    try{const r=await fetch(`${API}/api/assistant/brief`);const d=await r.json();setBrief(d.message||null);}catch{}
  },[]);
  useEffect(()=>{
    load();loadBrief();
    fetch(`${API}/api/assistant/timezones`).then(r=>r.json()).then(d=>setTimezones(d.timezones||[])).catch(()=>{});
    const onFocus=()=>{load();loadBrief();};
    window.addEventListener("focus",onFocus);
    return()=>window.removeEventListener("focus",onFocus);
  },[load,loadBrief]);

  const patch=async body=>{
    setBusy(true);setErr("");
    try{
      const r=await fetch(`${API}/api/assistant`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      const d=await r.json();setData(d);
    }catch(e){setErr(String(e.message||e));}
    finally{setBusy(false);}
  };
  const saveProfile=()=>{
    // Merge the codeagent checkbox into the persona's EXISTING tool list —
    // overwriting it wholesale wiped tools added via the Agents panel.
    const tools=(draft.tool_ids||[]).filter(x=>x!=="codeagent");
    if(draft.codeagent)tools.push("codeagent");
    return patch({
      name:draft.name,personality:draft.personality,model:draft.model,
      timezone:draft.timezone,location:draft.location,
      enabled_gatherers:draft.enabled_gatherers,
      allow_autonomous_email:draft.allow_autonomous_email,
      quiet_hours:draft.quiet_hours,
      tool_ids:tools,
    });
  };
  const runNow=async id=>{
    setErr("");
    try{
      const r=await fetch(`${API}/api/tasks/${id}/run`,{method:"POST"});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      setTimeout(()=>{load();loadBrief();},4000);
    }catch(e){setErr(String(e.message||e));}
  };
  const speakBrief=async()=>{
    if(!brief?.content||speaking)return;
    setSpeaking(true);setErr("");
    try{
      const r=await fetch(`${API}/api/audio/speech`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:brief.content})});
      if(!r.ok)throw new Error("Text-to-speech is unavailable (set the TTS URL in Settings).");
      const url=URL.createObjectURL(await r.blob());
      if(audioRef.current)audioRef.current.pause();
      audioRef.current=new Audio(url);
      audioRef.current.onended=()=>{setSpeaking(false);URL.revokeObjectURL(url);};
      audioRef.current.onerror=()=>{setSpeaking(false);URL.revokeObjectURL(url);};
      await audioRef.current.play();
    }catch(e){setSpeaking(false);setErr(String(e.message||e));}
  };

  if(!data||!draft){
    if(err)return <div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",color:t.err,fontSize:12}}>{err}</div>;
    return <div style={{flex:1,overflowY:"auto",padding:"20px 28px"}}>
      <div style={{maxWidth:820,display:"flex",flexDirection:"column",gap:16}}>
        {Array.from({length:3},(_,i)=><SkeletonCard key={i} t={t} h={i===0?150:100} lines={i===0?3:2}/>)}
      </div>
    </div>;
  }
  const gatherers=data.gatherers||[];
  const checkIns=data.check_ins||[];
  const qh=draft.quiet_hours;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.warm} icon={<IC.Bot/>} title="Personal Assistant"
      subtitle="Your proactive agent: pinned chat, check-ins, and scheduled work.">
      <button onClick={()=>{load();loadBrief();}} style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      <button onClick={()=>openAssistantChat&&openAssistantChat(data.profile?.conversation_id)} style={btnS(t.warm)}><IC.Chat/> Open Assistant Chat</button>
    </PanelHeader>
    <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
      <div style={{maxWidth:820}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}

        {brief&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.warm}44`}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:8}}>
            <div style={{fontSize:12,fontWeight:800,color:t.warm}}>Latest brief</div>
            <div style={{fontSize:9,color:t.mut}}>{fmtUtcToLocal(brief.created_at)}</div>
            <div style={{flex:1}}/>
            <button onClick={speakBrief} disabled={speaking} title="Read aloud" style={{...btnS(t.acc),padding:"3px 9px",fontSize:10}}>{speaking?"Speaking…":"🔊 Speak"}</button>
            <button onClick={()=>openAssistantChat&&openAssistantChat(data.profile?.conversation_id)} style={{...btnS(t.mut),padding:"3px 9px",fontSize:10}}>Open in chat</button>
          </div>
          <div style={{fontSize:11,color:t.dim,whiteSpace:"pre-wrap",maxHeight:220,overflowY:"auto",lineHeight:1.5}}>{brief.content}</div>
        </div>}

        <div style={{...cardS,marginBottom:16}}>
          <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:10}}>Profile</div>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
            <div style={{flex:1,minWidth:180}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Name</div>
              <input value={draft.name} onChange={e=>setDraft(d=>({...d,name:e.target.value}))} style={inputS}/>
            </div>
            <div style={{flex:1,minWidth:180}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Model</div>
              {Array.isArray(models)&&models.length>0
                ?<select value={draft.model} onChange={e=>setDraft(d=>({...d,model:e.target.value}))} style={inputS}>
                  {!models.includes(draft.model)&&draft.model&&<option value={draft.model}>{draft.model}</option>}
                  {models.map(m=><option key={m} value={m}>{m}</option>)}
                </select>
                :<input value={draft.model} onChange={e=>setDraft(d=>({...d,model:e.target.value}))} style={inputS}/>}
              {isCloudModel(draft.model)&&<div style={{fontSize:9,color:t.warm,marginTop:3}}>⚠ Cloud model — every scheduled check-in will spend API credits in the background.</div>}
            </div>
            <div style={{flex:1,minWidth:180}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Timezone</div>
              <select value={draft.timezone} onChange={e=>setDraft(d=>({...d,timezone:e.target.value}))} style={inputS}>
                {!timezones.includes(draft.timezone)&&<option value={draft.timezone}>{draft.timezone}</option>}
                {timezones.map(z=><option key={z} value={z}>{z}</option>)}
              </select>
            </div>
            <div style={{flex:1,minWidth:180}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Location (for weather in briefs)</div>
              <input value={draft.location} onChange={e=>setDraft(d=>({...d,location:e.target.value}))} placeholder="e.g. Austin, TX" style={inputS}/>
            </div>
          </div>
          <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Personality</div>
          <textarea value={draft.personality} onChange={e=>setDraft(d=>({...d,personality:e.target.value}))} rows={7} style={{...inputS,resize:"vertical",marginBottom:8,fontSize:11,lineHeight:1.5}}/>
          <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center",marginBottom:6}}>
            <input type="checkbox" checked={draft.codeagent} onChange={e=>setDraft(d=>({...d,codeagent:e.target.checked}))}/>
            Enable CodeAgent tools (code execution, shell, files, email, web research, weather)
          </label>
          <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center",marginBottom:10}}>
            <input type="checkbox" checked={draft.allow_autonomous_email} onChange={e=>setDraft(d=>({...d,allow_autonomous_email:e.target.checked}))}/>
            Allow autonomous email sending (master switch — each email account's own "allow send" toggle must also be on; off = replies are saved as drafts)
          </label>
          <button onClick={saveProfile} disabled={busy} style={btnS(t.ok)}>{busy?"Saving...":"Save Profile"}</button>
        </div>

        <div style={{...cardS,marginBottom:16}}>
          <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:6}}>Quiet hours</div>
          <div style={{fontSize:10,color:t.mut,marginBottom:10}}>Pauses ntfy/email pushes during the window (assistant timezone). In-app notifications always land.</div>
          <div style={{display:"flex",gap:12,flexWrap:"wrap",alignItems:"center",marginBottom:10}}>
            <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center"}}>
              <input type="checkbox" checked={!!qh.enabled} onChange={e=>setDraft(d=>({...d,quiet_hours:{...d.quiet_hours,enabled:e.target.checked}}))}/>
              Enabled
            </label>
            <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center"}}>
              from <input type="time" value={qh.start} onChange={e=>setDraft(d=>({...d,quiet_hours:{...d.quiet_hours,start:e.target.value}}))} style={{...inputS,width:"auto"}}/>
              to <input type="time" value={qh.end} onChange={e=>setDraft(d=>({...d,quiet_hours:{...d.quiet_hours,end:e.target.value}}))} style={{...inputS,width:"auto"}}/>
            </label>
            <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center"}}>
              <input type="checkbox" checked={qh.urgent_override!==false} onChange={e=>setDraft(d=>({...d,quiet_hours:{...d.quiet_hours,urgent_override:e.target.checked}}))}/>
              Urgent email still pushes
            </label>
          </div>
          <button onClick={saveProfile} disabled={busy} style={btnS(t.ok)}>Save</button>
        </div>

        <div style={{...cardS,marginBottom:16}}>
          <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:6}}>Check-in data sources</div>
          <div style={{fontSize:10,color:t.mut,marginBottom:10}}>What the assistant gathers before writing a check-in brief. Empty selection = everything.</div>
          <div style={{display:"flex",gap:12,flexWrap:"wrap",marginBottom:10}}>
            {gatherers.map(g=><label key={g} style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center"}}>
              <input type="checkbox"
                checked={draft.enabled_gatherers.length===0||draft.enabled_gatherers.includes(g)}
                onChange={e=>{
                  setDraft(d=>{
                    let list=d.enabled_gatherers.length===0?[...gatherers]:[...d.enabled_gatherers];
                    list=e.target.checked?[...new Set([...list,g])]:list.filter(x=>x!==g);
                    if(list.length===gatherers.length)list=[];
                    return {...d,enabled_gatherers:list};
                  });
                }}/>
              {g}
            </label>)}
          </div>
          <button onClick={saveProfile} disabled={busy} style={btnS(t.ok)}>Save</button>
        </div>

        <div style={{...cardS,marginBottom:16}}>
          <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:6}}>Check-ins</div>
          <div style={{fontSize:10,color:t.mut,marginBottom:12}}>Scheduled briefs posted in the assistant chat: gathered context → prioritized summary → actions. Daily, weekly, monthly, or cron.</div>
          {checkIns.map(ci=><CheckInRow key={ci.id} t={t} btnS={btnS} inputS={inputS} ci={ci} patch={patch} confirmAction={confirmAction} runNow={runNow}/>)}
          <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginTop:12,borderTop:`1px solid ${t.brd}33`,paddingTop:12}}>
            <input value={newCheckIn.name} onChange={e=>setNewCheckIn(c=>({...c,name:e.target.value}))} placeholder="Name" style={{...inputS,flex:1,minWidth:140}}/>
            <input type="time" value={newCheckIn.time} onChange={e=>setNewCheckIn(c=>({...c,time:e.target.value}))} style={{...inputS,width:"auto"}}/>
            <input value={newCheckIn.prompt} onChange={e=>setNewCheckIn(c=>({...c,prompt:e.target.value}))} placeholder="Optional focus (e.g. 'emphasize deadlines')" style={{...inputS,flex:2,minWidth:180}}/>
            <button onClick={async()=>{await patch({check_ins:[{...newCheckIn}]});setNewCheckIn({name:"Morning check-in",time:"08:30",prompt:""});}} style={btnS(t.warm)}><IC.Plus/> Add</button>
          </div>
        </div>
      </div>
    </div>
  </div>;
}
