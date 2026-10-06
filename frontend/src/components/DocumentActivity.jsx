import React,{useEffect,useMemo,useState} from 'react';
import {API,userScopedUrl} from '../session.js';
import {isDocumentEvent} from '../documentFiles.js';

const labels={read:'Document read',create:'Document created',edit:'Revision saved',convert:'Export saved',preview:'Preview ready'};
export default function DocumentActivity({runIds,events=[],live=false,t,onPreview}){
  const [runs,setRuns]=useState({}),[error,setError]=useState('');
  const key=runIds.join('|');
  useEffect(()=>{
    let closed=false,timer;
    const load=async()=>{
      const rows=await Promise.all(runIds.map(async id=>{
        try{const r=await fetch(`${API}/api/runs/${encodeURIComponent(id)}`);if(!r.ok)throw new Error();return await r.json();}catch{return null;}
      }));
      if(closed)return;
      setRuns(old=>({...old,...Object.fromEntries(rows.filter(Boolean).map(r=>[r.id,r]))}));
      setError(rows.some(r=>!r)?'Some document status is unavailable. Reopen this chat to retry.':'');
      if(rows.some(r=>!r)||rows.some(r=>r&&['queued','pending','running','cancelling'].includes(r.status))||live)timer=setTimeout(load,2000);
    };
    if(runIds.length)load();
    return()=>{closed=true;clearTimeout(timer);};
  },[key,live]);
  const relevant=events.filter(isDocumentEvent);
  const files=useMemo(()=>{
    const map=new Map();
    for(const id of runIds)for(const a of runs[id]?.result_envelope?.artifacts||[])map.set(a.artifact_id,a);
    for(const e of events)if(e.type==='file_ready'&&isDocumentEvent(e)&&e.data?.artifact_id)map.set(e.data.artifact_id,e.data);
    return [...map.values()];
  },[key,runs,events]);
  const validations=[...new Set(relevant.filter(e=>e.type==='tool_error'&&!e.data?.run_id).map(e=>e.data?.status).filter(Boolean))];
  if(!runIds.length&&!validations.length&&!files.length)return null;
  return <div aria-label="Document activity" style={{display:'grid',gap:7,marginTop:8,fontSize:12}}>
    {runIds.map(id=>{
      const row=runs[id],env=row?.result_envelope||{};
      const ev=[...relevant].reverse().find(e=>e.data?.run_id===id&&['tool_end','tool_error'].includes(e.type));
      const state=row?.status||ev?.data?.run_status||'running';
      const failed=state==='failed',stopped=state==='cancelled',busy=['running','queued','pending','cancelling'].includes(state);
      const action=env.action||ev?.data?.document_action||relevant.find(e=>e.data?.run_id===id)?.data?.tool?.replace('document_','');
      return <div key={id} role={failed?'alert':undefined} style={{color:failed?t.err:stopped?t.mut:t.dim,display:'flex',gap:8,alignItems:'center'}}>
        <span>{failed?`Document failed: ${env.error||ev?.data?.status||'Processing failed'}`:stopped?'Document stopped':state==='cancelling'?'Stopping document…':busy?'Processing document…':labels[action]||'Document operation finished'}</span>
        {state==='cancelling'&&env.error&&<span role="status">{env.error}</span>}
        {busy&&<button onClick={async()=>{try{const r=await fetch(`${API}/api/runs/${encodeURIComponent(id)}/cancel`,{method:'POST'});const result=await r.json();if(!r.ok)throw new Error(result.detail||'Could not stop the document job.');setRuns(old=>({...old,[id]:{...old[id],id,status:result.status}}));setError(result.error||'');}catch(e){setError(e.message||'Could not stop the document job.');}}} style={{background:'none',border:`1px solid ${t.brd}`,borderRadius:5,color:t.mut,cursor:'pointer'}}>{state==='cancelling'?'Retry Stop':'Stop'}</button>}
      </div>;
    })}
    {validations.map(v=><div role="alert" key={v} style={{color:t.err}}>{v}</div>)}
    {error&&<div role="status" style={{color:t.mut}}>{error}</div>}
    {files.map(a=><div key={a.artifact_id} style={{display:'flex',alignItems:'center',gap:8,padding:'9px 11px',border:`1px solid ${t.brd}55`,borderRadius:8,background:`${t.surface}88`}}>
      <span style={{flex:1,minWidth:0,overflowWrap:'anywhere',color:t.text}}>{a.filename}</span>
      <a href={userScopedUrl(a.url)} download={a.filename} style={{color:t.ok}}>Download</a>
      <button title="Preview" aria-label={`Preview ${a.filename}`} onClick={()=>onPreview(a.filename,a.url)} style={{background:'none',border:`1px solid ${t.acc}55`,borderRadius:5,padding:'4px 8px',color:t.acc,cursor:'pointer'}}>👁</button>
    </div>)}
  </div>;
}
