// Pure view-model helpers for the Daedalus job card. Everything here reads the job
// snapshot the card already receives; every field is optional so older workflows
// (policy-6 briefs without batches, legacy cancelled jobs) render without throwing.

const STAGES=['Plan','Build','Check','Audit','Review','Deliver'];
const STAGE_OF={queued:0,inspecting:0,baselining:0,planning:0,coding:1,checking:2,auditing:3,reviewing:3,
  accepting:4,visual_review:4,packaging:5,candidate_packaging:5};
const MAX_REPAIRS=2,MAX_AUDIT_CORRECTIONS=2,MAX_BUILD_PASSES=6;
const KEY_ERROR=/\berror\b|Error:|\bFAILED\b|Assertion|Segmentation fault|panicked|undefined reference|cannot find|expected .* found|SyntaxError|Traceback|No such file|not found/;
const NOISE=/^g?make(\[\d+\])?:|npm error A complete log|^\[.*INFO.*\]|^\s*at\s|^\s*File "/;

const list=value=>Array.isArray(value)?value:[];
const clip=(value,size=160)=>{const text=String(value||'').replace(/\x1b\[[0-9;]*m/g,'').replace(/\s+/g,' ').trim();return text.length>size?`${text.slice(0,size-1)}…`:text;};

export function stageRail(job={}){
  const state=job.state||'queued';
  const accepted=state==='completed'&&!!job.artifact;
  const stopped=['cancelled','blocked','waiting_for_input','ready_for_review'].includes(state);
  // A finished or stopped job shows how far it got, judged by what it produced.
  let reached=STAGE_OF[state];
  if(reached===undefined){
    reached=accepted?6:job.review||list(job.reviews).length?4:list(job.audit_history).length?3:list(job.checks).length?2:
      job.last_patch||job.revision_id&&job.brief?1:job.brief?1:0;
  }
  return STAGES.map((label,index)=>({label,
    state:accepted||index<reached?'done':index===reached?(stopped?'stopped':'current'):'pending'}));
}

export function loopLabel(job={}){
  const parts=[];
  if(job.state==='coding'){
    const narrowed=list(job.narrowed_targets);
    if(narrowed.length)parts.push(`Editing ${narrowed[0]}${narrowed.length>1?` (${narrowed.length} files queued)`:''}`);
    else if(job.builder==='sdk')parts.push(`Agent builder · pass ${Math.min((job.build_continuations||0)+1,MAX_BUILD_PASSES)} of ${MAX_BUILD_PASSES}`);
  }
  if(job.repair_round>0)parts.push(`Repair ${Math.min(job.repair_round,MAX_REPAIRS)} of ${MAX_REPAIRS}`);
  if(job.audit_corrections>0)parts.push(`Audit correction ${Math.min(job.audit_corrections,MAX_AUDIT_CORRECTIONS)} of ${MAX_AUDIT_CORRECTIONS}`);
  return parts.join(' · ');
}

const KIND_LABEL={behavior:'behavior',documentation:'docs',tests:'tests',preservation:'unchanged files',visual:'visual'};

export function planOutcomes(job={}){
  const judged=new Map(list(job.verification_summary?.outcomes).map(row=>[row.id,row]));
  const checks=list(job.checks);
  return list(job.brief?.outcomes).map((outcome,index)=>{
    const verdict=judged.get(outcome.id)||{};
    const kinds=list(verdict.evidence_types||outcome.evidence_types);
    const hasEvidence=checks.some(check=>check.passed&&(list(check.outcomes).includes(outcome.id)||check.repair_demonstrated));
    const status=verdict.status||(hasEvidence?'evidence':'pending');
    return {id:outcome.id||`o${index+1}`,text:outcome.text||String(outcome),kinds:kinds.map(kind=>KIND_LABEL[kind]||kind),status,
      missing:list(verdict.missing_evidence).map(kind=>KIND_LABEL[kind]||kind),
      // A reviewer can be satisfied while the controller still lacks executed evidence; say which.
      reason:status==='failed'?clip(verdict.reason,220):status==='unverified'?(list(verdict.missing_evidence).length?
        `No ${list(verdict.missing_evidence).map(kind=>KIND_LABEL[kind]||kind).join(' or ')} evidence was recorded for this outcome.`:clip(verdict.reason,220)):''};
  });
}

export function planSteps(job={}){
  const batches=list(job.brief?.batches);
  const written=new Set([...Object.keys(job.last_patch?.source_hashes||{}),...list(job.last_patch?.changed)]);
  const current=job.batch||0,active=job.state==='coding',pastBuild=(STAGE_OF[job.state]??(job.checks?.length?2:0))>1;
  // The agent builder works on the whole brief at once, so steps are judged by their files.
  const wholeBrief=job.builder==='sdk';
  let claimed=false;
  return batches.map((batch,index)=>{
    const files=list(batch.files).map(path=>({path,written:written.has(path)}));
    const allWritten=files.length>0&&files.every(file=>file.written);
    const started=files.some(file=>file.written);
    let state=allWritten||pastBuild&&!files.length?'done':started?'partial':'pending';
    // Only one step is "current": the first unfinished one for the agent builder, the batch index for Aider.
    if(state!=='done'&&active&&!claimed&&(wholeBrief||index===current)){state='current';claimed=true;}
    else if(!wholeBrief&&index<current&&state!=='done')state='done';
    return {index:index+1,task:batch.task||'',files,state};
  });
}

function headline(check){
  const lines=String(check.log_tail||'').replace(/\x1b\[[0-9;]*m/g,'').split('\n').map(line=>line.trim()).filter(Boolean);
  const decisive=lines.filter(line=>KEY_ERROR.test(line)&&!NOISE.test(line));
  return clip(decisive[decisive.length-1]||check.reason||lines[lines.length-1]||check.classification||'Failed',200);
}

// Setup rows carry no cwd; their id is "<package>:<phase>:<n>".
const scope=check=>{const cwd=check.cwd||String(check.id||'').split(':')[0];return cwd&&cwd!=='.'&&!/^(audit|protected|interface|launch)/.test(cwd)?cwd:'';};
const CHECK_LABEL={setup:'Install dependencies',build:'Build',lint:'Lint',typecheck:'Type check',test:'Tests',launch:'Start the app'};

export function checkSummary(job={}){
  // Checks are appended per round; the latest row for an id is the current truth.
  const latest=new Map();
  list(job.checks).forEach(check=>{if(check&&check.id)latest.set(check.id,check);});
  const rows=[...latest.values()].map(check=>{
    const advisory=!check.passed&&['lint','typecheck'].includes(check.phase)&&!check.is_test;
    const label=check.id==='audit-metadata'?'Independent audit':check.id.startsWith('audit-')?'Independent audit check':
      check.id.startsWith('protected:')?`Unchanged: ${check.id.slice(10)}`:check.id.startsWith('interface:')?`Requested ${check.id.slice(10)}`:
      check.id.startsWith('page:')?'Page loads without script errors':check.id==='immutable-source'?'Checks must not edit source':check.id==='execution-contract'?'Project run configuration':
      `${CHECK_LABEL[check.phase]||check.id}${scope(check)?` (${scope(check)})`:''}`;
    return {id:check.id,label,command:check.command||'',passed:!!check.passed,advisory,tests:check.test_count??null,
      headline:check.passed?'':headline(check),check};
  });
  const failed=rows.filter(row=>!row.passed&&!row.advisory);
  return {rows,passed:rows.filter(row=>row.passed).length,failed:failed.length,advisory:rows.filter(row=>row.advisory).length,
    failing:[...failed,...rows.filter(row=>row.advisory)]};
}

const LIMIT_TEXT={application_repairs:'both automatic repair rounds were used',
  audit_corrections:'the independent audit could not be completed after two corrections',
  no_progress:'the editor stopped making changes',
  model_calls:'the model-call allowance was used up before verification finished (Continue grants another)'};

export function outcomeExplanation(job={}){
  const state=job.state;
  const outcomes=planOutcomes(job),checks=checkSummary(job);
  const open=outcomes.filter(row=>row.status!=='passed');
  const openText=open.slice(0,4).map(row=>`${row.id} ${row.status==='failed'?'failed review':row.missing.length?`needs ${row.missing.join(' + ')} evidence`:'not verified'}`).join('; ');
  if(state==='completed')return job.artifact?{tone:'good',title:'Accepted',
      text:`Every requested outcome was verified by executed checks${outcomes.length?` (${outcomes.length} outcome${outcomes.length===1?'':'s'}, ${checks.passed} checks passed)`:''}.`}:
    {tone:'neutral',title:job.answer?'Answered':'Finished',text:job.answer?'':'The job finished without publishing a revision.'};
  if(state==='ready_for_review'){
    const why=LIMIT_TEXT[job.stop_limit]||clip(job.blocker,180)||'not every outcome could be verified';
    return {tone:'warn',title:job.candidate_artifact?.metadata?.runnable?'Ready for your review':'Build incomplete',
      text:`Not auto-accepted: ${why}. The saved candidate is yours to download and check.`+
        (checks.failed?` ${checks.failed} check${checks.failed===1?'':'s'} still failing.`:'')+(openText?` Still open: ${openText}.`:'')};
  }
  if(state==='cancelled')return {tone:'neutral',title:'Stopped',
    text:`Work stopped at a saved checkpoint${job.inventory?.files?` with ${job.inventory.files} file${job.inventory.files===1?'':'s'}`:''}.`};
  if(state==='blocked'||state==='waiting_for_input')return {tone:'warn',title:'Needs attention',text:clip(job.blocker,260)||'The job cannot continue on its own.'};
  return null;
}

export function nowLine(job={}){
  if(job.state==='coding'){
    const narrowed=list(job.narrowed_targets);
    if(narrowed.length)return `Editing ${narrowed[0]}`;
    const steps=planSteps(job),current=steps.find(step=>step.state==='current')||steps.find(step=>step.state!=='done');
    const pending=current?.files.filter(file=>!file.written).map(file=>file.path)||[];
    if(pending.length)return `Writing ${pending.slice(0,3).join(', ')}${pending.length>3?` +${pending.length-3} more`:''}`;
    return clip(current?.task||'Creating project files',140);
  }
  return '';
}

export function requestSummary(task,size=200){return clip(String(task||'').replace(/[*_`#>]+/g,' '),size);}
