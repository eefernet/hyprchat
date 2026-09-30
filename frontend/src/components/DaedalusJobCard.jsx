import React,{useEffect,useState,useRef} from 'react';
import {API,userScopedUrl} from '../session.js';
import {terminalJobStates,jobBlockerMessage,jobStatusLabel,jobActivity,continueReason,elapsedLabel,timestamp} from '../daedalusJobs.js';
import {loopLabel,nowLine,outcomeExplanation} from '../daedalusProgress.js';
import {useDaedalusJob} from '../daedalusJobStore.js';
import {StageRail,ResultBanner,PlanSection,ChecksSection} from './DaedalusPlanPanel.jsx';
import './daedalusJob.css';

export default function DaedalusJobCard({workflow,t,font,md,onOpenArtifact,compact=false}){
  const {job,connection,error,busy,action}=useDaedalusJob(workflow);
  const [details,setDetails]=useState(false),[tab,setTab]=useState('Overview'),[now,setNow]=useState(Date.now());
  const [resumeVisual,setResumeVisual]=useState('unchanged');
  const [clarification,setClarification]=useState('');
  const dialog=useRef(null),opener=useRef(null);
  const active=!terminalJobStates.has(job.state),status=jobStatusLabel(job);
  useEffect(()=>{if(!active)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[active]);
  useEffect(()=>{if(details){opener.current=document.activeElement;dialog.current?.showModal();}else if(dialog.current?.open){dialog.current.close();opener.current?.focus();}},[details]);
  const button={background:t.bgDeep,border:`1px solid ${t.brd}`,borderRadius:8,padding:'8px 12px',color:t.text,fontFamily:font,fontSize:13,cursor:'pointer',textDecoration:'none',display:'inline-block'};
  const vars={'--dj-surface':t.surface,'--dj-bg':t.bgDeep,'--dj-text':t.text,'--dj-muted':t.dim||t.mut,'--dj-border':t.brd,'--dj-accent':t.acc,'--dj-good':t.ok,fontFamily:font};
  const name=job.presentation?.project_name||job.project_name||'Daedalus project';
  const elapsed=(job.seconds_used||0)+(active?Math.max(job.operation_seconds||0,job.worker_contact_at?(now-timestamp(job.worker_contact_at))/1000+(job.operation_seconds||0):0):0);
  const updated=timestamp(job.meaningful_update_at||job.updated_at||job.created_at);
  const resumable=['blocked','waiting_for_input','ready_for_review','cancelled'].includes(job.state);
  const disabledReason=continueReason(job);
  const calls=(job.calls_used||0)+(job.operation_calls||0);
  const facts=[job.model,`${elapsedLabel(elapsed)} elapsed`,job.call_limit?`${calls} of ${job.call_limit} model calls`:`${calls} model call${calls===1?'':'s'}`,job.inventory?.files?`${job.inventory.files} files`:''].filter(Boolean).join(' · ');
  const artifact=job.artifact||job.candidate_artifact;
  const controls=<>{active?<button style={button} disabled={busy||job.state==='cancelling'} onClick={()=>action('cancel')}>{job.state==='cancelling'?'Stopping…':'Stop'}</button>:resumable?<button style={button} disabled={busy||!!disabledReason||!!job.scope_question&&!clarification.trim()} title={disabledReason} onClick={()=>action('resume',resumeVisual,clarification)}>Continue</button>:null}</>;
  const downloads=<>{artifact&&<a style={button} href={userScopedUrl(`/api/artifacts/${artifact.id}/download`)} download>{job.artifact?'Download accepted project':'Download candidate'}</a>}
    {!artifact&&resumable&&job.revision_id&&<a style={button} href={userScopedUrl(`/api/coder/workflows/${job.id}/checkpoint`)} download>Download checkpoint</a>}</>;
  const evidence=check=><><CheckEvidence jobId={job.id} check={check} button={button}/><BrowserEvidence jobId={job.id} check={check} button={button}/></>;
  const progress=<>
    <StageRail job={job}/>
    {active&&<p className="dp-now" role="status" aria-live="polite"><strong>Now:</strong> {nowLine(job)||jobActivity(job)}{Number.isFinite(updated)?<span className="dp-facts"> · updated {elapsedLabel((now-updated)/1000)} ago</span>:null}</p>}
    {!active&&<ResultBanner job={job}/>}
    {connection==='reconnecting'&&<p className="dj-notice">Reconnecting to progress. Last confirmed status is shown.</p>}
    {active&&job.blocker&&<p className="dj-notice">{jobBlockerMessage(job.blocker)}</p>}
    {resumable&&disabledReason&&!outcomeExplanation(job)&&<p className="dj-notice">{disabledReason}</p>}
    {resumable&&job.scope_question&&<label style={{display:'block'}}>Clarify this change: {job.scope_question}<textarea aria-label="Scope clarification" value={clarification} maxLength={10000} onChange={e=>setClarification(e.target.value)} style={{...button,display:'block',width:'100%',boxSizing:'border-box',minHeight:85,marginTop:8}}/></label>}
  </>;
  if(compact)return <>
    <section className="dj-card dj-strip" aria-label="Background coding job" style={vars}>
      <div className="dj-heading"><strong>{name}</strong><span role="status" aria-live="polite" className="dj-state"><span aria-hidden="true" className={active&&connection==='connected'&&job.state!=='cancelling'?'dj-dot dj-active':'dj-dot'}/>{status}{loopLabel(job)?` · ${loopLabel(job)}`:''}</span>
        <span className="dj-actions"><button style={button} onClick={()=>setDetails(true)}>View details</button>{controls}</span></div>
      {error&&<p role="alert">{error}</p>}
    </section>{renderDialog()}</>;
  return <>
    <section className="dj-card" aria-label="Daedalus coding job" style={vars}>
      <div className="dj-heading"><strong>{name}</strong><span role="status" aria-live="polite" className="dj-state"><span aria-hidden="true" className={active&&connection==='connected'&&job.state!=='cancelling'?'dj-dot dj-active':'dj-dot'}/>{status}</span></div>
      <p className="dp-facts">{facts}</p>
      {progress}
      {job.answer&&<div className="dp-answer">{md?md(job.answer):job.answer}</div>}
      <PlanSection job={job} defaultOpen={job.state!=='completed'}/>
      <ChecksSection job={job} renderEvidence={evidence}/>
      <div className="dj-actions" style={{marginTop:12}}><span style={{display:'flex',gap:8,flexWrap:'wrap'}}><button style={button} onClick={()=>setDetails(true)}>View details</button>{downloads}</span>{controls}</div>
      {error&&<p role="alert">{error}</p>}
    </section>{renderDialog()}</>;

  function renderDialog(){return <dialog ref={dialog} className="dj-dialog" aria-label={`${name} details`} style={vars} onCancel={()=>setDetails(false)} onClose={()=>{setDetails(false);opener.current?.focus();}}>
      <header className="dj-heading"><strong>{name}</strong><button style={button} onClick={()=>setDetails(false)} aria-label="Close job details">Close</button></header>
      <nav className="dj-tabs" aria-label="Job detail sections">{['Overview','Checks','Files & logs','History'].map(label=><button key={label} style={button} aria-pressed={tab===label} onClick={()=>setTab(label)}>{label}</button>)}</nav>
      {details&&<div className="dj-content">
        {tab==='Overview'&&<>
          <h2>{status}</h2><p className="dp-facts">{facts}{job.context?` · ${Number(job.context.num_ctx).toLocaleString()} context`:''}</p>
          {progress}
          {job.answer&&<p>{job.answer}</p>}
          <PlanSection job={job}/>
          <div className="dj-download">{downloads}{artifact&&onOpenArtifact&&<button style={button} onClick={()=>onOpenArtifact(artifact.id)}>Artifact details</button>}</div>
          {(job.candidate_artifact?.metadata?.run_instructions||[]).map(profile=><div key={profile.cwd}><h3>Run from {profile.cwd}</h3><pre>{[...(profile.commands?.setup||[]),...(profile.commands?.build||[]),...(profile.commands?.test||[]),profile.launch?.replaceAll('{port}','8000')].filter(Boolean).join('\n')}</pre></div>)}
          {resumable&&<><label>Visual review when continuing <select value={resumeVisual} onChange={e=>setResumeVisual(e.target.value)} style={button}><option value="unchanged">Keep setting</option><option value="on">Enable</option><option value="off">Disable</option></select></label><p className="dp-muted">{disabledReason||'Continue uses this exact saved candidate revision.'}</p>{controls}</>}
          <details className="dp-section dp-request"><summary>Original request</summary><div className="dp-request-body">{md?md(job.user_task||''):job.user_task}</div></details>
          {(job.blocker||job.failure)&&<details className="dp-section"><summary>Diagnostic</summary>{job.blocker&&<pre>{job.blocker}</pre>}{job.failure&&<pre>{JSON.stringify(job.failure,null,2)}</pre>}</details>}
          <p className="dp-muted">Revision <code>{job.revision_id?job.revision_id.slice(0,12):'none yet'}</code></p>
        </>}
        {tab==='Checks'&&<>
          <h2>Checks on this revision</h2>
          {!job.checks?.length&&<p>No checks have completed yet.</p>}
          <ChecksSection job={job} renderEvidence={evidence}/>
          {!!job.verification_summary?.criteria?.length&&<details className="dp-section"><summary>Independent verification of requirements</summary>
            <p>Passing generated audits do not prove every requested behavior. Requirements below need independent verification before automatic acceptance.</p>
            {job.verification_summary.criteria.map(criterion=><div className="dj-row" key={criterion.id}><strong>{criterion.status==='passed'?'Verified':'Needs verification'}</strong><p>{criterion.request_excerpt}</p></div>)}
          </details>}
          {!!job.checks?.length&&<details className="dp-section"><summary>Every check with its log</summary>
            {(job.checks||[]).map((check,index)=><div className="dj-row" key={`${check.id}-${index}`}><strong>{check.passed?'✓':'✗'} {check.id}</strong><p>{check.classification|| (check.passed?'Passed':'Needs attention')}{check.test_count!=null&&` · ${check.test_count} tests`}</p><pre>{check.log_tail||check.error||check.summary||check.command}</pre>{evidence(check)}</div>)}</details>}
          {job.visual_review?.status&&<p>AI visual review: {job.visual_review.status} {job.visual_review.reason}</p>}
        </>}
        {tab==='Files & logs'&&<><h2>Files & logs</h2>{job.workspace?<ProjectEvidence jobId={job.id} t={t} font={font}/>:<p>Project files will appear after the first checkpoint.</p>}
          {(job.checks||[]).filter(c=>c.log).map((check,index)=><div className="dj-row" key={index}><p>{check.id}</p><CheckEvidence jobId={job.id} check={check} button={button}/></div>)}
          <details><summary>Model responses and recovery evidence</summary><pre>{JSON.stringify({plan:job.plan_text,patch:job.last_patch&&{...job.last_patch,source_hashes:undefined},recovery:job.verification_events,knowledge:job.knowledge_sources},null,2)}</pre></details>
        </>}
        {tab==='History'&&<><h2>History</h2>{job.project_id&&<ProjectHistory projectId={job.project_id} refreshKey={job.state} button={button}/>}
          {!!job.brief?.requirement_history?.length&&<details><summary>Requirement changes</summary>{job.brief.requirement_history.map(row=><p key={row.parent_id}><strong>{row.action}</strong>: {row.parent_outcome.text}{row.request_quote&&<> — “{row.request_quote}”</>}</p>)}</details>}
          {(job.scope_clarifications||[]).map((row,index)=><p key={`clarification-${index}`}>Clarification: {row.text}</p>)}
          {(job.stage_usage||[]).map(row=><p key={row.operation_id}>{row.stage} · {row.status} · {elapsedLabel(row.seconds)}</p>)}
          {(job.reviews||[]).map((review,index)=><details key={index}><summary>Review {index+1}</summary><pre>{JSON.stringify(review,null,2)}</pre></details>)}
          {(job.audit_history||job.check_history||[]).map((row,index)=><details key={index}><summary>Audit {index+1} · {row.revision_id?.slice(0,10)}</summary><pre>{JSON.stringify(row,null,2)}</pre></details>)}
        </>}
      </div>}
    </dialog>;}
}

export function DaedalusStatusStrip(props){return <DaedalusJobCard {...props} compact/>;}

function BrowserEvidence({jobId,check,button}){
  return <div>{(check.screenshots||[]).map(shot=><details key={shot.evidence.id}><summary>{shot.viewport} screenshot</summary><img loading="lazy" src={userScopedUrl(`/api/coder/workflows/${jobId}/evidence/${shot.evidence.id}`)} alt={`${shot.viewport} browser check`} style={{maxWidth:'100%'}}/></details>)}
    {(check.traces||[]).map(trace=><a key={trace.evidence.id} style={button} href={userScopedUrl(`/api/coder/workflows/${jobId}/evidence/${trace.evidence.id}`)} download={`${trace.viewport}-trace.zip`}>Download {trace.viewport} test trace</a>)}
    {!!check.traces?.length&&<p style={{fontSize:10}}>Replay locally with Playwright Trace Viewer: npx playwright show-trace &lt;trace.zip&gt;</p>}
  </div>;
}

function ProjectHistory({projectId,refreshKey,button}){
  const [rows,setRows]=useState(null),[error,setError]=useState('');
  useEffect(()=>{setRows(null);setError('');},[projectId,refreshKey]);
  const load=async()=>{try{const r=await fetch(`${API}/api/coder/projects/${projectId}/history`);if(!r.ok)throw new Error('Unable to load project history');setRows(await r.json());}catch(e){setError(e.message);}};
  return <details style={{fontSize:11,marginTop:8}} onToggle={e=>{if(e.currentTarget.open&&!rows)load();}}><summary>Project revisions</summary>
    {rows?.map(row=><p key={row.id} title={row.user_task}>{String(row.user_task||'').replace(/\s+/g,' ').slice(0,110)}{(row.user_task||'').length>110?'…':''} · {row.state} · {row.revision_id?.slice(0,10)} {row.artifact&&<a style={button} href={userScopedUrl(`/api/artifacts/${row.artifact.id}/download`)}>Download revision</a>}</p>)}{error&&<p role="alert">{error}</p>}
  </details>;
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
