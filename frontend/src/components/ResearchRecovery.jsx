import React,{useEffect,useState} from 'react';
import {apiJson} from '../api.js';

export default function ResearchRecovery({report,t,btnS,inputS,onRefresh}){
  const [open,setOpen]=useState(false),[versions,setVersions]=useState([]),[question,setQuestion]=useState(''),[answer,setAnswer]=useState(''),[diff,setDiff]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{const c=new AbortController();setAnswer('');setDiff('');setError('');setVersions([]);apiJson(`/api/research/reports/${report.id}/versions`,{signal:c.signal}).then(d=>setVersions(d.versions||[])).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[report.id,report.updated_at]);
  const run=async(fn)=>{setBusy(true);setError('');try{await fn();}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{setBusy(false);}};
  return <div style={{padding:'8px 14px',borderBottom:`1px solid ${t.brd}`,fontSize:12}}>
    <button style={btnS(t.acc)} onClick={()=>setOpen(!open)}>Saved evidence & versions</button>
    {versions.length>0&&['interrupted','failed','partial','cancelled'].includes(report.status)&&<button disabled={busy} style={{...btnS(t.ok),marginLeft:8}} onClick={()=>run(async()=>{await apiJson(`/api/research/reports/${report.id}/resume`,{method:'POST'});onRefresh?.();})}>Resume saved work</button>}
    {open&&<div style={{display:'grid',gap:8,marginTop:8}}>
      <span>{versions.length?`${versions.length} recorded checkpoints. Resume keeps the saved evidence and settings.`:'Historical checkpoints are unavailable. Use Rerun to collect fresh sources.'}</span>
      {versions.length>1&&<button disabled={busy} style={btnS(t.mut)} onClick={()=>run(async()=>{const d=await apiJson(`/api/research/reports/${report.id}/compare?older=${versions.at(-1).id}&newer=${versions[0].id}`);setDiff(d.diff||'No text differences.');})}>Compare earliest and latest drafts</button>}
      {diff&&<pre style={{maxHeight:240,overflow:'auto',whiteSpace:'pre-wrap',color:t.text}}>{diff}</pre>}
      <label>Ask about saved evidence<input value={question} onChange={e=>setQuestion(e.target.value)} style={{...inputS,width:'100%'}} placeholder="What do the saved sources say?"/></label>
      <button disabled={busy||!question.trim()} style={btnS(t.acc)} onClick={()=>run(async()=>{const d=await apiJson(`/api/research/reports/${report.id}/question`,{method:'POST',body:{question}});setAnswer(d.answer);})}>{busy?'Working…':'Ask using saved sources'}</button>
      {answer&&<div style={{whiteSpace:'pre-wrap',lineHeight:1.6}}>{answer}</div>}
    </div>}
    {error&&<div role="alert" style={{color:t.err,marginTop:8}}>{error}</div>}
  </div>;
}
