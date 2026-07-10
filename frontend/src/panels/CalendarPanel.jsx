import React,{useState,useEffect,useCallback} from 'react';

import { API } from '../session.js';
import { IC } from '../components/icons.jsx';

const EMPTY_EVENT={title:"",start_local:"",end_local:"",location:"",description:"",remind_minutes:""};
const EMPTY_ACCOUNT={label:"",url:"",username:"",password:"",calendar_url:""};
const pad=n=>String(n).padStart(2,"0");
const dkey=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;

export default function CalendarPanel({t,btnS,cardS,inputS}){
  const [events,setEvents]=useState([]);
  const [month,setMonth]=useState(()=>{const d=new Date();return new Date(d.getFullYear(),d.getMonth(),1);});
  const [form,setForm]=useState(null);
  const [accounts,setAccounts]=useState([]);
  const [acctForm,setAcctForm]=useState(null);
  const [showAccounts,setShowAccounts]=useState(false);
  const [syncing,setSyncing]=useState(false);
  const [err,setErr]=useState("");

  const load=useCallback(async()=>{
    const start=new Date(month);start.setDate(start.getDate()-7);
    const end=new Date(month.getFullYear(),month.getMonth()+1,7);
    try{
      const r=await fetch(`${API}/api/calendar/events?start_local=${dkey(start)}T00:00&end_local=${dkey(end)}T23:59`);
      setEvents(await r.json());
    }catch(e){setErr(String(e));}
  },[month]);
  const loadAccounts=useCallback(async()=>{
    try{const r=await fetch(`${API}/api/calendar/caldav`);setAccounts(await r.json());}catch{}
  },[]);
  useEffect(()=>{load();loadAccounts();},[load,loadAccounts]);

  const save=async()=>{
    if(!form?.title?.trim()||!form?.start_local)return setErr("Title and start time are required");
    setErr("");
    const body={title:form.title,start_local:form.start_local,end_local:form.end_local||"",
      location:form.location,description:form.description,
      ...(form.remind_minutes!==""?{remind_minutes:Number(form.remind_minutes)}:{})};
    try{
      const r=await fetch(`${API}/api/calendar/events${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);load();
    }catch(e){setErr(String(e.message||e));}
  };
  const remove=async id=>{
    if(!confirm("Delete this event?"))return;
    await fetch(`${API}/api/calendar/events/${id}`,{method:"DELETE"});load();
  };
  const saveAccount=async()=>{
    if(!acctForm?.url)return;
    const r=await fetch(`${API}/api/calendar/caldav${acctForm.id?`/${acctForm.id}`:""}`,{
      method:acctForm.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(acctForm)});
    if(r.ok){setAcctForm(null);loadAccounts();}
    else{const d=await r.json().catch(()=>({}));setErr(d.detail||`HTTP ${r.status}`);}
  };
  const testAccount=async id=>{
    setErr("");
    const r=await fetch(`${API}/api/calendar/caldav/${id}/test`,{method:"POST"});
    const d=await r.json().catch(()=>({}));
    if(r.ok)alert("Connected. Calendars:\n"+(d.calendars||[]).map(c=>`${c.name}: ${c.url}`).join("\n"));
    else setErr(d.detail||"Connection failed");
  };
  const syncNow=async()=>{
    setSyncing(true);setErr("");
    try{
      const r=await fetch(`${API}/api/calendar/sync`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||"Sync failed");
      load();loadAccounts();
    }catch(e){setErr(String(e.message||e));}
    finally{setSyncing(false);}
  };

  const byDay={};
  for(const e of events){const k=(e.start_local||"").slice(0,10);(byDay[k]=byDay[k]||[]).push(e);}
  const firstDow=(month.getDay()+6)%7; // Monday-first
  const daysInMonth=new Date(month.getFullYear(),month.getMonth()+1,0).getDate();
  const todayKey=dkey(new Date());
  const cells=[];
  for(let i=0;i<firstDow;i++)cells.push(null);
  for(let d=1;d<=daysInMonth;d++)cells.push(new Date(month.getFullYear(),month.getMonth(),d));
  const upcoming=[...events].filter(e=>(e.start_local||"")>=todayKey).slice(0,15);
  const monthLabel=month.toLocaleDateString(undefined,{month:"long",year:"numeric"});

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"14px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",alignItems:"center",gap:8,flexShrink:0,flexWrap:"wrap"}}>
      <span style={{display:"flex",color:t.f1||t.acc}}><IC.Clock/></span>
      <div style={{marginRight:12}}>
        <div style={{fontSize:14,fontWeight:800,letterSpacing:1,textTransform:"uppercase",color:t.f1||t.acc}}>Calendar</div>
        <div style={{fontSize:10,color:t.mut,marginTop:2}}>Times shown in your assistant timezone. CalDAV keeps your phone in sync.</div>
      </div>
      <button onClick={()=>setMonth(m=>new Date(m.getFullYear(),m.getMonth()-1,1))} style={{...btnS(t.mut),padding:"5px 10px"}}>◀</button>
      <div style={{fontSize:12,fontWeight:800,color:t.text,minWidth:130,textAlign:"center"}}>{monthLabel}</div>
      <button onClick={()=>setMonth(m=>new Date(m.getFullYear(),m.getMonth()+1,1))} style={{...btnS(t.mut),padding:"5px 10px"}}>▶</button>
      <div style={{flex:1}}/>
      {accounts.length>0&&<button onClick={syncNow} disabled={syncing} style={btnS(t.acc)}>{syncing?"Syncing...":"⟳ Sync"}</button>}
      <button onClick={()=>setShowAccounts(s=>!s)} style={btnS(showAccounts?t.acc:t.mut)}>CalDAV</button>
      <button onClick={()=>{const now=new Date();setForm({...EMPTY_EVENT,start_local:`${dkey(now)}T09:00`});}} style={btnS(t.warm)}><IC.Plus/> Event</button>
    </div>
    <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
      <div style={{maxWidth:980}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}

        {showAccounts&&<div style={{...cardS,marginBottom:16}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
            <div style={{fontSize:12,fontWeight:800,color:t.acc}}>CalDAV accounts</div>
            <div style={{flex:1}}/>
            <button onClick={()=>setAcctForm({...EMPTY_ACCOUNT})} style={{...btnS(t.warm),padding:"4px 9px",fontSize:10}}><IC.Plus/> Add</button>
          </div>
          {accounts.map(a=><div key={a.id} style={{display:"flex",gap:8,alignItems:"center",fontSize:11,color:t.dim,padding:"5px 0",flexWrap:"wrap"}}>
            <span style={{fontWeight:700,color:t.text}}>{a.label||a.url}</span>
            <span style={{color:t.mut,fontSize:9}}>{a.username}</span>
            {a.last_error?<span style={{color:t.err,fontSize:9}} title={a.last_error}>sync error</span>
              :a.last_sync_at?<span style={{color:t.ok,fontSize:9}}>synced {String(a.last_sync_at).replace("T"," ").slice(0,16)}</span>:null}
            <div style={{flex:1}}/>
            <button onClick={()=>testAccount(a.id)} style={{...btnS(t.acc),padding:"3px 8px",fontSize:9}}>Test</button>
            <button onClick={()=>setAcctForm({id:a.id,label:a.label,url:a.url,username:a.username,password:"",calendar_url:a.calendar_url||""})} style={{...btnS(t.mut),padding:"3px 8px",fontSize:9}}><IC.Pencil/></button>
            <button onClick={async()=>{if(confirm("Remove this CalDAV account? (events stay, become local-only)")){await fetch(`${API}/api/calendar/caldav/${a.id}`,{method:"DELETE"});loadAccounts();}}} style={{...btnS(t.err),padding:"3px 8px",fontSize:9}}><IC.Trash/></button>
          </div>)}
          {acctForm&&<div style={{marginTop:10,borderTop:`1px solid ${t.brd}33`,paddingTop:10}}>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
              <input value={acctForm.label} onChange={e=>setAcctForm(f=>({...f,label:e.target.value}))} placeholder="Label (e.g. iCloud)" style={{...inputS,flex:1,minWidth:120}}/>
              <input value={acctForm.url} onChange={e=>setAcctForm(f=>({...f,url:e.target.value}))} placeholder="https://caldav.example.com/" style={{...inputS,flex:2,minWidth:200}}/>
            </div>
            <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
              <input value={acctForm.username} onChange={e=>setAcctForm(f=>({...f,username:e.target.value}))} placeholder="Username" style={{...inputS,flex:1,minWidth:120}}/>
              <input type="password" value={acctForm.password} onChange={e=>setAcctForm(f=>({...f,password:e.target.value}))} placeholder={acctForm.id?"Password (blank = keep saved)":"Password / app password"} style={{...inputS,flex:1,minWidth:140}}/>
              <input value={acctForm.calendar_url} onChange={e=>setAcctForm(f=>({...f,calendar_url:e.target.value}))} placeholder="Calendar URL (optional — first calendar if empty)" style={{...inputS,flex:2,minWidth:200}}/>
            </div>
            <div style={{display:"flex",gap:8}}>
              <button onClick={saveAccount} style={btnS(t.ok)}>{acctForm.id?"Save":"Add account"}</button>
              <button onClick={()=>setAcctForm(null)} style={btnS(t.mut)}>Cancel</button>
            </div>
          </div>}
        </div>}

        {form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.warm}55`}}>
          <div style={{fontSize:12,fontWeight:800,color:t.warm,marginBottom:10}}>{form.id?"Edit event":"New event"}</div>
          <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,marginBottom:8}} autoFocus/>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8,alignItems:"flex-end"}}>
            <div><div style={{fontSize:9,color:t.mut,marginBottom:3}}>Start (local)</div>
              <input type="datetime-local" value={form.start_local} onChange={e=>setForm(f=>({...f,start_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <div><div style={{fontSize:9,color:t.mut,marginBottom:3}}>End</div>
              <input type="datetime-local" value={form.end_local||""} onChange={e=>setForm(f=>({...f,end_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <input value={form.location} onChange={e=>setForm(f=>({...f,location:e.target.value}))} placeholder="Location" style={{...inputS,flex:1,minWidth:140}}/>
            <div><div style={{fontSize:9,color:t.mut,marginBottom:3}}>Remind (min before)</div>
              <input type="number" min={0} value={form.remind_minutes} onChange={e=>setForm(f=>({...f,remind_minutes:e.target.value}))} style={{...inputS,width:90}}/></div>
          </div>
          <textarea value={form.description} onChange={e=>setForm(f=>({...f,description:e.target.value}))} placeholder="Details (optional)" rows={2} style={{...inputS,marginBottom:10,resize:"vertical"}}/>
          <div style={{display:"flex",gap:8}}>
            <button onClick={save} style={btnS(t.ok)}>{form.id?"Save":"Create"}</button>
            <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
          </div>
        </div>}

        <div style={{...cardS,padding:14,marginBottom:16}}>
          <div style={{display:"grid",gridTemplateColumns:"repeat(7,1fr)",gap:4}}>
            {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(d=><div key={d} style={{fontSize:9,fontWeight:800,color:t.mut,textAlign:"center",padding:"2px 0",textTransform:"uppercase"}}>{d}</div>)}
            {cells.map((d,i)=>{
              if(!d)return <div key={`x${i}`}/>;
              const k=dkey(d);const dayEvents=byDay[k]||[];const isToday=k===todayKey;
              return <div key={k} onClick={()=>{setForm({...EMPTY_EVENT,start_local:`${k}T09:00`});}} style={{minHeight:64,border:`1px solid ${isToday?t.acc:t.brd}${isToday?"":"22"}`,borderRadius:6,padding:4,cursor:"pointer",background:isToday?`${t.acc}0d`:"transparent",overflow:"hidden"}}>
                <div style={{fontSize:9,fontWeight:800,color:isToday?t.acc:t.mut}}>{d.getDate()}</div>
                {dayEvents.slice(0,3).map(e=><div key={e.id} onClick={ev=>{ev.stopPropagation();setForm({id:e.id,title:e.title,start_local:e.start_local,end_local:e.end_local||"",location:e.location||"",description:e.description||"",remind_minutes:e.remind_minutes??""});}} title={e.title} style={{fontSize:8,color:t.text,background:`${t.acc}22`,border:`1px solid ${t.acc}33`,borderRadius:3,padding:"1px 3px",marginTop:2,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>
                  {(e.start_local||"").slice(11,16)} {e.title}
                </div>)}
                {dayEvents.length>3&&<div style={{fontSize:8,color:t.mut}}>+{dayEvents.length-3} more</div>}
              </div>;
            })}
          </div>
        </div>

        <div style={{...cardS}}>
          <div style={{fontSize:12,fontWeight:800,color:t.mut,marginBottom:8}}>Upcoming</div>
          {upcoming.length===0?<div style={{fontSize:11,color:t.mut}}>Nothing scheduled. Try: "dentist appointment Tuesday at 3pm" in chat.</div>
          :upcoming.map(e=><div key={e.id} style={{display:"flex",gap:10,alignItems:"center",padding:"5px 0",fontSize:12,color:t.dim,borderBottom:`1px solid ${t.brd}18`}}>
            <span style={{color:t.acc,fontWeight:700,minWidth:118,fontSize:11}}>{(e.start_local||"").replace("T"," ")}</span>
            <span style={{flex:1,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{e.title}{e.location?<span style={{color:t.mut}}> @ {e.location}</span>:null}</span>
            {e.sync_state==="synced"&&<span title="Synced via CalDAV" style={{fontSize:9,color:t.ok}}>⟳</span>}
            <button onClick={()=>remove(e.id)} style={{...btnS(t.err),padding:"2px 7px",fontSize:9}}><IC.Trash/></button>
          </div>)}
        </div>
      </div>
    </div>
  </div>;
}
