import React,{useEffect,useState} from 'react';
import {IC} from './icons.jsx';
import {terminalJobStates,jobBlockerMessage,jobStatusLabel,continueReason,elapsedLabel} from '../daedalusJobs.js';
import {loopLabel} from '../daedalusProgress.js';
import {liveActivity,stageTimings,counters,verdict,filesSummary} from '../daedalusActivity.js';
import {activityVisual} from '../chatActivityVisual.js';
import {useDaedalusJob} from '../daedalusJobStore.js';
import {openWorkbench,useDaedalusAutoOpen} from '../workbench.js';
import {StageRail,PlanSection,ChecksSection} from './DaedalusPlanPanel.jsx';
import {JobControls,JobDownloads,ClarificationField,resumableStates,jobButtonStyle,jobVars} from './DaedalusJobControls.jsx';
import {CheckEvidence,BrowserEvidence} from './DaedalusEvidence.jsx';
import DaedalusActivityFeed from './DaedalusActivityFeed.jsx';
import {ActivityLabel} from './ActivityLabel.jsx';
import './daedalusJob.css';

// The inline job card: one live line, the stage rail, the newest actions and
// folded sections. Everything deeper opens in the workbench pane.
export default function DaedalusJobCard({workflow,t,font,md,onOpenArtifact,compact=false}){
  const {job,connection,error,busy,action}=useDaedalusJob(workflow);
  const [now,setNow]=useState(Date.now());
  const [clarification,setClarification]=useState('');
  const active=!terminalJobStates.has(job.state);
  useEffect(()=>{if(!active)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[active]);
  useDaedalusAutoOpen(job);
  const button=jobButtonStyle(t,font),vars=jobVars(t,font);
  const name=job.presentation?.project_name||job.project_name||'Daedalus project';
  const status=jobStatusLabel(job),detail=job.presentation?.detail||'',loops=loopLabel(job);
  const label=detail?`${status} · ${detail}`:status;
  const connected=active&&connection==='connected'&&job.state!=='cancelling';
  const resumable=resumableStates.includes(job.state);
  const disabledReason=continueReason(job);
  const live=liveActivity(job,now);
  const result=verdict(job);
  const files=filesSummary(job);
  const handle={id:job.id,workflow_version:3,state:job.state,event_sequence:job.event_sequence};
  const openBench=tab=>openWorkbench({kind:'daedalus',job:handle,tab});
  const evidence=check=><><CheckEvidence jobId={job.id} check={check} button={button}/><BrowserEvidence jobId={job.id} check={check} button={button}/></>;
  const benchButton=<button style={button} onClick={()=>openBench()} title="Open this job in the side pane"><IC.Sidebar/> Workbench</button>;
  const controls=<JobControls job={job} busy={busy} action={action} button={button} clarification={clarification}/>;
  if(compact)return <section className="dj-card dj-strip" aria-label="Background coding job" style={vars}>
    <div className="dj-heading"><strong>{name}</strong>
      <span role="status" aria-live="polite" className="dj-state"><span aria-hidden="true" className={connected?'dj-dot dj-active':'dj-dot'}/>{label}{loops?` · ${loops}`:''}{live?<span className="dj-strip-live"> · {live.text}</span>:null}</span>
      <span className="dj-actions">{benchButton}{controls}</span></div>
    {error&&<p role="alert">{error}</p>}
  </section>;
  const visual=live?activityVisual({phase:live.phase,active:true},t,live.tool):null;
  const LiveIcon=visual?(IC[visual.icon]||IC.Activity):null;
  // An accepted job lists its advisory rows in the Checks fold, not as a headline.
  const failing=result&&(result.tone!=='good'||result.failing?.outcome==='failed')?result.failing:null;
  return <section className="dj-card" aria-label="Daedalus coding job" style={vars}>
    <div className="dj-heading">
      <span className="dj-title"><strong>{name}</strong><span role="status" aria-live="polite" className="dj-state"><span aria-hidden="true" className={connected?'dj-dot dj-active':'dj-dot'}/>{label}{loops?` · ${loops}`:''}</span></span>
      <span className="dj-actions">{controls}{benchButton}</span>
    </div>
    {live&&<p className="dj-live" role="status" aria-live="polite" style={visual.vars}>
      <span className="hc-activity-icon" aria-hidden="true"><span className="hc-activity-badge"><LiveIcon/></span></span>
      <span className="dj-live-text">{live.target?<>{live.label} <code>{live.target}</code></>:<ActivityLabel label={live.label} phase={live.phase} active seed={job.id}/>}{live.detail&&<span className="dp-muted"> · {live.detail}</span>}</span>
      {live.elapsed>=1&&<span className="dj-live-time">{elapsedLabel(live.elapsed)}</span>}
    </p>}
    {result&&<div className={`dj-verdict dp-tone-${result.tone}`} role="status"><strong>{result.title}.</strong> {result.text}</div>}
    <StageRail job={job} timings={stageTimings(job,now)}/>
    <p className="dp-facts">{counters(job,now).join(' · ')}</p>
    {failing&&<ul className="dp-checks dj-failing"><li className={failing.outcome==='failed'?'dp-check dp-failed':'dp-check dp-advisory'}>
      <span className="dp-icon" aria-hidden="true">{failing.outcome==='failed'?'✗':'?'}</span>
      <div><span className="dp-text">{failing.label}{failing.outcome!=='failed'?` (${failing.outcome})`:''}</span>
        {failing.command&&<code className="dp-command">{failing.command}</code>}
        <span className="dp-reason">{failing.headline}</span>{evidence(failing.check)}</div></li></ul>}
    {connection==='reconnecting'&&<p className="dj-notice">Reconnecting to progress. Last confirmed status is shown.</p>}
    {active&&job.blocker&&<p className="dj-notice">{jobBlockerMessage(job.blocker)}</p>}
    {resumable&&disabledReason&&!result&&<p className="dj-notice">{disabledReason}</p>}
    <ClarificationField job={job} button={button} value={clarification} onChange={setClarification}/>
    {job.answer&&<div className="dp-answer">{md?md(job.answer):job.answer}</div>}
    <DaedalusActivityFeed job={job} limit={3} onShowAll={()=>openBench('Activity')}/>
    <PlanSection job={job} defaultOpen={false}/>
    <ChecksSection job={job} defaultOpen={false} renderEvidence={evidence}/>
    {(files.changed>0||files.total>0)&&<details className="dp-section"><summary>Files — {files.changed?`${files.changed} changed`:`${files.total} files`}{files.last?<> · last <code>{files.last}</code></>:null}</summary>
      <ul className="dj-files">{(job.progress?.files||[]).slice(0,12).map(row=><li key={row.id}><span>{({create:'Created',edit:'Edited',delete:'Deleted'})[row.action]||'Changed'} <code>{row.path}</code></span><span>{row.outcome==='failed'?'Failed':row.outcome==='interrupted'?'Interrupted':''}</span></li>)}</ul>
      <button type="button" style={{...button,marginTop:8}} onClick={()=>openBench('Files')}>Browse files and logs</button>
    </details>}
    {job.presentation?.not_verified&&<details className="dp-section"><summary>Not verified by Daedalus</summary><p className="dp-muted">{job.presentation.not_verified}</p></details>}
    <div className="dj-actions" style={{marginTop:12,justifyContent:'flex-start'}}><JobDownloads job={job} busy={busy} action={action} button={button} t={t} onOpenArtifact={onOpenArtifact}/></div>
    {error&&<p role="alert">{error}</p>}
  </section>;
}

export function DaedalusStatusStrip(props){return <DaedalusJobCard {...props} compact/>;}
export {CheckEvidence} from './DaedalusEvidence.jsx';
