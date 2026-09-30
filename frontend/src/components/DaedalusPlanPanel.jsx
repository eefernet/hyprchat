import React from 'react';
import {stageRail,loopLabel,planOutcomes,planSteps,checkSummary,outcomeExplanation} from '../daedalusProgress.js';
import './daedalusPlan.css';

// What the job is doing and why, read straight from the job snapshot. Icons always
// travel with text so state never depends on colour alone.
const STAGE_ICON={done:'✓',current:'●',stopped:'■',pending:'○'};
const OUTCOME={passed:['✓','Verified'],failed:['✗','Failed review'],unverified:['?','Not verified'],evidence:['◐','Evidence found'],pending:['○','Pending']};
const STEP_ICON={done:'✓',current:'●',partial:'◐',pending:'○'};

export function StageRail({job}){
  const loops=loopLabel(job);
  return <div className="dp-rail-wrap">
    <ol className="dp-rail" aria-label="Job stages">{stageRail(job).map(stage=>
      <li key={stage.label} className={`dp-stage dp-${stage.state}`} aria-current={stage.state==='current'?'step':undefined}>
        <span aria-hidden="true">{STAGE_ICON[stage.state]}</span> {stage.label}<span className="dp-sr"> ({stage.state})</span></li>)}</ol>
    {loops&&<p className="dp-loops">{loops}</p>}
  </div>;
}

export function ResultBanner({job,children}){
  const banner=outcomeExplanation(job);
  if(!banner)return null;
  return <div className={`dp-banner dp-tone-${banner.tone}`} role="status"><strong>{banner.title}</strong>{banner.text&&<p>{banner.text}</p>}{children}</div>;
}

export function PlanSection({job,defaultOpen=true}){
  const outcomes=planOutcomes(job),steps=planSteps(job);
  if(!outcomes.length&&!steps.length)return <p className="dp-muted">The plan appears here once planning finishes.</p>;
  const verified=outcomes.filter(row=>row.status==='passed').length;
  return <details className="dp-section" open={defaultOpen}>
    <summary>Plan — {outcomes.length} requested outcome{outcomes.length===1?'':'s'}{verified?` · ${verified} verified`:''}</summary>
    <ul className="dp-outcomes">{outcomes.map(row=>{const [icon,label]=OUTCOME[row.status]||OUTCOME.pending;return <li key={row.id} className={`dp-outcome dp-o-${row.status}`}>
      <span className="dp-icon" aria-hidden="true">{icon}</span>
      <div><span className="dp-text">{row.text}</span>
        <span className="dp-meta"><span className="dp-status">{label}</span>{row.kinds.map(kind=><span key={kind} className={`dp-chip${row.missing.includes(kind)?' dp-chip-missing':''}`}>{kind}{row.missing.includes(kind)?' — missing':''}</span>)}</span>
        {row.reason&&<span className="dp-reason">{row.reason}</span>}</div></li>;})}</ul>
    {steps.length>0&&<><h4 className="dp-subhead">Build steps</h4>
      <ol className="dp-steps">{steps.map(step=><li key={step.index} className={`dp-step dp-s-${step.state}`} aria-current={step.state==='current'?'step':undefined}>
        <span className="dp-icon" aria-hidden="true">{STEP_ICON[step.state]}</span>
        <div><span className="dp-text">{step.task}</span>
          {step.files.length>0&&<span className="dp-files">{step.files.map(file=><code key={file.path} className={file.written?'dp-file-done':''} title={file.written?'Written':'Not written yet'}>{file.written?'✓ ':''}{file.path}</code>)}</span>}</div></li>)}</ol></>}
  </details>;
}

export function ChecksSection({job,defaultOpen=true,renderEvidence}){
  const summary=checkSummary(job);
  if(!summary.rows.length)return null;
  const passed=summary.rows.filter(row=>row.passed);
  return <details className="dp-section" open={defaultOpen&&summary.failing.length>0}>
    <summary>Checks — {summary.passed} passed{summary.failed?` · ${summary.failed} failed`:''}{summary.advisory?` · ${summary.advisory} advisory`:''}</summary>
    <ul className="dp-checks">{summary.failing.map(row=><li key={row.id} className={row.advisory?'dp-check dp-advisory':'dp-check dp-failed'}>
      <span className="dp-icon" aria-hidden="true">{row.advisory?'!':'✗'}</span>
      <div><span className="dp-text">{row.label}{row.advisory?' (advisory — does not block acceptance)':''}</span>
        {row.command&&<code className="dp-command">{row.command}</code>}
        <span className="dp-reason">{row.headline}</span>
        {renderEvidence&&renderEvidence(row.check)}</div></li>)}</ul>
    {passed.length>0&&<p className="dp-muted">Passed: {[...passed.reduce((groups,row)=>{const key=row.label+(row.tests!=null?` (${row.tests} tests)`:'');return groups.set(key,(groups.get(key)||0)+1);},new Map())].map(([label,count])=>count>1?`${label} ×${count}`:label).join(' · ')}</p>}
  </details>;
}
