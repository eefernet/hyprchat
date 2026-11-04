// Pure view-model helpers for the Daedalus job card. Everything here reads the job
// snapshot the card already receives; every field is optional so older workflows
// (policy-6 briefs without batches, legacy cancelled jobs) render without throwing.
import {checkOutcome,currentChecks,onlyInconclusiveChecks} from './daedalusCheckOutcomes.js';
import {jobBlockerMessage,reviewableCandidate} from './daedalusJobs.js';

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
  if(job.engine==='openhands159'){
    const stopped=['cancelled','blocked','waiting_for_input'].includes(state);
    const stage=stopped?(job.failure?.stage||job.resume_state||job.operation_kind):state;
    const indices={queued:0,inspecting:0,planning:0,coding:1,code:1,baselining:2,checking:2,check:2,candidate_packaging:3,packaging:3,package:3,ready_for_review:4,completed:4};
    let reached=indices[stage]??0;
    if(state==='coding'&&job.progress?.current?.kind==='plan')reached=0;
    const rail=['Plan','Build','Check','Deliver'].map((label,index)=>({label,
      state:index<reached?'done':index===reached?(stopped?'stopped':'current'):'pending'}));
    const summary=job.verification_summary;
    const currentEvidence=summary?.revision_id && summary.revision_id===job.revision_id;
    const terminal=['ready_for_review','completed','blocked','waiting_for_input','cancelled'].includes(state);
    if(reached>=2||terminal){
      if(job.failure?.category==='environment')rail[2].state='blocked';
      else if(state==='checking')rail[2].state='current';
      else if(currentEvidence){
        rail[2].state=(summary.failures||[]).length?'failed':summary.checks_passed&&!onlyInconclusiveChecks(job)?'done':'unverified';
      } else rail[2].state=stopped&&reached===2?'stopped':'unverified';
    }
    if(job.candidate_artifact||job.artifact)rail[3].state='delivered';
    else if(terminal)rail[3].state='pending';
    return rail;
  }
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
  // Optional: the lowest verification layer still failing (foundation, core behavior, interface integration, delivery).
  const phase=job.stage_progress;
  if(phase&&phase.of&&!['completed','cancelled'].includes(job.state))parts.push(`Phase ${phase.phase}/${phase.of} · ${phase.label||''}`.trim());
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
  if(job.progress?.version===1)return list(job.progress.plan).map((step,index)=>({index:index+1,task:step.title,files:[],
    state:step.status==='done'?'done':job.state==='coding'?({todo:'pending',in_progress:'current'})[step.status]||'pending':'not_updated'}));
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
  const rows=currentChecks(job).map(check=>{
    const outcome=checkOutcome(check,job.engine!=='openhands159'),advisory=outcome==='advisory';
    const label=check.id==='audit-metadata'?'Independent audit':check.id.startsWith('audit-')?'Independent audit check':
      check.id.startsWith('protected:')?`Unchanged: ${check.id.slice(10)}`:check.id.startsWith('interface:')?`Requested ${check.id.slice(10)}`:
      check.id.startsWith('page:')?'Page loads without script errors':check.id.startsWith('documentation:')?'Requested documentation present':check.id==='immutable-source'?'Checks must not edit source':check.id==='execution-contract'?'Project run configuration':
      `${CHECK_LABEL[check.phase]||check.id}${scope(check)?` (${scope(check)})`:''}`;
    return {id:check.id,label,command:check.command||'',passed:outcome==='passed',advisory,outcome,tests:check.test_count??null,
      headline:outcome==='passed'?'':headline(check),check};
  });
  const failed=rows.filter(row=>row.outcome==='failed');
  return {rows,passed:rows.filter(row=>row.passed).length,failed:failed.length,advisory:rows.filter(row=>row.advisory).length,
    skipped:rows.filter(row=>row.outcome==='skipped').length,unverified:rows.filter(row=>row.outcome==='unverified').length,
    failing:[...failed,...rows.filter(row=>!row.passed&&row.outcome!=='failed')]};
}

const LIMIT_TEXT={input_budget:'the builder prompt did not fit the configured input budget, so the model never ran (raise the Daedalus context window in Settings, then Continue)',application_repairs:'both automatic repair rounds were used',
  audit_corrections:'the independent audit could not be completed after two corrections',
  no_progress:'the editor stopped making changes',
  model_calls:'the model-call allowance was used up before verification finished (Continue grants another)'};

export function outcomeExplanation(job={}){
  const state=job.state;
  const outcomes=planOutcomes(job),checks=checkSummary(job);
  const open=outcomes.filter(row=>row.status!=='passed');
  const openText=open.slice(0,4).map(row=>`${row.id} ${row.status==='failed'?'failed review':row.missing.length?`needs ${row.missing.join(' + ')} evidence`:'not verified'}`).join('; ');
  const kind=job.accepted_by||job.acceptance?.kind||job.artifact?.metadata?.accepted_by||'';
  if(state==='completed'&&job.artifact&&kind==='user')return {tone:'good',title:'Complete',
    text:'You accepted this exact revision. Later edits start from the latest build; this version stays downloadable.'};
  if(state==='completed'&&job.artifact&&kind==='examples'){
    const count=job.verification_summary?.confirmed_examples||0;
    return {tone:'good',title:'Complete',text:`Every acceptance example you confirmed passed (${count}), along with the build and its tests. They cover only those cases.`};
  }
  if(state==='completed')return job.artifact?{tone:'good',title:'Accepted',
      text:`Every requested outcome was verified by executed checks${outcomes.length?` (${outcomes.length} outcome${outcomes.length===1?'':'s'}, ${checks.passed} checks passed)`:''}.`}:
    {tone:'neutral',title:job.answer?'Answered':'Finished',text:job.answer?'':'The job finished without publishing a revision.'};
  const summary=job.verification_summary||{};
  const uiGap=job.ui_contract_version&&(summary.criteria||[]).some(c=>c.status!=='passed');
  if(state==='ready_for_review'&&summary.rolled_back)return {tone:'warn',title:'Change rolled back',
    text:clip(job.blocker,420)||'A change broke tests that passed before, so the last version that kept them passing was restored for review.'};
  if(state==='ready_for_review'&&!summary.incomplete_reason&&!uiGap&&!onlyInconclusiveChecks(job)&&(summary.checks_passed||job.candidate_artifact?.metadata?.checks_passed))return {tone:'good',title:'Checks passed — please review',
    text:`The build${checks.passed?` and ${checks.passed} check${checks.passed===1?'':'s'}`:''} passed on this revision. Download it and try it, then Accept it or ask for changes in the chat.`};
  if(state==='ready_for_review'&&summary.checks_ok&&summary.exercised===false)return {tone:'warn',title:'Built, not exercised',
    text:'It builds and nothing failed, but no test or requested command ran the program. Try it yourself before you Accept it, or ask for tests in the chat.'};
  if(state==='ready_for_review'){
    const why=LIMIT_TEXT[job.stop_limit]||clip(job.blocker,180)||'not every outcome could be verified';
    return {tone:'warn',title:reviewableCandidate(job)?'Ready for your review':'Build incomplete',
      text:`Not auto-accepted: ${why}. The saved candidate is yours to download and check.`+
        (checks.failed?` ${checks.failed} check${checks.failed===1?'':'s'} still failing.`:'')+(openText?` Still open: ${openText}.`:'')};
  }
  if(state==='cancelled')return {tone:'neutral',title:'Stopped',
    text:`Work stopped at a saved checkpoint${job.inventory?.files?` with ${job.inventory.files} file${job.inventory.files===1?'':'s'}`:''}.`};
  if(['blocked','waiting_for_input'].includes(state)&&job.failure?.category==='environment')return {tone:'warn',title:'Dependency setup needs attention',text:'Dependency setup failed; your project files are saved. See Diagnostic for the cause.'};
  if(state==='blocked'||state==='waiting_for_input')return {tone:'warn',title:'Needs attention',text:clip(jobBlockerMessage(job.blocker),260)||'The job cannot continue on its own.'};
  return null;
}

export function nowLine(job={}){
  if(job.progress?.version===1){
    const activity=job.progress.current;
    if(activity)return ({preparation:'Preparing dependencies',command:'Running a command',interface:'Checking the interface',ui_preflight:'Checking UI flows',file:'Updating project files',plan:'Updating the implementation plan',tool:'Inspecting the project'})[activity.kind]||'Working on the project';
    if(job.state==='coding')return 'Preparing the next model action';
  }
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

export function fileActivity(job={}){
  const active=!['blocked','waiting_for_input','ready_for_review','completed','cancelled','cancelling'].includes(job.state);
  const current=job.progress?.current;
  if(active&&current?.kind==='file'&&current.path)return {label:current.action==='create'?'Creating':'Editing',path:current.path};
  const last=(job.progress?.files||[]).find(row=>row.outcome!=='failed');
  return last?{label:'Last updated:',path:last.path}:{label:'No file changes yet',path:''};
}
