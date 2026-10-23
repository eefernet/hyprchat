import SelectionControl from '../components/SelectionControl.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import {IC} from '../components/icons.jsx';
import React,{useState,useEffect,useRef} from 'react';
import OperationsSummary from '../components/OperationsSummary.jsx';
import { API, userScopedUrl } from '../session.js';
import { apiJson } from '../api.js';

const kinds=['research','coding','coding_run','document','image','indexing','scheduled'];
export default function JobsPanel({t,btnS,inputS,onNavigate,workspaces=[],workspaceId="",embedded=false,onAddContent,focusId=null}){
  const [jobs,setJobs]=useState([]),[kind,setKind]=useState(''),[status,setStatus]=useState('');
  const [error,setError]=useState(''),[busy,setBusy]=useState(''),[refresh,setRefresh]=useState(0);
  const [offset,setOffset]=useState(0);
  const [workspace,setWorkspace]=useState(workspaceId),[runResult,setRunResult]=useState(null);
  const epoch=useRef(0);
  useEffect(()=>{
    const version=++epoch.current,controller=new AbortController();
    let stream,timer,loading=false;
    const load=async()=>{
      if(loading)return;loading=true;
      try{
        const data=await apiJson(`/api/jobs?kind=${kind}&status=${status}&offset=${offset}&limit=50&workspace_id=${workspace}`,{signal:controller.signal});
        if(version!==epoch.current)return;
        if(focusId&&!data.jobs.some(j=>j.id===focusId)){const focused=await apiJson(`/api/jobs/${encodeURIComponent(focusId)}`,{signal:controller.signal});if(version!==epoch.current)return;data.jobs=[focused,...data.jobs];}
        setJobs(data.jobs);setError('');
        if(!stream){
          stream=new EventSource(userScopedUrl(`/api/events/jobs?after=${data.cursor}`));
          stream.onmessage=()=>{clearTimeout(timer);timer=setTimeout(load,150);};
          stream.onerror=()=>{if(version===epoch.current)setError('Live updates disconnected. Reconnecting…');};
        }
      }catch(e){if(e.name!=='AbortError'&&version===epoch.current)setError(e.message);}
      finally{loading=false;}
    };
    load();const poll=setInterval(load,10000);
    return()=>{epoch.current++;controller.abort();stream?.close();clearTimeout(timer);clearInterval(poll);};
  },[kind,status,offset,refresh,workspace,focusId]);
  useEffect(()=>{if(focusId&&jobs.some(j=>j.id===focusId))document.getElementById(`job-${focusId}`)?.scrollIntoView({block:'center'});},[focusId,jobs.some(j=>j.id===focusId)]);
  const act=async(job,action)=>{
    const version=epoch.current;setBusy(job.id);setError('');
    try{
      await apiJson(`/api/jobs/${encodeURIComponent(job.id)}/actions/${action}`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate_revision:job.details.revision_id})});
      if(version===epoch.current)setRefresh(n=>n+1);
    }catch(e){if(e.name!=='AbortError'&&version===epoch.current)setError(e.message);}
    finally{if(version===epoch.current)setBusy('');}
  };
  return <section style={{padding:embedded?0:20,overflowY:embedded?'visible':'auto',height:embedded?'auto':'100%',color:t.text}}>
    <PanelHeader embedded={embedded} t={t} icon={<IC.Layers/>} title="Jobs" subtitle="Progress and saved outputs from background work."/>
    <div style={{display:'flex',gap:8,flexWrap:'wrap',marginBottom:16}}>
      {!embedded&&<select aria-label="Jobs workspace" value={workspace} disabled={!!workspaceId} onChange={e=>{setWorkspace(e.target.value);setOffset(0);}} style={{...inputS,flex:'1 1 160px',width:'auto'}}><option value="">All workspaces</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select>}
      <SelectionControl label="Job type" value={kind} onChange={v=>{setKind(v);setOffset(0);}} options={[{value:'',label:'All types'},...kinds.map(value=>({value,label:value}))]}/>
      <SelectionControl label="Job status" value={status} onChange={v=>{setStatus(v);setOffset(0);}} options={[{value:'',label:'All states'},...['active','queued','running','cancelling','succeeded','partial','failed','cancelled','interrupted'].map(value=>({value,label:value}))]}/>
      <button style={btnS(t.acc)} onClick={()=>setRefresh(n=>n+1)}>Refresh</button>
    </div>
    {!embedded&&<OperationsSummary t={t}/>}
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {runResult&&<article style={{padding:14,border:`1px solid ${t.brd}`,whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}><button style={btnS(t.acc)} onClick={()=>setRunResult(null)}>Close result</button><p>{runResult.result_text||runResult.error||'No output saved yet.'}</p></article>}
    {!jobs.length&&<p style={{color:t.dim}}>No jobs match these filters.</p>}
    <div style={{display:'grid',gap:12}}>{jobs.map(job=><article id={`job-${job.id}`} key={job.id} style={{padding:16,border:`1px solid ${t.brd}`,borderRadius:10,overflowWrap:'anywhere'}}>
      <div style={{fontWeight:700}}>{job.title||job.kind}</div>
      <p style={{color:t.dim,fontSize:12}}>{job.kind} · {job.status} · {job.phase}
        {job.elapsed_seconds!=null&&` · ${Math.floor(job.elapsed_seconds/60)}m ${job.elapsed_seconds%60}s`}</p>
      {job.progress!=null&&<progress value={job.progress} max="100" aria-label="Job progress"/>}
      {job.error&&<p role="status" style={{color:t.err}}>{job.error}</p>}
      {job.status==='interrupted'&&<p style={{fontSize:12}}>Review saved outputs and possible external effects before starting again.</p>}
      {job.details.results?.length>0&&<details><summary>Per-file results</summary>{job.details.results.map(result=><div key={result.artifact_id} style={{margin:8}}>{result.artifact_id}: {result.status}{result.error&&` — ${result.error}`}{result.outputs?.map(output=><a key={output.artifact_id} href={userScopedUrl(output.url)} style={{display:'block',color:t.acc}}>{output.filename}</a>)}</div>)}</details>}
      <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
        {job.kind==='scheduled'&&<button style={btnS(t.acc)} onClick={async()=>{try{setRunResult(await apiJson(`/api/tasks/runs/${job.domain_id}`));}catch(e){setError(e.message);}}}>Read result</button>}
        {job.details.kb_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('kb',job.details.kb_id)}>Open knowledge base</button>}
        {job.details.conversation_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('chat',job.details.conversation_id)}>Open conversation</button>}
        {job.details.report_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('research',job.details.report_id)}>Open report</button>}
        {job.details.artifact_id&&<button style={btnS(t.acc)} onClick={()=>onNavigate('artifacts',job.details.artifact_id)}>Open artifact</button>}
        {(job.details.images||[]).map((image,i)=>{const url=typeof image==='string'?image:image.url;return url?<a key={i} href={userScopedUrl(url)} target="_blank" rel="noreferrer" style={{color:t.acc}}>Image {i+1}</a>:null;})}
        {job.actions.map(action=><button key={action} disabled={busy===job.id} style={btnS(action==='cancel'?t.warm:t.acc)} onClick={()=>act(job,action)}>{action==='rerun'?'Run again':action==='retry'?(job.kind==='image'?'Retry transfer':'Retry indexing'):action==='resume'?'Resume':'Cancel'}</button>)}
      </div>
    </article>)}</div>
    <div style={{display:'flex',gap:12,marginTop:16}}>
      <button disabled={!offset} style={btnS(t.acc)} onClick={()=>setOffset(n=>Math.max(0,n-50))}>Previous</button>
      <button disabled={jobs.length<50} style={btnS(t.acc)} onClick={()=>setOffset(n=>n+50)}>Next</button>
    </div>
  </section>;
}
