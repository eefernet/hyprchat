import React,{useState,useEffect,useCallback} from 'react';

import { API } from '../session.js';
import { fmtUtcMinute } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';

export default function AssistantPanel({t,btnS,cardS,inputS,confirmAction,models,openAssistantChat}){
  const [data,setData]=useState(null);
  const [timezones,setTimezones]=useState([]);
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState("");
  const [draft,setDraft]=useState(null);   // editable copy of persona+profile
  const [newCheckIn,setNewCheckIn]=useState({name:"Morning check-in",time:"08:30",prompt:""});

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
        enabled_gatherers:d.profile?.enabled_gatherers||[],
        codeagent:(d.persona?.tool_ids||[]).includes("codeagent"),
      });
    }catch(e){setErr(String(e));}
  },[]);
  useEffect(()=>{
    load();
    fetch(`${API}/api/assistant/timezones`).then(r=>r.json()).then(d=>setTimezones(d.timezones||[])).catch(()=>{});
  },[load]);

  const patch=async body=>{
    setBusy(true);setErr("");
    try{
      const r=await fetch(`${API}/api/assistant`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      const d=await r.json();setData(d);
    }catch(e){setErr(String(e.message||e));}
    finally{setBusy(false);}
  };
  const saveProfile=()=>patch({
    name:draft.name,personality:draft.personality,model:draft.model,
    timezone:draft.timezone,enabled_gatherers:draft.enabled_gatherers,
    tool_ids:draft.codeagent?["codeagent"]:[],
  });

  if(!data||!draft)return <div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",color:t.mut,fontSize:12}}>{err||"Loading assistant..."}</div>;
  const gatherers=data.gatherers||[];
  const checkIns=data.check_ins||[];

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.warm} icon={<IC.Bot/>} title="Personal Assistant"
      subtitle="Your proactive agent: pinned chat, daily check-ins, and scheduled work.">
      <button onClick={()=>openAssistantChat&&openAssistantChat(data.profile?.conversation_id)} style={btnS(t.warm)}><IC.Chat/> Open Assistant Chat</button>
    </PanelHeader>
    <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
      <div style={{maxWidth:820}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}

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
            </div>
            <div style={{flex:1,minWidth:180}}>
              <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Timezone</div>
              <select value={draft.timezone} onChange={e=>setDraft(d=>({...d,timezone:e.target.value}))} style={inputS}>
                {!timezones.includes(draft.timezone)&&<option value={draft.timezone}>{draft.timezone}</option>}
                {timezones.map(z=><option key={z} value={z}>{z}</option>)}
              </select>
            </div>
          </div>
          <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Personality</div>
          <textarea value={draft.personality} onChange={e=>setDraft(d=>({...d,personality:e.target.value}))} rows={7} style={{...inputS,resize:"vertical",marginBottom:8,fontSize:11,lineHeight:1.5}}/>
          <label style={{fontSize:11,color:t.dim,display:"flex",gap:6,alignItems:"center",marginBottom:10}}>
            <input type="checkbox" checked={draft.codeagent} onChange={e=>setDraft(d=>({...d,codeagent:e.target.checked}))}/>
            Enable CodeAgent tools (code execution, shell, files, downloads)
          </label>
          <button onClick={saveProfile} disabled={busy} style={btnS(t.ok)}>{busy?"Saving...":"Save Profile"}</button>
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
          <div style={{fontSize:12,fontWeight:800,color:t.acc,marginBottom:6}}>Daily check-ins</div>
          <div style={{fontSize:10,color:t.mut,marginBottom:12}}>Scheduled briefs posted in the assistant chat: gathered context → prioritized summary → actions.</div>
          {checkIns.map(ci=><div key={ci.id} style={{display:"flex",gap:8,alignItems:"center",marginBottom:8,flexWrap:"wrap"}}>
            <input defaultValue={ci.title} onBlur={e=>{if(e.target.value!==ci.title)patch({check_ins:[{id:ci.id,name:e.target.value}]});}} style={{...inputS,flex:1,minWidth:140}}/>
            <input type="time" defaultValue={(ci.schedule_json||{}).time||"08:30"} onBlur={e=>patch({check_ins:[{id:ci.id,time:e.target.value}]})} style={{...inputS,width:"auto"}}/>
            <button onClick={()=>patch({check_ins:[{id:ci.id,enabled:!ci.enabled}]})} style={{...btnS(ci.enabled?t.ok:t.mut),padding:"5px 10px",fontSize:10}}>{ci.enabled?"On":"Off"}</button>
            <button onClick={async()=>{if(await confirmAction({title:"Delete check-in",body:"Delete this check-in?",confirmLabel:"Delete",tone:"danger"}))patch({check_ins:[{id:ci.id,delete:true}]});}} style={{...btnS(t.err),padding:"5px 8px",fontSize:10}}><IC.Trash/></button>
            <div style={{width:"100%",fontSize:9,color:t.mut}}>
              {ci.next_run?`next ${fmtUtcMinute(ci.next_run)}`:""}
              {ci.last_status?` · last ${ci.last_status}`:""}
            </div>
          </div>)}
          <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginTop:12,borderTop:`1px solid ${t.brd}33`,paddingTop:12}}>
            <input value={newCheckIn.name} onChange={e=>setNewCheckIn(c=>({...c,name:e.target.value}))} placeholder="Name" style={{...inputS,flex:1,minWidth:140}}/>
            <input type="time" value={newCheckIn.time} onChange={e=>setNewCheckIn(c=>({...c,time:e.target.value}))} style={{...inputS,width:"auto"}}/>
            <input value={newCheckIn.prompt} onChange={e=>setNewCheckIn(c=>({...c,prompt:e.target.value}))} placeholder="Optional focus (e.g. 'emphasize deadlines')" style={{...inputS,flex:2,minWidth:180}}/>
            <button onClick={()=>patch({check_ins:[{...newCheckIn}]})} style={btnS(t.warm)}><IC.Plus/> Add</button>
          </div>
        </div>
      </div>
    </div>
  </div>;
}
