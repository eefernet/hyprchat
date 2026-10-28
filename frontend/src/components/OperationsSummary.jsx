import React,{useEffect,useState} from 'react';
import {apiJson} from '../api.js';
import {hcCaptureUserContext} from '../session.js';

export default function OperationsSummary({t}){
  const [data,setData]=useState(null),[error,setError]=useState('');
  const admin=hcCaptureUserContext().userId==='default';
  useEffect(()=>{if(!admin)return;const c=new AbortController();const load=()=>apiJson('/api/operations',{signal:c.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);});load();const timer=setInterval(load,30000);return()=>{clearInterval(timer);c.abort();};},[admin]);
  if(!admin)return null;
  return <details style={{margin:'12px 0',padding:12,border:`1px solid ${t.brd}`,borderRadius:"var(--hc-radius)"}}><summary>Operations · {data?(data.ready?'ready':'needs attention'):'loading'}</summary>{error&&<p role="alert">{error}</p>}{data&&<>
    <p>Backend reachable: {data.reachable?'yes':'no'} · Database and storage ready: {data.ready?'yes':'no'} · Free storage: {(data.storage.free_bytes/1024**3).toFixed(1)} GB ({data.storage.pressure})</p>
    {data.errors.map(e=><p key={e} style={{color:t.err}}>{e}</p>)}
    {data.queues.map(q=><p key={q.kind+q.status}>{q.kind}: {q.count} {q.status}; oldest {q.oldest_age_seconds??'unknown'} seconds</p>)}
    <strong>Most recent successful work</strong>{data.recent_success.map(s=><p key={s.kind}>{s.kind}: {s.last_success||'unavailable'}</p>)}
    <strong>Notification delivery</strong>{data.notification_delivery.map(d=><p key={d.status}>{d.status}: {d.count}; {d.attempts||0} delivery attempts</p>)}
    <details><summary>Installed dependencies</summary><pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(data.dependencies,null,2)}</pre></details>
  </>}</details>;
}
