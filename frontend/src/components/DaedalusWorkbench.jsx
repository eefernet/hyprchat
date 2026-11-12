import React,{useEffect,useState} from 'react';
import {IC} from './icons.jsx';
import {userScopedUrl} from '../session.js';
import {useDaedalusJob} from '../daedalusJobStore.js';
import {terminalJobStates,jobStatusLabel,jobBlockerMessage,elapsedLabel} from '../daedalusJobs.js';
import {loopLabel} from '../daedalusProgress.js';
import {counters,stageTimings} from '../daedalusActivity.js';
import {checkOutcome} from '../daedalusCheckOutcomes.js';
import {DAEDALUS_TABS,setWorkbenchTab} from '../workbench.js';
import {StageRail,PlanSection,ChecksSection} from './DaedalusPlanPanel.jsx';
import {JobControls,JobDownloads,ClarificationField,ResumeVisualField,resumableStates,jobButtonStyle,jobVars} from './DaedalusJobControls.jsx';
import {BrowserEvidence,ProjectHistory,ProjectEvidence,CheckEvidence,FileActivity} from './DaedalusEvidence.jsx';
import DaedalusActivityFeed from './DaedalusActivityFeed.jsx';
import DaedalusPreview from './DaedalusPreview.jsx';
import './daedalusJob.css';
import './daedalusWorkbench.css';

const OUTCOME_LABEL={passed:'Passed',failed:'Failed',advisory:'Advisory',skipped:'Skipped',unverified:'Unverified'};

// The side-pane view of one Daedalus job: Activity · Plan · Checks · Files · Preview.
// `job` is a handle {id,...}; the live snapshot comes from the shared job store.
export default function DaedalusWorkbench({job:handle,tab,t,font,md,onOpenArtifact,onClose}){
  const {job,connection,error,busy,action}=useDaedalusJob(handle);
  const [clarification,setClarification]=useState(''),[resumeVisual,setResumeVisual]=useState('unchanged');
  const [visited,setVisited]=useState(()=>new Set([tab]));
  const [now,setNow]=useState(Date.now());
  const active=!terminalJobStates.has(job.state);
  useEffect(()=>{setVisited(previous=>previous.has(tab)?previous:new Set([...previous,tab]));},[tab]);
  useEffect(()=>{if(!active)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[active]);
  const button=jobButtonStyle(t,font),vars=jobVars(t,font);
  const name=job.presentation?.project_name||job.project_name||'Daedalus project';
  const status=jobStatusLabel(job),detail=job.presentation?.detail||'',loops=loopLabel(job);
  const label=detail?`${status} · ${detail}`:status;
  const artifact=job.artifact||job.candidate_artifact;
  const resumable=resumableStates.includes(job.state);
  const connected=active&&connection==='connected'&&job.state!=='cancelling';
  const evidence=check=><><CheckEvidence jobId={job.id} check={check} button={button}/><BrowserEvidence jobId={job.id} check={check} button={button}/></>;
  const shots=(job.checks||[]).flatMap(check=>(check.screenshots||[]).map(shot=>({check,shot})));
  const livePreview=job.engine==='openhands159'&&!!artifact;
  return <div className="dw-root" style={vars}>
    <header className="dw-head">
      <div className="dw-title"><strong>{name}</strong>
        <span role="status" aria-live="polite" className="dj-state"><span aria-hidden="true" className={connected?'dj-dot dj-active':'dj-dot'}/>{label}{loops?` · ${loops}`:''}</span></div>
      <button type="button" className="dw-close" onClick={onClose} aria-label="Close workbench"><IC.X/></button>
    </header>
    <nav className="dw-tabs" role="tablist" aria-label="Workbench sections">
      {DAEDALUS_TABS.map(item=><button key={item} type="button" role="tab" aria-selected={tab===item} onClick={()=>setWorkbenchTab(item)}>{item}</button>)}
    </nav>
    <div className="dw-body">
      {connection==='reconnecting'&&<p className="dj-notice">Reconnecting to progress. Last confirmed status is shown.</p>}
      {connection==='gone'&&<p className="dj-notice">{error||'This job no longer exists.'}</p>}
      {tab==='Activity'&&<section aria-label="Activity">
        <StageRail job={job} timings={stageTimings(job,now)}/>
        {active&&job.blocker&&<p className="dj-notice">{jobBlockerMessage(job.blocker)}</p>}
        {resumable&&<div className="dp-section" style={{borderTop:0,paddingTop:0}}>
          <ClarificationField job={job} button={button} value={clarification} onChange={setClarification}/>
          <ResumeVisualField job={job} button={button} value={resumeVisual} onChange={setResumeVisual}/>
        </div>}
        <DaedalusActivityFeed job={job} paged/>
        <details className="dp-section"><summary>Earlier and history</summary>
          {job.project_id&&<ProjectHistory projectId={job.project_id} refreshKey={job.state} button={button}/>}
          {!!job.brief?.requirement_history?.length&&<details><summary>Requirement changes</summary>{job.brief.requirement_history.map(row=><p key={row.parent_id}><strong>{row.action}</strong>: {row.parent_outcome?.text}{row.request_quote&&<> — “{row.request_quote}”</>}</p>)}</details>}
          {(job.scope_clarifications||[]).map((row,index)=><p key={`clarification-${index}`}>Clarification: {row.text}</p>)}
          {!!job.stage_usage?.length&&<details><summary>Stage usage</summary>{job.stage_usage.map(row=><p key={row.operation_id}>{row.stage} · {row.status} · {elapsedLabel(row.seconds)} · {row.calls} call{row.calls===1?'':'s'}</p>)}</details>}
          {(job.reviews||[]).map((review,index)=><details key={index}><summary>Review {index+1}</summary><pre>{JSON.stringify(review,null,2)}</pre></details>)}
          {(job.audit_history||job.check_history||[]).map((row,index)=><details key={index}><summary>Audit {index+1} · {row.revision_id?.slice(0,10)}</summary><pre>{JSON.stringify(row,null,2)}</pre></details>)}
        </details>
      </section>}
      {tab==='Plan'&&<section aria-label="Plan">
        <PlanSection job={job} defaultOpen/>
        {job.answer&&<div className="dp-answer">{md?md(job.answer):job.answer}</div>}
        <details className="dp-section dp-request"><summary>Original request</summary><div className="dp-request-body">{md?md(job.user_task||''):job.user_task}</div></details>
      </section>}
      {tab==='Checks'&&<section aria-label="Checks">
        {!job.checks?.length&&<p className="dw-empty">No checks have completed yet.</p>}
        {!!job.required_tests?.length&&<p className="dp-muted">{job.required_tests.length} named test{job.required_tests.length===1?'':'s'} passed earlier and must keep passing; a change that breaks, skips or removes one is rolled back.</p>}
        {!!job.superseded_tests?.length&&<details className="dp-section"><summary>Tests your request allowed to change ({job.superseded_tests.length})</summary><ul>{job.superseded_tests.map(item=><li key={item}><code>{item.split('::').slice(1).join('::')}</code></li>)}</ul></details>}
        <ChecksSection job={job} renderEvidence={evidence}/>
        {!!job.verification_summary?.criteria?.length&&<details className="dp-section"><summary>Independent verification of requirements</summary>
          <p>These observations cover the stated requirements at this revision. Other behavior still needs your review.</p>
          {job.verification_summary.criteria.map(criterion=><div className="dj-row" key={criterion.id}>
            <strong>{criterion.status==='passed'?'Verified':criterion.status==='failed'?'Failed':'Needs verification'}</strong>
            {criterion.supported&&criterion.action&&criterion.target&&<p>After {criterion.action.name}: {criterion.target.name||criterion.target.selector||criterion.target.kind} — {criterion.property==='value'?'input value':'displayed text'}.</p>}
            <p>{criterion.request_excerpt}</p>{criterion.reason&&<pre>{criterion.reason}</pre>}</div>)}
        </details>}
        {!!job.checks?.length&&<details className="dp-section"><summary>Every check with its log</summary>
          {(job.checks||[]).map((check,index)=><div className="dj-row" key={`${check.id}-${index}`}><strong>{checkOutcome(check)==='passed'?'✓':checkOutcome(check)==='failed'?'✗':'?'} {check.id}</strong><p>{OUTCOME_LABEL[checkOutcome(check)]}{check.classification&&` · ${check.classification}`}{check.test_count!=null&&` · ${check.test_count} tests`}</p><pre>{check.reason||check.log_tail||check.error||check.summary||check.command}</pre>{(check.lost_tests?.length>0||check.diagnostic)&&<details><summary>Diagnostic</summary><pre>{JSON.stringify({baseline_revision:check.baseline_revision,candidate_revision:check.revision_id,lost_tests:check.lost_tests,preparation:check.diagnostic},null,2)}</pre></details>}{evidence(check)}</div>)}</details>}
        {job.visual_review?.status&&<p>AI visual review: {job.visual_review.status} {job.visual_review.reason}</p>}
        {(job.blocker||job.failure)&&<details className="dp-section"><summary>Diagnostic</summary>{job.blocker&&<pre>{job.blocker}</pre>}{job.failure&&<pre>{JSON.stringify(job.failure,null,2)}</pre>}</details>}
      </section>}
      {tab==='Files'&&<section aria-label="Files and logs">
        {job.workspace?<ProjectEvidence jobId={job.id} changedAt={job.progress?.files_changed_at} t={t} font={font}/>:<p className="dw-empty">Project files will appear after the first checkpoint.</p>}
        <FileActivity job={job} defaultOpen/>
        {(job.checks||[]).filter(check=>check.log).map((check,index)=><div className="dj-row" key={index}><p>{check.id}</p><CheckEvidence jobId={job.id} check={check} button={button}/></div>)}
        {(job.candidate_artifact?.metadata?.run_instructions||[]).map(profile=><div key={profile.cwd}><h3>Run from {profile.cwd}</h3><pre>{[...(profile.commands?.setup||[]),...(profile.commands?.build||[]),...(profile.commands?.test||[]),profile.launch?.replaceAll('{port}','8000')].filter(Boolean).join('\n')}</pre></div>)}
        <details className="dp-section"><summary>Model responses and recovery evidence</summary><pre>{JSON.stringify({plan:job.plan_text,patch:job.last_patch&&{...job.last_patch,source_hashes:undefined},recovery:job.verification_events,knowledge:job.knowledge_sources},null,2)}</pre></details>
        <p className="dp-muted">Revision <code>{job.revision_id?job.revision_id.slice(0,12):'none yet'}</code></p>
      </section>}
      {/* Stays mounted once visited: unmounting DaedalusPreview deletes its application session. */}
      {visited.has('Preview')&&<section aria-label="Preview" hidden={tab!=='Preview'}>
        {livePreview&&<DaedalusPreview job={job} button={button}/>}
        {shots.length>0&&<><h3>Browser screenshots</h3><div className="dw-shots">{shots.map(({check,shot})=><figure key={shot.evidence.id}><img loading="lazy" src={userScopedUrl(`/api/coder/workflows/${job.id}/evidence/${shot.evidence.id}`)} alt={`${shot.viewport} screenshot from ${check.id}`}/><figcaption>{check.id} · {shot.viewport}</figcaption></figure>)}</div></>}
        {job.progress?.ui_preflight&&<p className="dp-muted">UI preflight: {job.progress.ui_preflight.status}{job.progress.ui_preflight.reason?` — ${job.progress.ui_preflight.reason}`:''}</p>}
        {!livePreview&&!shots.length&&<p className="dw-empty">{active?'A preview appears once a revision has been checked.':'No preview is available for this revision.'}</p>}
      </section>}
    </div>
    <footer className="dw-foot">
      <span>{counters(job,now).join(' · ')}{job.context?` · ${Number(job.context.num_ctx).toLocaleString()} context`:''}</span>
      <span className="dw-actions">
        <JobDownloads job={job} busy={busy} action={action} button={button} t={t} onOpenArtifact={onOpenArtifact} artifactDetails/>
        <JobControls job={job} busy={busy} action={action} button={button} clarification={clarification} resumeVisual={resumeVisual}/>
      </span>
    </footer>
    {error&&connection!=='gone'&&<p role="alert" className="dw-empty">{error}</p>}
  </div>;
}
