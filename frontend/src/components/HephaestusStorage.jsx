import React, {useEffect, useRef, useState} from 'react';
import {apiJson} from '../api.js';
import {formatSize,VM_AUTO_CLEANUP} from '../storageSummary.js';
import './storage.css';

const COLORS={docker:'#77b9ed',dependencies:'#bd9ce8',projects:'#79c7ac',tests:'#e4b46a',runtime:'#e59dad',system:'#9da7b6',free:'#42515d'};
const fmt=formatSize;

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
  const used=snapshot.categories||[];
  const categories=[...used,...(disk?[{id:'free',label:'Free space',bytes:disk.free}]:[])];
  const result=operation||info?.storage?.operations?.[0];
  const reclaimable=snapshot.estimated_reclaimable_bytes;
  return <section className="sto-card" aria-labelledby="vm-storage-title">
    <div className="sto-head">
      <div>
        <h3 id="vm-storage-title" className="sto-title">Sandbox VM</h3>
        <p className="sto-sub">Where your code runs (Hephaestus): project files, dependencies and test environments.</p>
      </div>
      {disk&&<div className="sto-figure">{fmt(disk.used)} used<small>{fmt(disk.free)} free of {fmt(disk.total)}</small></div>}
    </div>
    {disk?<>
      {used.length?<div className="sto-bar" role="img" aria-label={categories.map(c=>`${c.label}: ${fmt(c.bytes)}`).join(', ')}>
        {categories.map(c=><span key={c.id} title={`${c.label}: ${fmt(c.bytes)}`} style={{width:`${100*c.bytes/disk.total}%`,background:COLORS[c.id]}}/>)}
      </div>:<progress aria-label="Sandbox VM disk usage" value={disk.used} max={disk.total} style={{width:'100%',accentColor:t.acc}}/>}
      {used.length>0&&<div className="sto-legend">{used.map(c=><span key={c.id}><i style={{background:COLORS[c.id]}}/>{c.label} {fmt(c.bytes)}</span>)}</div>}
      {snapshot.status==='stale'&&<p className="sto-note" style={{color:t.warm}}>Showing an earlier scan: {snapshot.reason}</p>}
      <details className="sto-details">
        <summary>What is using the space</summary>
        <p>{snapshot.scanned_at?`Scanned ${new Date(snapshot.scanned_at*1000).toLocaleString()}.`:'The first scan is still running…'}</p>
        {categories.map(c=><details key={c.id} style={{margin:'2px 0'}}>
          <summary><span aria-hidden="true" style={{display:'inline-block',width:10,height:10,borderRadius:3,background:COLORS[c.id],margin:'0 8px 0 4px'}}/>{c.label} · <strong>{fmt(c.bytes)}</strong></summary>
          <div style={{padding:'4px 0 10px 24px',overflowWrap:'anywhere'}}>
            {c.id==='docker'?<>
              <p>{snapshot.docker?.note}</p>
              {(snapshot.docker?.images||[]).map(image=><div key={image.id} style={{marginBottom:8}}><code>{image.name}:{image.version}</code> · {image.reported_size} · {image.containers} containers<br/>{image.reason}</div>)}
              {(snapshot.docker?.summary||[]).map((row,i)=><div key={i}>{row.Type}: {row.Size}; can be freed: {row.Reclaimable}</div>)}
            </>:c.id==='free'?<p>10 GB is kept free for new work. New work waits if cleanup cannot restore it.</p>:<>
              {resources.filter(r=>r.category===c.id).map(r=><div key={r.id} style={{marginBottom:8}}><code>{r.path}</code><br/>{r.eligible?'Can be removed':'Kept'}: {r.reason}</div>)}
              {!resources.some(r=>r.category===c.id)&&<p>{c.id==='system'?'Shared data and filesystem overhead that cannot be assigned to one item.':'Kept. Nothing here can be cleaned.'}</p>}
            </>}
          </div>
        </details>)}
      </details>
    </>:<p className="sto-note">{loading?'Loading…':info?.message||'The sandbox VM did not answer.'}</p>}
    <p className="sto-note">{VM_AUTO_CLEANUP}</p>
    <div className="sto-actions">
      <button style={btnS(t.warm)} disabled={busy||running||!disk} onClick={()=>preview('safe')}>{busy?'Preparing…':'Clean up…'}</button>
      <button style={btnS(t.acc)} disabled={loading||busy} onClick={()=>onRefresh()}>Refresh</button>
      <button style={btnS(t.err)} disabled={busy||running||!disk} onClick={()=>preview('reset')}>Reset project data…</button>
      {disk&&reclaimable!=null&&<p className="sto-note">About {fmt(reclaimable)} can be freed.</p>}
    </div>
    {error&&<p role="alert" className="sto-note" data-tone="bad">{error}</p>}
    {plan&&<div style={{padding:14,border:`1px solid ${plan.mode==='reset'?t.err:t.brd}`,borderRadius:8}}>
      <h4 style={{marginTop:0}}>{plan.mode==='reset'?'Reset project data?':'Clean up the sandbox VM?'}</h4>
      <p style={prose}>{plan.mode==='reset'
        ?'Deletes inactive project workspaces, revisions and app data on the VM, including stopped pinned apps. Files saved in HyprChat and chat history stay. This cannot be undone.'
        :'Removes unused dependencies, finished test environments, and containers and images nothing uses.'}</p>
      <p style={prose}>About {fmt(plan.estimated_reclaimable_bytes)} will be freed. This review expires in five minutes.</p>
      {!!plan.blockers?.length&&<p role="status" style={{...prose,color:t.warm}}>Wait for running work to finish first: {Object.entries(plan.blockers.reduce((counts,b)=>({...counts,[b.status]:(counts[b.status]||0)+1}),{})).map(([label,count])=>`${count} ${label}`).join(', ')}.</p>}
      <details className="sto-details">
        <summary>What will be removed and kept</summary>
        <div style={{maxHeight:280,overflow:'auto',overflowWrap:'anywhere'}}>
          {plan.resources.map(r=><div key={r.id} style={{...prose,marginBottom:8}}><strong>{r.eligible?'Remove':'Keep'}</strong> <code>{r.path}</code> — {r.reason}</div>)}
          {plan.docker?.images?.map(r=><div key={r.id} style={prose}><strong>{r.protected?'Keep':'Remove'}</strong> {r.name}:{r.version} — {r.reason}</div>)}
        </div>
      </details>
      {plan.mode==='reset'&&<label style={{...prose,display:'block',margin:'14px 0'}}>Type <strong>DELETE VM PROJECT DATA</strong> to confirm:
        <input aria-label="Project data reset confirmation" value={confirmation} onChange={e=>setConfirmation(e.target.value)} autoComplete="off" spellCheck={false} style={{display:'block',width:'100%',maxWidth:430,boxSizing:'border-box',marginTop:8,padding:10,color:t.text,background:t.bgDeep,border:`1px solid ${t.brd}`,borderRadius:7}}/>
      </label>}
      <div style={{display:'flex',flexWrap:'wrap',gap:8,marginTop:12}}>
        <button style={btnS(plan.mode==='reset'?t.err:t.warm)} disabled={busy||!!plan.blockers?.length||(plan.mode==='reset'&&confirmation!=='DELETE VM PROJECT DATA')} onClick={clean}>{plan.mode==='reset'?'Reset project data':'Clean up'}</button>
        <button style={btnS(t.mut)} disabled={busy} onClick={()=>setPlan(null)}>Cancel</button>
      </div>
    </div>}
    {result&&<div role="status" className="sto-note">
      <strong>{result.status==='completed'?'Cleanup complete':result.status==='blocked'?'Cleanup stopped':'Cleanup in progress'}</strong>
      {result.status==='completed'&&<> · freed {fmt(result.recovered_bytes)}</>}
      {result.error&&<p style={{color:t.warm,margin:'4px 0 0'}}>{result.error}</p>}
      <details className="sto-details"><summary>Results</summary>
        {(result.results||[]).map((row,i)=><div key={i} style={{overflowWrap:'anywhere'}}>{row.status}: {row.path}{row.reason?` — ${row.reason}`:''}</div>)}
        {(result.warnings||[]).map((warning,i)=><p key={i}>{warning}</p>)}
      </details>
    </div>}
  </section>;
}
