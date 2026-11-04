import React, {useEffect, useRef, useState} from 'react';
import {apiJson} from '../api.js';

const COLORS={docker:'#77b9ed',dependencies:'#bd9ce8',projects:'#79c7ac',tests:'#e4b46a',runtime:'#e59dad',system:'#9da7b6',free:'#42515d'};
const fmt=(n)=>{if(n==null)return '—';const i=n>0?Math.min(4,Math.floor(Math.log(n)/Math.log(1024))):0;return `${i?(n/1024**i).toFixed(1):n} ${['B','KiB','MiB','GiB','TiB'][i]}`;};

export default function HephaestusStorage({info,loading,onRefresh,t,btnS,active=true}){
  const [plan,setPlan]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [confirmation,setConfirmation]=useState(''),[operation,setOperation]=useState(null);
  const submission=useRef(null),mounted=useRef(true);
  useEffect(()=>{mounted.current=true;return ()=>{mounted.current=false;};},[]);
  const snapshot=info?.storage?.snapshot||{};
  const disk=snapshot.disk||info?.disk;
  const running=operation&&['queued','running'].includes(operation.status);
  useEffect(()=>{
    if(!active)return;
    const update=info?.storage?.operations?.find(row=>row.id===operation?.id);
    if(update)setOperation(update);
  },[info,active,operation?.id]);
  useEffect(()=>{
    if(!active||(!running&&snapshot.scanned_at))return;
    const timer=setInterval(()=>onRefresh(),running?3000:6000);
    return ()=>clearInterval(timer);
  },[active,running,snapshot.scanned_at,onRefresh]);
  const preview=async(mode)=>{
    setBusy(true);setError('');setConfirmation('');submission.current=null;
    try{const result=await apiJson(`/api/settings/cleanup-preview?target=hephaestus&action=preview&mode=${mode}`);if(mounted.current)setPlan(result);}
    catch(e){if(mounted.current)setError(e.message);}
    finally{if(mounted.current)setBusy(false);}
  };
  const clean=async()=>{
    if(!plan)return;
    setBusy(true);setError('');
    // Keep this identity after a network interruption; retry must not start another cleanup.
    if(!submission.current)submission.current=Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
    try{
      const result=await apiJson('/api/settings/cleanup-now',{method:'POST',body:{target:'hephaestus',mode:plan.mode,preview_id:plan.id,operation_id:submission.current,confirmation}});
      if(mounted.current){setOperation(result);setPlan(null);onRefresh();}
    }catch(e){if(mounted.current)setError(e.message);}
    finally{if(mounted.current)setBusy(false);}
  };
  const prose={color:t.mut,fontSize:'var(--hc-text-secondary)',lineHeight:1.55};
  const resources=snapshot.resources||[];
  const categories=[...(snapshot.categories||[]),...(disk?[{id:'free',label:'Free space',bytes:disk.free}]:[])];
  const result=operation||info?.storage?.operations?.[0];
  return <section aria-labelledby="vm-storage-title" style={{border:'1px solid var(--hc-line-subtle)',borderRadius:'var(--hc-radius-card)',padding:'clamp(12px, 2vw, 22px)',minWidth:0}}>
    <h3 id="vm-storage-title" style={{margin:0,fontSize:'var(--hc-text-control)',color:t.acc}}>Hephaestus · Docker execution VM</h3>
    <p style={prose}>Working code, revisions, application data, and dependencies live here. Containers run your builds, tests, documents, and previews. Saved downloads and chat history stay on HyprChat.</p>
    {disk?<>
      <div style={{display:'flex',flexWrap:'wrap',gap:'8px 24px',...prose}}>
        <span>Used: <strong>{fmt(disk.used)}</strong></span><span>Free: <strong>{fmt(disk.free)}</strong></span><span>Total: <strong>{fmt(disk.total)}</strong></span>
      </div>
      {snapshot.categories?.length?<div role="img" aria-label={categories.map(c=>`${c.label}: ${fmt(c.bytes)}`).join(', ')} style={{display:'flex',height:22,borderRadius:7,overflow:'hidden',margin:'14px 0'}}>
        {categories.map(c=><span key={c.id} title={`${c.label}: ${fmt(c.bytes)}`} style={{width:`${100*c.bytes/disk.total}%`,background:COLORS[c.id]}}/>)}
      </div>:<progress aria-label="Hephaestus data disk usage" value={disk.used} max={disk.total} style={{width:'100%',margin:'14px 0',accentColor:t.acc}}/>}
      <p style={prose}>{snapshot.scanned_at?`Scanned ${new Date(snapshot.scanned_at*1000).toLocaleString()}.`:'Preparing the categorized storage scan…'} {snapshot.status==='stale'&&<span style={{color:t.warm}}>Previous scan shown: {snapshot.reason}</span>}</p>
      <div style={{display:'grid',gap:6}}>{categories.map(c=><details key={c.id} style={{...prose,margin:0}}>
        <summary style={{cursor:'pointer',padding:'7px 0'}}><span aria-hidden="true" style={{display:'inline-block',width:12,height:12,borderRadius:3,background:COLORS[c.id],margin:'0 8px'}}/>{c.label} · <strong>{fmt(c.bytes)}</strong></summary>
        <div style={{padding:'4px 0 12px 24px',overflowWrap:'anywhere'}}>
          {c.id==='docker'?<>
            <p>{snapshot.docker?.note}</p>
            {(snapshot.docker?.images||[]).map(image=><div key={image.id} style={{marginBottom:8}}><code>{image.name}:{image.version}</code> · {image.reported_size} · {image.containers} containers<br/>{image.reason}</div>)}
            {(snapshot.docker?.summary||[]).map((row,i)=><div key={i}>{row.Type}: {row.Size}; Docker estimate reclaimable: {row.Reclaimable}</div>)}
          </>:c.id==='free'?<p>A 10 GiB reserve is kept for preparation. New work waits if cleanup cannot restore it.</p>:<>
            {resources.filter(r=>r.category===c.id).map(r=><div key={r.id} style={{marginBottom:8}}><code>{r.path}</code><br/>{r.eligible?'Can be reclaimed':'Kept'}: {r.reason}</div>)}
            {!resources.some(r=>r.category===c.id)&&<p>{c.id==='system'?'Includes shared physical blocks, filesystem overhead, and data that could not be attributed safely.':'Retained data; no managed cleanup candidates in this category.'}</p>}
          </>}
        </div>
      </details>)}</div>
      <p style={prose}>Estimated reclaimable files: <strong>{fmt(snapshot.estimated_reclaimable_bytes)}</strong>. Shared blocks and Docker layers can reduce the actual recovery.</p>
    </>:<p style={prose}>{loading?'Loading VM storage…':info?.message||'VM storage status is unavailable.'}</p>}
    <p style={prose}>Automatic cleanup checks every 10 minutes. Completed tests are eligible after 24 hours and unused caches after 7 days. Active work, pinned apps, retained revisions, and the current release plus a verified rollback are protected.</p>
    <div style={{display:'flex',flexWrap:'wrap',gap:8}}>
      <button style={btnS(t.acc)} disabled={loading||busy} onClick={()=>onRefresh()}>Refresh VM status</button>
      <button style={btnS(t.acc)} disabled={busy||running||!disk} onClick={()=>preview('safe')}>{busy?'Preparing…':'Preview cleanup'}</button>
      <button style={btnS(t.warm)} disabled={busy||running||!disk} onClick={()=>preview('safe')}>Free maximum safe space</button>
      <button style={btnS(t.err)} disabled={busy||running||!disk} onClick={()=>preview('reset')}>Reset VM project data…</button>
    </div>
    {error&&<p role="alert" style={{...prose,color:t.err}}>{error}</p>}
    {plan&&<div style={{marginTop:18,padding:14,border:`1px solid ${plan.mode==='reset'?t.err:t.brd}`,borderRadius:8}}>
      <h4 style={{marginTop:0}}>{plan.mode==='reset'?'Review project-data reset':'Review safe cleanup'}</h4>
      <p style={prose}>{plan.mode==='reset'?'This also deletes inactive VM workspaces, revisions, and application data, including stopped pinned apps. Saved HyprChat artifacts and chat history remain. Deleted workspaces cannot resume without restoring their source.':'Removes unused reconstructible dependencies, archived test environments, and unreferenced containers, images, and retired releases.'}</p>
      <p style={prose}>Estimated files: {fmt(plan.estimated_reclaimable_bytes)}. Review valid for five minutes.</p>
      {!!plan.blockers?.length&&<p role="status" style={{...prose,color:t.warm}}>Wait for active jobs, documents, and previews to stop before cleanup: {Object.entries(plan.blockers.reduce((counts,b)=>({...counts,[b.status]:(counts[b.status]||0)+1}),{})).map(([label,count])=>`${count} ${label}`).join(', ')}.</p>}
      <div style={{maxHeight:280,overflow:'auto',overflowWrap:'anywhere'}}>
        {plan.resources.map(r=><div key={r.id} style={{...prose,marginBottom:8}}><strong>{r.eligible?'Remove':'Keep'}</strong> <code>{r.path}</code> — {r.reason}</div>)}
        {plan.docker?.images?.map(r=><div key={r.id} style={prose}><strong>{r.protected?'Keep':'Remove'}</strong> {r.name}:{r.version} — {r.reason}</div>)}
      </div>
      {plan.mode==='reset'&&<label style={{...prose,display:'block',margin:'14px 0'}}>Type <strong>DELETE VM PROJECT DATA</strong> to confirm:
        <input aria-label="Project data reset confirmation" value={confirmation} onChange={e=>setConfirmation(e.target.value)} autoComplete="off" spellCheck={false} style={{display:'block',width:'100%',maxWidth:430,boxSizing:'border-box',marginTop:8,padding:10,color:t.text,background:t.bgDeep,border:`1px solid ${t.brd}`,borderRadius:7}}/>
      </label>}
      <div style={{display:'flex',flexWrap:'wrap',gap:8,marginTop:12}}>
        <button style={btnS(plan.mode==='reset'?t.err:t.warm)} disabled={busy||!!plan.blockers?.length||(plan.mode==='reset'&&confirmation!=='DELETE VM PROJECT DATA')} onClick={clean}>{plan.mode==='reset'?'Confirm project-data reset':'Run safe cleanup'}</button>
        <button style={btnS(t.mut)} disabled={busy} onClick={()=>setPlan(null)}>Cancel</button>
      </div>
    </div>}
    {result&&<div role="status" style={{...prose,marginTop:16}}>
      <strong>{result.status==='completed'?'Cleanup complete':result.status==='blocked'?'Cleanup stopped': 'Cleanup in progress'}</strong>
      {result.status==='completed'&&<> · Recovered {fmt(result.recovered_bytes)}</>}
      {result.error&&<p style={{color:t.warm}}>{result.error}</p>}
      <details><summary style={{cursor:'pointer'}}>Cleanup results</summary>
        {(result.results||[]).map((row,i)=><div key={i} style={{overflowWrap:'anywhere'}}>{row.status}: {row.path}{row.reason?` — ${row.reason}`:''}</div>)}
        {(result.warnings||[]).map((warning,i)=><p key={i}>{warning}</p>)}
      </details>
    </div>}
  </section>;
}
