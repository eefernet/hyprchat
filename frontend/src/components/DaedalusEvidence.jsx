import React,{useEffect,useState,useRef} from 'react';
import {API,userScopedUrl} from '../session.js';
import {fileActivity} from '../daedalusProgress.js';

// Evidence browsers shared by the job card and the workbench: screenshots and
// traces, project revisions, the live-workspace inspector, check logs and the
// file-activity list. Moved verbatim from DaedalusJobCard.jsx.
export function BrowserEvidence({jobId,check,button}){
  return <div>{(check.screenshots||[]).map(shot=><details key={shot.evidence.id}><summary>{shot.viewport} screenshot</summary><img loading="lazy" src={userScopedUrl(`/api/coder/workflows/${jobId}/evidence/${shot.evidence.id}`)} alt={`${shot.viewport} browser check`} style={{maxWidth:'100%'}}/></details>)}
    {(check.traces||[]).map(trace=><a key={trace.evidence.id} style={button} href={userScopedUrl(`/api/coder/workflows/${jobId}/evidence/${trace.evidence.id}`)} download={`${trace.viewport}-trace.zip`}>Download {trace.viewport} test trace</a>)}
    {!!check.traces?.length&&<p style={{fontSize:"var(--hc-text-secondary)"}}>Replay locally with Playwright Trace Viewer: npx playwright show-trace &lt;trace.zip&gt;</p>}
  </div>;
}

export function ProjectHistory({projectId,refreshKey,button}){
  const [rows,setRows]=useState(null),[error,setError]=useState('');
  useEffect(()=>{setRows(null);setError('');},[projectId,refreshKey]);
  const load=async()=>{try{const r=await fetch(`${API}/api/coder/projects/${projectId}/history`);if(!r.ok)throw new Error('Unable to load project history');setRows(await r.json());}catch(e){setError(e.message);}};
  return <details style={{fontSize:"var(--hc-text-secondary)",marginTop:8}} onToggle={e=>{if(e.currentTarget.open&&!rows)load();}}><summary>Project revisions</summary>
    {rows?.map(row=><p key={row.id} title={row.user_task}>{String(row.user_task||'').replace(/\s+/g,' ').slice(0,110)}{(row.user_task||'').length>110?'…':''} · {row.state} · {row.revision_id?.slice(0,10)} {row.artifact&&<a style={button} href={userScopedUrl(`/api/artifacts/${row.artifact.id}/download`)}>Download revision</a>}</p>)}{error&&<p role="alert">{error}</p>}
  </details>;
}

export function ProjectEvidence({jobId,changedAt,t,font}){
  const [kind,setKind]=useState('files'),[query,setQuery]=useState(''),[result,setResult]=useState(null),[request,setRequest]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const generation=useRef(0),opened=useRef(false),latestRequest=useRef(null);
  useEffect(()=>{if(!opened.current)return;const timer=setTimeout(()=>load({operation:'files',refresh:true}),750);return()=>clearTimeout(timer);},[changedAt]);
  useEffect(()=>{setResult(null);setRequest(null);setError('');setBusy(false);return()=>{generation.current++;};},[jobId]);
  const load=async(body)=>{
    const version=++generation.current;setBusy(true);setError('');
    try{
      const response=await fetch(`${API}/api/coder/workflows/${jobId}/inspect`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();if(version!==generation.current)return;
      if(!response.ok)throw new Error(data.detail||'Inspection failed');
      setResult(data);setRequest(body);latestRequest.current=body;
    }catch(e){if(version===generation.current)setError(e.message);}finally{if(version===generation.current)setBusy(false);}
  };
  const control={background:t.bgDeep,color:t.text,border:`1px solid ${t.brd}`,borderRadius:"var(--hc-radius-sm)",padding:'5px 8px',fontFamily:font,fontSize:"var(--hc-text-secondary)"};
  const next=()=>{
    if(result.next_offset!=null)load({...request,offset:result.next_offset,sha256:result.sha256});
    else if(result.next_line!=null)load({...request,start:result.next_line,sha256:result.sha256});
    else if(result.next_cursor!=null)load({...request,cursor:result.next_cursor});
  };
  return <details style={{marginTop:10,fontSize:"var(--hc-text-secondary)"}} onToggle={e=>{opened.current=e.currentTarget.open;if(opened.current&&!result&&!busy)load({operation:'files',refresh:true});}}>
    <summary style={{cursor:'pointer'}}>Browse working files</summary><p className="dp-muted">Live workspace. Checks refer to the saved revision shown in their evidence.</p>
    <form onSubmit={e=>{e.preventDefault();load({operation:kind,query});}} style={{display:'flex',gap:6,flexWrap:'wrap',margin:'10px 0'}}>
      <select aria-label="Evidence type" value={kind} onChange={e=>setKind(e.target.value)} style={control}><option value="files">Files</option><option value="symbols">Symbols</option><option value="search">Text search</option><option value="dependencies">Imports</option></select>
      <input aria-label="Search project evidence" value={query} onChange={e=>setQuery(e.target.value)} style={{...control,flex:1,minWidth:100}} placeholder="Path, symbol, or text"/>
      <button disabled={busy} style={control}>Search</button>
    </form>
    {result?.path&&<div style={{color:t.mut}}>{result.path} · {result.start!=null?`line ${result.start}`:`byte ${result.offset||0}`} · {result.sha256?.slice(0,10)}</div>}
    {result?.content!=null&&<pre style={{whiteSpace:'pre-wrap',maxHeight:300,overflow:'auto',background:t.bgDeep,padding:8}}>{result.content}</pre>}
    {result?.items?.map((row,index)=><div key={row.cursor||index} style={{padding:'4px 0',borderBottom:`1px solid ${t.brd}`}}>
      <button style={{...control,border:0,padding:0,cursor:'pointer',color:t.acc,textAlign:'left'}} onClick={()=>load({path:row.path,sha256:row.sha256,...(row.line?{operation:'read',start:row.line,limit:100}:{operation:'bytes',offset:0,length:32768})})}>{row.path}{row.line?`:${row.line}`:''}</button>
      {(row.name||row.target)&&<span style={{marginLeft:8,color:t.mut}}>{row.name||row.target}</span>}
      {row.text&&<pre style={{whiteSpace:'pre-wrap',margin:'4px 0',maxHeight:80,overflow:'auto'}}>{row.text}</pre>}
    </div>)}
    {!busy&&result?.items?.length===0&&<p>No matching evidence.</p>}
    {(result?.next_cursor!=null||result?.next_offset!=null||result?.next_line!=null)&&<button disabled={busy} onClick={next} style={{...control,marginTop:8,cursor:'pointer'}}>Next page</button>}
    {busy&&<p role="status">Loading evidence…</p>}{error&&<p role="alert" style={{color:t.err}}>{error}</p>}
  </details>;
}

export function CheckEvidence({jobId,check,button}){
  const [result,setResult]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const generation=useRef(0);
  useEffect(()=>{setResult(null);setError('');setBusy(false);return()=>{generation.current++;};},[jobId,check.log,check.server_log,check.screenshot]);
  const load=async(operation,offset=0)=>{
    const version=++generation.current;setBusy(true);setError('');
    try{
      const response=await fetch(`${API}/api/coder/workflows/${jobId}/inspect`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({operation,path:operation==='screenshot'?check.screenshot:(check.log||check.server_log),offset})});
      const data=await response.json();if(version!==generation.current)return;
      if(!response.ok)throw new Error(data.detail||'Evidence unavailable');setResult(data);
    }catch(e){if(version===generation.current)setError(e.message);}finally{if(version===generation.current)setBusy(false);}
  };
  return <div>
    {(check.log||check.server_log)&&<button disabled={busy} onClick={()=>load('log')} style={button}>Read full log</button>}
    {check.screenshot&&<button disabled={busy} onClick={()=>load('screenshot')} style={button}>Browser screenshot</button>}
    {result?.content!=null&&<pre style={{whiteSpace:'pre-wrap',maxHeight:240,overflow:'auto'}}>{result.content}</pre>}
    {result?.next_offset!=null&&<button disabled={busy} onClick={()=>load('log',result.next_offset)} style={button}>Next log page</button>}
    {result?.image&&<img src={result.image} alt="Browser verification screenshot" style={{display:'block',maxWidth:'100%',marginTop:8}}/>}
    {error&&<p role="alert">{error}</p>}
  </div>;
}

export function FileActivity({job,defaultOpen=false}){
  const [expanded,setExpanded]=useState(defaultOpen),[older,setOlder]=useState([]),[cursor,setCursor]=useState(0),[more,setMore]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const current=fileActivity(job);
  const rows=[...new Map([...(job.progress?.files||[]),...older].filter(row=>row.kind==='file').map(row=>[row.id,row])).values()];
  const load=async()=>{
    setBusy(true);setError('');
    try{const response=await fetch(userScopedUrl(`/api/coder/workflows/${job.id}/events?view=activity&before=${cursor}&limit=50`));
      const data=await response.json();if(!response.ok)throw new Error(data.detail||'Activity unavailable');
      setOlder(previous=>[...previous,...data.items]);setCursor(data.next_cursor||0);setMore(data.next_cursor!=null);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  };
  return <details className="dp-section dp-file-activity" open={expanded} onToggle={e=>setExpanded(e.currentTarget.open)}>
    <summary>{current.label} {current.path&&<code>{current.path}</code>}</summary>
    {!rows.length&&<p className="dp-muted">No file changes have been recorded for this job.</p>}
    <ol className="dp-activity-list">{rows.map(row=><li key={row.id}><span>{({create:'Created',edit:'Edited',delete:'Deleted'})[row.action]} <code>{row.path}</code></span><span className="dp-muted">{row.outcome==='succeeded'?'Saved':row.outcome==='interrupted'?'Interrupted':'Failed'}{row.at&&<> · <time dateTime={new Date(typeof row.at==='number'?row.at*1000:row.at).toISOString()}>{new Date(typeof row.at==='number'?row.at*1000:row.at).toLocaleTimeString()}</time></>}</span></li>)}</ol>
    {more&&rows.length>=50&&<button type="button" disabled={busy} onClick={load}>{busy?'Loading…':'Load earlier activity'}</button>}
    {error&&<p role="alert">{error}</p>}
  </details>;
}
