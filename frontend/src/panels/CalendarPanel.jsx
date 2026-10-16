import React,{useState,useEffect,useCallback,useRef} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtLocalMinute, fmtUtcToLocal } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { colorFor } from '../components/identityColor.js';
import { SkeletonCard } from '../components/Skeleton.jsx';
import useIsMobile from '../useIsMobile.js';

const EMPTY_EVENT={title:"",start_local:"",end_local:"",location:"",description:"",remind_minutes:""};
const EMPTY_ACCOUNT={label:"",url:"",username:"",password:"",calendar_url:""};
const pad=n=>String(n).padStart(2,"0");
const dkey=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const mondayOf=d=>{const x=new Date(d.getFullYear(),d.getMonth(),d.getDate());x.setDate(x.getDate()-((x.getDay()+6)%7));return x;};
const toMin=s=>{const hm=(s||"").slice(11,16);if(!hm)return 0;const[h,m]=hm.split(":").map(Number);return(h||0)*60+(m||0);};
const HOUR_H=44;

// Week/day hour grid: absolute-positioned event blocks over 24 hour rows, naive
// lane layout for overlaps (max 3 lanes + "+N"), all-day strip, "now" line.
function TimeGrid({t,days,byDay,todayKey,nowMin,evColor,onEventClick,onSlotClick,onDayClick}){
  const scrollRef=useRef(null);
  const daysKey=days.map(dkey).join(",");
  useEffect(()=>{if(scrollRef.current)scrollRef.current.scrollTop=7*HOUR_H;},[daysKey]);
  const gutterW=46;
  const allDayRows=days.map(d=>({d,evs:(byDay[dkey(d)]||[]).filter(e=>e.all_day||!(e.start_local||"").includes("T"))}));
  return <div style={{display:"flex",flexDirection:"column",maxHeight:560}}>
    <div style={{display:"flex",borderBottom:`1px solid ${t.brd}22`}}>
      <div style={{width:gutterW,flexShrink:0}}/>
      {days.map(d=>{const k=dkey(d);const isToday=k===todayKey;
        return <div key={k} onClick={()=>onDayClick&&onDayClick(d)} style={{flex:1,minWidth:0,textAlign:"center",padding:"6px 0",cursor:onDayClick?"pointer":"default",color:isToday?t.acc:t.mut,fontWeight:800,fontSize:"var(--hc-text-secondary)",textTransform:"none"}} title={onDayClick?"Open day view":undefined}>
          {d.toLocaleDateString(undefined,{weekday:"short"})} <span style={{fontSize:12,color:isToday?t.acc:t.text}}>{d.getDate()}</span>
        </div>;})}
    </div>
    {allDayRows.some(r=>r.evs.length>0)&&<div style={{display:"flex",borderBottom:`1px solid ${t.brd}22`}}>
      <div style={{width:gutterW,flexShrink:0,fontSize:"var(--hc-text-secondary)",color:t.mut,display:"flex",alignItems:"center",justifyContent:"flex-end",paddingRight:6}}>all-day</div>
      {allDayRows.map(({d,evs})=><div key={dkey(d)} style={{flex:1,minWidth:0,padding:2,display:"flex",flexDirection:"column",gap:2}}>
        {evs.slice(0,2).map(e=>{const c=evColor(e);return <div key={e.occurrence_id||e.id} onClick={ev=>{ev.stopPropagation();onEventClick(e);}} title={e.title} style={{fontSize:"var(--hc-text-secondary)",color:t.text,background:`${c}22`,borderLeft:`3px solid ${c}`,borderRadius:4,padding:"2px 4px",whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis",cursor:"pointer"}}>{e.title}</div>;})}
        {evs.length>2&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>+{evs.length-2}</div>}
      </div>)}
    </div>}
    <div ref={scrollRef} style={{overflowY:"auto",flex:1,minHeight:0}}>
      <div style={{display:"flex",position:"relative"}}>
        <div style={{width:gutterW,flexShrink:0}}>
          {Array.from({length:24},(_,h)=><div key={h} style={{height:HOUR_H,fontSize:"var(--hc-text-secondary)",color:t.mut,textAlign:"right",paddingRight:6,boxSizing:"border-box",borderTop:`1px solid ${t.brd}14`}}>{pad(h)}:00</div>)}
        </div>
        {days.map(d=>{
          const k=dkey(d);const isToday=k===todayKey;
          const timed=(byDay[k]||[]).filter(e=>!e.all_day&&(e.start_local||"").includes("T"));
          const sorted=[...timed].sort((a,b)=>(a.start_local||"").localeCompare(b.start_local||""));
          const laneEnds=[];const placed=[];let hidden=0;
          for(const e of sorted){
            const s=toMin(e.start_local);
            const sameDayEnd=(e.end_local||"").slice(0,10)===k?toMin(e.end_local):0;
            const en=Math.max(sameDayEnd,s+30);
            let lane=laneEnds.findIndex(x=>x<=s);
            if(lane===-1){
              if(laneEnds.length>=3){hidden++;continue;}
              lane=laneEnds.length;laneEnds.push(en);
            }else laneEnds[lane]=en;
            placed.push({e,lane,s,en});
          }
          const laneN=Math.max(laneEnds.length,1);
          return <div key={k} onClick={ev=>{const rect=ev.currentTarget.getBoundingClientRect();const hour=Math.max(0,Math.min(23,Math.floor((ev.clientY-rect.top)/HOUR_H)));onSlotClick(k,hour);}} style={{flex:1,minWidth:0,position:"relative",borderLeft:`1px solid ${t.brd}14`,background:isToday?`${t.acc}06`:"transparent",height:24*HOUR_H,cursor:"pointer"}} title="Click a slot to create an event">
            {Array.from({length:24},(_,h)=><div key={h} style={{position:"absolute",top:h*HOUR_H,left:0,right:0,borderTop:`1px solid ${t.brd}14`,pointerEvents:"none"}}/>)}
            {placed.map(({e,lane,s,en})=>{const c=evColor(e);const w=100/laneN;
              return <div key={e.occurrence_id||e.id} onClick={ev=>{ev.stopPropagation();onEventClick(e);}} title={`${(e.start_local||"").slice(11,16)} ${e.title}`} style={{position:"absolute",top:s/60*HOUR_H,height:Math.max((en-s)/60*HOUR_H,24)-2,left:`calc(${lane*w}% + 2px)`,width:`calc(${w}% - 4px)`,background:`${c}26`,borderLeft:`3px solid ${c}`,borderRadius:6,padding:"2px 5px",fontSize:"var(--hc-text-secondary)",color:t.text,overflow:"hidden",cursor:"pointer",boxSizing:"border-box",zIndex:1}}>
                <div style={{fontWeight:700,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{e.title}</div>
                <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{(e.start_local||"").slice(11,16)}{e.end_local?`–${(e.end_local||"").slice(11,16)}`:""}</div>
              </div>;})}
            {hidden>0&&<div style={{position:"absolute",top:2,right:2,fontSize:"var(--hc-text-secondary)",color:t.mut,background:`${t.surface}F5`,border:`1px solid ${t.brd}33`,borderRadius:4,padding:"1px 4px",zIndex:2}} title={`${hidden} more overlapping event${hidden>1?"s":""}`}>+{hidden}</div>}
            {isToday&&nowMin!=null&&<div style={{position:"absolute",top:nowMin/60*HOUR_H,left:0,right:0,borderTop:`1px solid ${t.err}`,zIndex:2,pointerEvents:"none"}}>
              <div style={{position:"absolute",left:-3,top:-3.5,width:6,height:6,borderRadius:"50%",background:t.err}}/>
            </div>}
          </div>;})}
      </div>
    </div>
  </div>;
}

export default function CalendarPanel({t,btnS,cardS,inputS,confirmAction,notify}){
  const isMobile=useIsMobile();
  const [events,setEvents]=useState([]);
  const [loaded,setLoaded]=useState(false);
  // Phones default to day view — the 7-column month grid gives ~44px cells at
  // 360px wide. Switching back is still allowed; only week view is blocked.
  const [view,setView]=useState(()=>isMobile?"day":"month");
  const [nowTick,setNowTick]=useState(()=>Date.now());
  const [anchor,setAnchor]=useState(()=>new Date());
  const [form,setForm]=useState(null);
  const [accounts,setAccounts]=useState([]);
  const [acctForm,setAcctForm]=useState(null);
  const [showAccounts,setShowAccounts]=useState(false);
  const [syncing,setSyncing]=useState(false);
  const [tz,setTz]=useState("");   // assistant-profile timezone (events are bucketed in it)
  const [err,setErr]=useState("");
  const [proposals,setProposals]=useState([]);
  const [conflicts,setConflicts]=useState([]);

  const load=useCallback(async()=>{
    let start,end;
    if(view==="month"){
      start=new Date(anchor.getFullYear(),anchor.getMonth(),1);start.setDate(start.getDate()-7);
      end=new Date(anchor.getFullYear(),anchor.getMonth()+1,7);
    }else if(view==="week"){
      start=mondayOf(anchor);start.setDate(start.getDate()-1);
      end=mondayOf(anchor);end.setDate(end.getDate()+8);
    }else{
      start=new Date(anchor);start.setDate(start.getDate()-1);
      end=new Date(anchor);end.setDate(end.getDate()+1);
    }
    try{
      setEvents(await apiJson(`/api/calendar/events?start_local=${dkey(start)}T00:00&end_local=${dkey(end)}T23:59`));
      setProposals(await apiJson('/api/calendar/proposals'));
      setConflicts(await apiJson('/api/calendar/conflicts'));
    }catch(e){setErr(String(e.message||e));}
    finally{setLoaded(true);}
  },[anchor,view]);
  const loadAccounts=useCallback(async()=>{
    try{setAccounts(await apiJson("/api/calendar/caldav"));}catch(e){setErr(String(e.message||e));}
  },[]);
  useEffect(()=>{load();loadAccounts();},[load,loadAccounts]);
  useEffect(()=>{apiJson("/api/assistant").then(d=>setTz(d?.profile?.timezone||"")).catch(()=>{});},[]);
  // Keep the "now" line moving (it froze at mount time before this tick).
  useEffect(()=>{const id=setInterval(()=>setNowTick(Date.now()),60000);return()=>clearInterval(id);},[]);
  useEffect(()=>{if(isMobile&&view==="week")setView("day");},[isMobile,view]);
  // "Today" in the ASSISTANT timezone — events arrive bucketed by assistant-tz
  // start_local, so a browser in another tz would highlight the wrong day.
  const tzToday=()=>{try{return new Intl.DateTimeFormat("en-CA",{timeZone:tz||undefined}).format(new Date());}catch{return dkey(new Date());}};

  const save=async()=>{
    if(!form?.title?.trim()||!form?.start_local)return setErr("Title and start time are required");
    setErr("");
    // Cleared reminder on an existing event → explicit null (the backend
    // treats present-with-null as "clear"; omitting the key means "unchanged").
    const body={title:form.title,start_local:form.start_local,end_local:form.end_local||"",
      ...(form.edit_scope==='occurrence'?{recurrence_id:form.recurrence_id}:{rrule:form.rrule||''}),
      location:form.location,description:form.description,
      ...(form.edit_scope==='occurrence'?{}:form.remind_minutes!==""?{remind_minutes:Number(form.remind_minutes)}
         :form.id?{remind_minutes:null}:{})};
    try{
      const r=await fetch(`${API}/api/calendar/events${form.id?`/${form.id}`:""}`,{
        method:form.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      setForm(null);load();
    }catch(e){setErr(String(e.message||e));}
  };
  const remove=async id=>{
    if(!await confirmAction({title:"Delete event",body:"Delete this event?",confirmLabel:"Delete",tone:"danger"}))return;
    try{await apiJson(`/api/calendar/events/${id}`,{method:"DELETE"});}
    catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Delete failed",detail:String(e.message||e)});}
    finally{load();}
  };
  const openEdit=e=>setForm({id:e.series_id||e.id,rrule:e.rrule||'',series_start_local:e.series_start_local,series_end_local:e.series_end_local,recurrence_id:e.recurrence_id,edit_scope:e.rrule?'occurrence':'series',title:e.title,start_local:e.start_local,end_local:e.end_local||"",location:e.location||"",description:e.description||"",remind_minutes:e.remind_minutes??""});
  const saveAccount=async()=>{
    if(!acctForm?.url)return;
    setErr("");
    try{
      await apiJson(`/api/calendar/caldav${acctForm.id?`/${acctForm.id}`:""}`,{
        method:acctForm.id?"PATCH":"POST",body:acctForm});
      setAcctForm(null);loadAccounts();
    }catch(e){setErr(String(e.message||e));}
  };
  const testAccount=async id=>{
    setErr("");
    try{
      const d=await apiJson(`/api/calendar/caldav/${id}/test`,{method:"POST"});
      notify({type:"success",text:"Connected",detail:`Calendars: ${(d.calendars||[]).map(c=>c.name).join(", ")||"none found"}`});
    }catch(e){setErr(String(e.message||e));}
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
  const todayKey=tzToday();
  // Per-calendar identity color: CalDAV events hash their account id; local events keep the accent.
  const evColor=e=>e.caldav_account_id?colorFor(e.caldav_account_id,t):t.acc;

  const firstDow=(new Date(anchor.getFullYear(),anchor.getMonth(),1).getDay()+6)%7; // Monday-first
  const daysInMonth=new Date(anchor.getFullYear(),anchor.getMonth()+1,0).getDate();
  const cells=[];
  for(let i=0;i<firstDow;i++)cells.push(null);
  for(let d=1;d<=daysInMonth;d++)cells.push(new Date(anchor.getFullYear(),anchor.getMonth(),d));
  const weekDays=Array.from({length:7},(_,i)=>{const d=mondayOf(anchor);d.setDate(d.getDate()+i);return d;});
  const gridDays=view==="week"?weekDays:[new Date(anchor.getFullYear(),anchor.getMonth(),anchor.getDate())];
  const upcoming=[...events].filter(e=>(e.start_local||"")>=todayKey).slice(0,15);
  const label=view==="month"?anchor.toLocaleDateString(undefined,{month:"long",year:"numeric"})
    :view==="week"?`${weekDays[0].toLocaleDateString(undefined,{month:"short",day:"numeric"})} – ${weekDays[6].toLocaleDateString(undefined,{month:"short",day:"numeric"})}, ${weekDays[6].getFullYear()}`
    :anchor.toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric",year:"numeric"});
  const step=dir=>setAnchor(a=>{
    if(view==="month")return new Date(a.getFullYear(),a.getMonth()+dir,1);
    const d=new Date(a);d.setDate(d.getDate()+dir*(view==="week"?7:1));return d;
  });
  const goToday=()=>{const[y,m,d]=todayKey.split("-").map(Number);setAnchor(new Date(y,m-1,d));};
  // "Now" line minute-of-day in the assistant timezone.
  const nowMin=(()=>{try{const p=new Intl.DateTimeFormat("en-GB",{timeZone:tz||undefined,hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(nowTick));const[h,m]=p.split(":").map(Number);return h*60+m;}catch{const n=new Date(nowTick);return n.getHours()*60+n.getMinutes();}})();
  const openDay=d=>{setAnchor(new Date(d.getFullYear(),d.getMonth(),d.getDate()));setView("day");};
  const slotCreate=(k,hour)=>setForm({...EMPTY_EVENT,start_local:`${k}T${pad(hour)}:00`});

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.f1||t.acc} icon={<IC.Clock/>} title="Calendar"
      subtitle="Times shown in your assistant timezone. CalDAV keeps your phone in sync."
      nav={<>
        <button aria-label="Previous" onClick={()=>step(-1)} style={{...btnS(t.mut),padding:"5px 10px"}}><IC.ChevLeft/></button>
        <div style={{fontSize:12,fontWeight:800,color:t.text,minWidth:150,textAlign:"center"}}>{label}</div>
        <button aria-label="Next" onClick={()=>step(1)} style={{...btnS(t.mut),padding:"5px 10px"}}><IC.ChevRight/></button>
        <button onClick={goToday} style={{...btnS(t.mut),padding:"5px 10px"}}>Today</button>
        <div style={{display:"flex",gap:2,marginLeft:6}}>
          {(isMobile?["month","day"]:["month","week","day"]).map(v=><button key={v} onClick={()=>setView(v)} style={{...btnS(view===v?(t.f1||t.acc):t.mut),padding:"5px 10px",textTransform:"capitalize"}}>{v}</button>)}
        </div>
      </>}>
      {accounts.length>0&&<button onClick={syncNow} disabled={syncing} style={btnS(t.acc)}>{syncing?"Syncing...":<><IC.Refresh/> Sync</>}</button>}
      <button onClick={()=>setShowAccounts(s=>!s)} style={btnS(showAccounts?t.acc:t.mut)}>CalDAV</button>
      <button onClick={()=>setForm({...EMPTY_EVENT,start_local:`${tzToday()}T09:00`})} style={btnS(t.warm)}><IC.Plus/> Event</button>
    </PanelHeader>
    {conflicts.length>0&&<div style={{padding:12,borderBottom:`1px solid ${t.brd}`}}><strong>Calendar conflicts</strong>{conflicts.map(c=><div key={c.id} style={{marginTop:8}}>
      <div>Both versions are saved. Choose which to use:</div>{['local','remote'].map(choice=><button key={choice} style={{...btnS(t.acc),margin:4}} onClick={async()=>{try{await apiJson(`/api/calendar/conflicts/${c.id}/${choice}`,{method:'POST'});await load();}catch(e){setErr(e.message);}}}>{choice==='local'?'HyprChat':'Calendar server'}: {c[choice]?.title} · {c[choice]?.start_at}</button>)}
    </div>)}</div>}
    {proposals.length>0&&<div style={{padding:12,maxHeight:220,overflowY:'auto',borderBottom:`1px solid ${t.brd}`}}>
      <strong>Appointments proposed from email</strong>
      <p style={{fontSize:"var(--hc-text-secondary)",color:t.dim}}>Confirm an appointment to add it to your calendar and allow synchronization.</p>
      {proposals.map(proposal=><div key={proposal.id} style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center',marginTop:8}}>
        <span style={{flex:1,fontSize:12}}>{proposal.title} · {new Date(proposal.start_at+'Z').toLocaleString(undefined,{timeZone:tz||'UTC'})}</span>
        {['confirmed','rejected'].map(decision=><button key={decision} style={btnS(decision==='confirmed'?t.ok:t.mut)} onClick={async()=>{try{await apiJson(`/api/calendar/proposals/${proposal.id}/${decision}`,{method:'POST'});await load();}catch(e){if(e.name!=='AbortError')setErr(e.message);}}}>{decision==='confirmed'?'Confirm':'Reject'}</button>)}
      </div>)}
    </div>}

    <div style={{overflowY:"auto",padding:isMobile?"14px 12px":"20px 28px",flex:1}}>
      <div style={{maxWidth:980}}>
        {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}

        {showAccounts&&<div style={{...cardS,marginBottom:16}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
            <div style={{fontSize:12,fontWeight:800,color:t.acc}}>CalDAV accounts</div>
            <div style={{flex:1}}/>
            <button onClick={()=>setAcctForm({...EMPTY_ACCOUNT})} style={{...btnS(t.warm),padding:"4px 9px",fontSize:"var(--hc-text-control)"}}><IC.Plus/> Add</button>
          </div>
          {accounts.map(a=><div key={a.id} style={{display:"flex",gap:8,alignItems:"center",fontSize:"var(--hc-text-secondary)",color:t.dim,padding:"5px 0",flexWrap:"wrap"}}>
            <span style={{fontWeight:700,color:t.text}}>{a.label||a.url}</span>
            <span style={{color:t.mut,fontSize:"var(--hc-text-secondary)"}}>{a.username}</span>
            {a.last_error?<span style={{color:t.err,fontSize:"var(--hc-text-secondary)"}} title={a.last_error}>sync error</span>
              :a.last_sync_at?<span style={{color:t.ok,fontSize:"var(--hc-text-secondary)"}}>synced {fmtUtcToLocal(a.last_sync_at)}</span>:null}
            <div style={{flex:1}}/>
            <button onClick={()=>testAccount(a.id)} style={{...btnS(t.acc),padding:"3px 8px",fontSize:"var(--hc-text-control)"}}>Test</button>
            <button aria-label="Edit" onClick={()=>setAcctForm({id:a.id,label:a.label,url:a.url,username:a.username,password:"",calendar_url:a.calendar_url||""})} style={{...btnS(t.mut),padding:"3px 8px",fontSize:"var(--hc-text-control)"}}><IC.Pencil/></button>
            <button aria-label="Delete" onClick={async()=>{if(!await confirmAction({title:"Remove CalDAV account",body:"Remove this CalDAV account? Synced events stay and become local-only.",confirmLabel:"Remove",tone:"danger"}))return;try{await apiJson(`/api/calendar/caldav/${a.id}`,{method:"DELETE"});}catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Remove failed",detail:String(e.message||e)});}finally{loadAccounts();}}} style={{...btnS(t.err),padding:"3px 8px",fontSize:"var(--hc-text-control)"}}><IC.Trash/></button>
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
          {form.recurrence_id&&form.rrule&&<label style={{display:'block',marginBottom:8}}>Apply changes to <select value={form.edit_scope} onChange={e=>setForm(f=>({...f,edit_scope:e.target.value,...(e.target.value==='series'?{start_local:f.series_start_local||f.start_local,end_local:f.series_end_local||f.end_local}:{})}))} style={inputS}><option value="occurrence">This occurrence</option><option value="series">Entire series</option></select></label>}
          {form.edit_scope!=='occurrence'&&<input aria-label="Recurrence rule" value={form.rrule||''} onChange={e=>setForm(f=>({...f,rrule:e.target.value}))} placeholder="Recurrence (optional): FREQ=WEEKLY;BYDAY=MO" style={{...inputS,marginBottom:8}}/>}
          <input value={form.title} onChange={e=>setForm(f=>({...f,title:e.target.value}))} placeholder="Title" style={{...inputS,marginBottom:8}} autoFocus/>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8,alignItems:"flex-end"}}>
            <div><div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:3}}>Start (local)</div>
              <input type="datetime-local" value={form.start_local} onChange={e=>setForm(f=>({...f,start_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <div><div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:3}}>End</div>
              <input type="datetime-local" value={form.end_local||""} onChange={e=>setForm(f=>({...f,end_local:e.target.value}))} style={{...inputS,width:"auto"}}/></div>
            <input value={form.location} onChange={e=>setForm(f=>({...f,location:e.target.value}))} placeholder="Location" style={{...inputS,flex:1,minWidth:140}}/>
            <div><div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:3}}>Remind (min before)</div>
              <input type="number" min={0} disabled={form.edit_scope==='occurrence'} value={form.remind_minutes} onChange={e=>setForm(f=>({...f,remind_minutes:e.target.value}))} style={{...inputS,width:90}}/>{form.edit_scope==='occurrence'&&<small>Reminder applies to the series.</small>}</div>
          </div>
          <textarea value={form.description} onChange={e=>setForm(f=>({...f,description:e.target.value}))} placeholder="Details (optional)" rows={2} style={{...inputS,marginBottom:10,resize:"vertical"}}/>
          <div style={{display:"flex",gap:8}}>
            <button onClick={save} style={btnS(t.ok)}>{form.id?"Save":"Create"}</button>
            <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
          </div>
        </div>}

        {accounts.length>0&&<div style={{display:"flex",gap:12,flexWrap:"wrap",alignItems:"center",marginBottom:10,fontSize:"var(--hc-text-secondary)",color:t.dim}}>
          <span style={{display:"inline-flex",alignItems:"center",gap:5}}><span style={{width:9,height:9,borderRadius:3,background:t.acc}}/>Local</span>
          {accounts.map(a=>{const c=colorFor(a.id,t);return <span key={a.id} style={{display:"inline-flex",alignItems:"center",gap:5}}><span style={{width:9,height:9,borderRadius:3,background:c}}/>{a.label||a.url}</span>;})}
        </div>}

        {!loaded?<div style={{marginBottom:16}}><SkeletonCard t={t} h={300} lines={4}/></div>
        :view==="month"?<div style={{...cardS,padding:14,marginBottom:16}}>
          <div style={{display:"grid",gridTemplateColumns:"repeat(7,1fr)",gap:4}}>
            {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(d=><div key={d} style={{fontSize:"var(--hc-text-secondary)",fontWeight:800,color:t.mut,textAlign:"center",padding:"2px 0",textTransform:"none"}}>{d}</div>)}
            {cells.map((d,i)=>{
              if(!d)return <div key={`x${i}`}/>;
              const k=dkey(d);const dayEvents=byDay[k]||[];const isToday=k===todayKey;
              return <div key={k} onClick={()=>{setForm({...EMPTY_EVENT,start_local:`${k}T09:00`});}} style={{minHeight:64,border:`1px solid ${isToday?t.acc:t.brd}${isToday?"":"22"}`,borderRadius:6,padding:4,cursor:"pointer",background:isToday?`${t.acc}0d`:"transparent",overflow:"hidden"}}>
                <div onClick={ev=>{ev.stopPropagation();openDay(d);}} title="Open day view" style={{fontSize:"var(--hc-text-secondary)",fontWeight:800,color:isToday?t.acc:t.mut,display:"inline-block"}}>{d.getDate()}</div>
                {dayEvents.slice(0,3).map(e=>{const c=evColor(e);return <div key={e.occurrence_id||e.id} onClick={ev=>{ev.stopPropagation();openEdit(e);}} title={e.title} style={{fontSize:"var(--hc-text-secondary)",color:t.text,background:`${c}22`,border:`1px solid ${c}44`,borderRadius:3,padding:"2px 4px",marginTop:2,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>
                  {(e.start_local||"").slice(11,16)} {e.title}
                </div>;})}
                {dayEvents.length>3&&<div onClick={ev=>{ev.stopPropagation();openDay(d);}} style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:2}}>+{dayEvents.length-3} more</div>}
              </div>;
            })}
          </div>
        </div>
        :<div style={{...cardS,padding:14,marginBottom:16}}>
          <TimeGrid t={t} days={gridDays} byDay={byDay} todayKey={todayKey} nowMin={nowMin} evColor={evColor}
            onEventClick={openEdit} onSlotClick={slotCreate} onDayClick={view==="week"?openDay:null}/>
        </div>}

        <div style={{...cardS}}>
          <div style={{fontSize:12,fontWeight:800,color:t.mut,marginBottom:8}}>Upcoming</div>
          {upcoming.length===0?<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>Nothing scheduled. Try: "dentist appointment Tuesday at 3pm" in chat.</div>
          :upcoming.map(e=><div key={e.occurrence_id||e.id} style={{display:"flex",gap:10,alignItems:"center",padding:"5px 0",fontSize:12,color:t.dim,borderBottom:`1px solid ${t.brd}18`}}>
            <span style={{width:9,height:9,borderRadius:3,background:evColor(e),flexShrink:0}}/>
            <span style={{color:t.acc,fontWeight:700,minWidth:118,fontSize:"var(--hc-text-secondary)"}}>{fmtLocalMinute(e.start_local)}</span>
            <span style={{flex:1,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{e.title}{e.location?<span style={{color:t.mut}}> @ {e.location}</span>:null}</span>
            {e.sync_state==="synced"&&<span title="Synced via CalDAV" style={{fontSize:"var(--hc-text-secondary)",color:t.ok,display:"inline-flex"}}><IC.Refresh/></span>}
            <button aria-label="Delete" onClick={()=>remove(e.id)} style={{...btnS(t.err),padding:"2px 7px",fontSize:"var(--hc-text-control)"}}><IC.Trash/></button>
          </div>)}
        </div>
      </div>
    </div>
  </div>;
}
