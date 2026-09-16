import React,{useEffect,useState,useRef} from 'react';
import {API,userScopedUrl} from '../session.js';
import {applyJobEvent,consumeSseFrames,terminalJobStates,jobBlockerMessage} from '../daedalusJobs.js';

export default function DaedalusJobCard({workflow,t,font,onOpenArtifact}){
  const [job,setJob]=useState(workflow),[error,setError]=useState(''),[reload,setReload]=useState(0),[busy,setBusy]=useState(false);
  useEffect(()=>{
    const controller=new AbortController();let stopped=false;
    setJob(current=>current.id===workflow.id?current:workflow);setError('');
    const start=async()=>{
      let sequence=0;
      while(!stopped){
        try{
          const snapshot=await fetch(`${API}/api/coder/workflows/${workflow.id}`,{signal:controller.signal});
          if(!snapshot.ok)throw new Error('Unable to load coding job');
          const current=await snapshot.json();if(stopped)return;setJob(current);sequence=Math.max(sequence,current.event_sequence||0);
          if(['completed','cancelled'].includes(current.state))return;
          if(terminalJobStates.has(current.state)){await new Promise(resolve=>setTimeout(resolve,4000));continue;}
          const response=await fetch(`${API}/api/coder/workflows/${workflow.id}/events?after=${sequence}`,{signal:controller.signal});
          if(!response.ok||!response.body)throw new Error('Progress disconnected; reconnecting');
          const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
          while(!stopped){
            const {value,done}=await reader.read();if(done)break;
            buffer+=decoder.decode(value,{stream:true});
            const parsed=consumeSseFrames(buffer);buffer=parsed.remainder;
            for(const event of parsed.events){
              if(event.seq<=sequence)continue;
              const previous=sequence;sequence=event.seq;
              setJob(job=>applyJobEvent(job,event,previous).job);
            }
            setError('');
          }
        }catch(e){if(stopped||e.name==='AbortError')return;setError(e.message);}
        await new Promise(resolve=>setTimeout(resolve,1500));
      }
    };
    start();return()=>{stopped=true;controller.abort();};
  },[workflow.id,reload]);
  const action=async(name)=>{
    setBusy(true);setError('');
    try{
      const response=await fetch(`${API}/api/coder/workflows/${job.id}/${name}`,{method:'POST'});
      const result=await response.json();if(!response.ok)throw new Error(result.detail||'Job action failed');
      setJob(result);setReload(n=>n+1);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  };
  const active=!terminalJobStates.has(job.state);
  const milestone=job.milestones?.[job.milestone_index||0];
  const button={background:t.bgDeep,border:`1px solid ${t.brd}`,borderRadius:6,padding:'5px 10px',color:t.acc,fontFamily:font,cursor:'pointer'};
  return <section aria-label="Daedalus coding job" style={{fontFamily:font,border:`1px solid ${t.brd}`,borderRadius:10,padding:14,margin:'10px 0',background:t.surface,color:t.text}}>
    <div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}>
      <strong style={{fontSize:13}}>Daedalus</strong><span role="status" style={{fontSize:12,color:t.acc}}>{job.state}</span>
      <span style={{fontSize:11,color:t.mut}}>{job.model}</span>
      {active&&<button disabled={busy||job.state==='cancelling'} onClick={()=>action('cancel')} style={{...button,marginLeft:'auto',color:t.err}}>Stop</button>}
      {['blocked','waiting_for_input'].includes(job.state)&&<button disabled={busy} onClick={()=>action('resume')} style={{...button,marginLeft:'auto'}}>Continue from checkpoint</button>}
    </div>
    {milestone&&<div style={{marginTop:10,fontSize:12}}>{(job.milestone_index||0)+1} / {job.milestones.length} · {milestone.title||milestone.task}</div>}
    <div style={{display:'flex',gap:14,flexWrap:'wrap',fontSize:10,color:t.mut,marginTop:8}}>
      {job.inventory&&<span>{Number(job.inventory.files||0).toLocaleString()} files · {Number(job.inventory.lines||0).toLocaleString()} lines</span>}
      <span>{(job.calls_used||0)+(job.operation_calls||0)} model calls · {Math.round(((job.seconds_used||0)+(job.operation_seconds||0))/60)} minutes used</span>
      {job.context&&<span>{Number(job.context.num_ctx).toLocaleString()} context · {job.context.compaction?"compaction on":"compaction off"}</span>}
      {job.revision_id&&<span>Revision {job.revision_id.slice(0,10)}</span>}
    </div>
    {job.blocker&&<p style={{fontSize:12,color:t.err,whiteSpace:'pre-wrap'}}>{jobBlockerMessage(job.blocker)}</p>}
    {job.answer&&<p style={{fontSize:12,whiteSpace:'pre-wrap'}}>{job.answer}</p>}
    {job.workspace&&<ProjectEvidence jobId={job.id} t={t} font={font}/>}
    {!!job.checks?.length&&<details style={{marginTop:10,fontSize:11}}><summary style={{cursor:'pointer'}}>Checks · {job.checks.filter(c=>c.passed).length}/{job.checks.length} passed</summary>
      {job.checks.map((check,index)=><div key={`${check.id}-${index}`} style={{marginTop:7,color:check.passed?t.ok:t.err}}>{check.passed?'✓':'!'} {check.id} · {check.cwd||'.'}<pre style={{whiteSpace:'pre-wrap',maxHeight:180,overflow:'auto',fontSize:10,color:t.mut}}>{check.log_tail||check.command||check.error||check.summary}</pre>{(check.log||check.server_log||check.screenshot)&&<CheckEvidence jobId={job.id} check={check} button={button}/>}</div>)}
    </details>}
    {job.state==='completed'&&job.artifact&&<div style={{display:'flex',gap:8,marginTop:12}}>
      <a style={button} href={userScopedUrl(`/api/artifacts/${job.artifact.id}/download`)} download={job.artifact.filename}>Download accepted revision</a>
      {onOpenArtifact&&<button style={button} onClick={()=>onOpenArtifact(job.artifact.id)}>Artifact details</button>}
    </div>}
    {error&&<div role="alert" style={{color:t.err,fontSize:11,marginTop:8}}>{error}</div>}
  </section>;
}

function ProjectEvidence({jobId,t,font}){
  const [kind,setKind]=useState('files'),[query,setQuery]=useState(''),[result,setResult]=useState(null),[request,setRequest]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const generation=useRef(0);
  useEffect(()=>{setResult(null);setRequest(null);setError('');setBusy(false);return()=>{generation.current++;};},[jobId]);
  const load=async(body)=>{
    const version=++generation.current;setBusy(true);setError('');
    try{
      const response=await fetch(`${API}/api/coder/workflows/${jobId}/inspect`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();if(version!==generation.current)return;
      if(!response.ok)throw new Error(data.detail||'Inspection failed');
      setResult(data);setRequest(body);
    }catch(e){if(version===generation.current)setError(e.message);}finally{if(version===generation.current)setBusy(false);}
  };
  const control={background:t.bgDeep,color:t.text,border:`1px solid ${t.brd}`,borderRadius:5,padding:'5px 8px',fontFamily:font,fontSize:11};
  const next=()=>{
    if(result.next_offset!=null)load({...request,offset:result.next_offset,sha256:result.sha256});
    else if(result.next_line!=null)load({...request,start:result.next_line,sha256:result.sha256});
    else if(result.next_cursor!=null)load({...request,cursor:result.next_cursor});
  };
  return <details style={{marginTop:10,fontSize:11}} onToggle={e=>{if(e.currentTarget.open&&!result&&!busy)load({operation:'files'});}}>
    <summary style={{cursor:'pointer'}}>Browse project evidence</summary>
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

function CheckEvidence({jobId,check,button}){
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
