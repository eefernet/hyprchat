import React from 'react';
import {userScopedUrl} from '../session.js';
import {terminalJobStates,continueReason,needsMoreAllowance} from '../daedalusJobs.js';

// Buttons shared by the inline card, the composer strip and the workbench so
// Stop/Continue/Accept/Download behave identically everywhere.
export const resumableStates=['blocked','waiting_for_input','ready_for_review','cancelled'];
export function jobButtonStyle(t,font){
  return {background:t.bgDeep,border:`1px solid ${t.brd}`,borderRadius:"var(--hc-radius)",padding:'8px 12px',color:t.text,fontFamily:font,fontSize:"var(--hc-text-control)",cursor:'pointer',textDecoration:'none',display:'inline-block'};
}
export function jobVars(t,font){
  return {'--dj-surface':`var(--hc-elev-1, ${t.surface})`,'--dj-bg':t.bgDeep,'--dj-text':t.text,'--dj-muted':t.dim||t.mut,'--dj-border':`var(--hc-line, ${t.brd})`,'--dj-accent':t.acc,'--dj-good':t.ok,fontFamily:font};
}

export function JobControls({job,busy,action,button,clarification='',resumeVisual='unchanged'}){
  const active=!terminalJobStates.has(job.state);
  const resumable=resumableStates.includes(job.state);
  const disabledReason=continueReason(job);
  const accept=job.presentation?.actions?.accept;
  const needsClarification=!!job.scope_question&&!clarification.trim();
  return <>
    {active?<button style={button} disabled={busy||job.state==='cancelling'} onClick={()=>action('cancel')}>{job.state==='cancelling'?'Stopping…':'Stop'}</button>
      :resumable&&!(accept?.enabled&&disabledReason)?<button style={button} disabled={busy||!!disabledReason||needsClarification} title={disabledReason} onClick={()=>action('resume',resumeVisual,clarification)}>{job.engine==='openhands159'?'Resume':'Continue'}</button>:null}
    {!active&&resumable&&job.engine==='openhands159'&&needsMoreAllowance(job)&&!disabledReason&&<button style={button} disabled={busy||needsClarification} onClick={()=>action('resume',resumeVisual,clarification,'new_allowance')}>Continue with more allowance</button>}
  </>;
}

export function JobDownloads({job,busy,action,button,t,onOpenArtifact,artifactDetails=false}){
  const artifact=job.artifact||job.candidate_artifact;
  const resumable=resumableStates.includes(job.state);
  const accept=job.presentation?.actions?.accept;
  return <>
    {artifact&&<a style={button} href={userScopedUrl(`/api/artifacts/${artifact.id}/download`)} download title={artifact.metadata?.download_name?`Saves as ${artifact.metadata.download_name}`:undefined}>{job.artifact?'Download accepted project':'Download candidate'}</a>}
    {!artifact&&resumable&&job.revision_id&&<a style={button} href={userScopedUrl(`/api/coder/workflows/${job.id}/checkpoint`)} download>Download checkpoint</a>}
    {accept?.enabled&&job.project_id&&<button style={{...button,borderColor:t.acc,color:t.acc}} disabled={busy} title="Mark this exact revision as the project's accepted version" onClick={()=>action('accept')}>Accept this build</button>}
    {artifactDetails&&artifact&&onOpenArtifact&&<button style={button} onClick={()=>onOpenArtifact(artifact.id)}>Artifact details</button>}
  </>;
}

export function ClarificationField({job,button,value,onChange}){
  if(!resumableStates.includes(job.state)||!job.scope_question)return null;
  return <label style={{display:'block'}}>Clarify this change: {job.scope_question}
    <textarea aria-label="Scope clarification" value={value} maxLength={10000} onChange={e=>onChange(e.target.value)} style={{...button,display:'block',width:'100%',boxSizing:'border-box',minHeight:85,marginTop:8}}/></label>;
}

export function ResumeVisualField({job,button,value,onChange}){
  if(!resumableStates.includes(job.state))return null;
  const reason=continueReason(job);
  return <div>
    <label>Visual review when continuing <select value={value} onChange={e=>onChange(e.target.value)} style={button}>
      <option value="unchanged">Keep setting</option><option value="on">Enable</option><option value="off">Disable</option></select></label>
    <p className="dp-muted">{reason||'Continue uses this exact saved candidate revision.'}</p>
  </div>;
}
