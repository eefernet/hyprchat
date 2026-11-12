import React,{useEffect,useState} from 'react';
import {userScopedUrl} from '../session.js';
import {IC} from './icons.jsx';
import {activityRows,rowsFromRecords,activityText,activityIcon} from '../daedalusActivity.js';
import {terminalJobStates,timestamp} from '../daedalusJobs.js';
import './daedalusWorkbench.css';

const STAGE_LABEL={queued:'Queued',inspecting:'Inspect',planning:'Plan',coding:'Build',baselining:'Baseline',checking:'Check',candidate_packaging:'Deliver',packaging:'Deliver'};
const clock=value=>{const ms=timestamp(value);return Number.isFinite(ms)?new Date(ms).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'';};

// The live activity feed. `limit` renders the newest rows only (the card); `paged`
// adds older rows from /events?view=activity (the workbench).
export default function DaedalusActivityFeed({job,limit=0,paged=false,onShowAll}){
  const [older,setOlder]=useState([]),[cursor,setCursor]=useState(0),[more,setMore]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{setOlder([]);setCursor(0);setMore(true);setError('');},[job.id]);
  const active=!terminalJobStates.has(job.state);
  const live=activityRows(job);
  const seen=new Set(live.map(row=>row.id));
  const rows=[...live,...rowsFromRecords(older,{active:false}).filter(row=>!seen.has(row.id))];
  const shown=limit?rows.slice(0,limit):rows;
  const load=async()=>{
    setBusy(true);setError('');
    try{
      const response=await fetch(userScopedUrl(`/api/coder/workflows/${job.id}/events?view=activity&before=${cursor}&limit=50`));
      const data=await response.json();if(!response.ok)throw new Error(data.detail||'Activity unavailable');
      setOlder(previous=>[...previous,...(data.items||[])]);setCursor(data.next_cursor||0);setMore(data.next_cursor!=null);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  };
  if(!shown.length)return <p className="dw-empty">{active?'Waiting for the first action.':'No activity was recorded for this job.'}</p>;
  const items=[];
  let group=null;
  shown.forEach((row,index)=>{
    const stage=STAGE_LABEL[row.stage]||'';
    if(!limit&&stage&&stage!==group){
      group=stage;
      items.push(<li key={`g:${row.id}`} className={`dw-group${index===0&&row.running?' is-live':''}`}><span>{stage}{index===0&&row.running?' · now':''}</span></li>);
    }
    const text=activityText(row),Icon=IC[activityIcon(row)]||IC.Activity;
    items.push(<li key={row.id} className={`dw-row is-${row.outcome}${row.verb==='check'?' is-check':''}`}>
      <span className="dw-icon" aria-hidden="true">{row.running?<span className="dw-spin"/>:<Icon/>}</span>
      <span className="dw-text">{text.label}{text.target&&<> <code>{text.target}</code></>}</span>
      <span className={`dw-note${row.outcome==='failed'?' is-failed':''}`}>{text.note}{!limit&&row.at?(text.note?' · ':'')+clock(row.at):''}</span>
    </li>);
  });
  return <div>
    <ol className="dw-feed" aria-label="Job activity">{items}</ol>
    {limit&&rows.length>limit&&onShowAll&&<button type="button" className="dw-more" onClick={onShowAll}>Show all activity</button>}
    {paged&&more&&<button type="button" className="dw-more" disabled={busy} onClick={load}>{busy?'Loading…':older.length?'Load earlier activity':'Load full history'}</button>}
    {error&&<p role="alert" className="dw-empty">{error}</p>}
  </div>;
}
