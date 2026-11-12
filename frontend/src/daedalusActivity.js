// Pure view-model for the compact Daedalus card and the workbench activity feed.
// Reads only the job snapshot; every field is optional so older jobs (no
// progress.recent, no verb on progress.current) render without throwing.
import {nowLine,checkSummary,outcomeExplanation} from './daedalusProgress.js';
import {jobActivity,elapsedLabel,timestamp,terminalJobStates} from './daedalusJobs.js';

const list=value=>Array.isArray(value)?value:[];
// Rail segment per worker stage (stage_usage.stage / operation_kind) with a job-state fallback.
export const STAGE_INDEX={inspect:0,inspecting:0,queued:0,planning:0,plan:0,qa:0,answering:0,
  code:1,coding:1,baseline:2,baselining:2,check:2,checking:2,package:3,packaging:3,candidate_packaging:3};

// Present tense for the live line, past tense for the feed.
const LIVE={run:'Running',check:'Checking',create:'Creating',edit:'Writing',undo:'Reverting',read:'Reading',delete:'Deleting',
  plan:'Updating the plan',prepare:'Preparing dependencies',check_ui:'Checking UI flows',inspect:'Inspecting the project',think:'Thinking'};
const PLAIN={run:'Running a command',check:'Running a check',create:'Creating project files',edit:'Updating project files',undo:'Reverting an edit',read:'Reading project files',delete:'Deleting project files'};
const DONE={run:'Ran',check:'Check',create:'Created',edit:'Edited',undo:'Reverted',read:'Read',delete:'Deleted',
  plan:'Updated the plan',prepare:'Prepared dependencies',check_ui:'Checked UI flows',inspect:'Inspected the project',think:'Thought'};
const TARGETED=new Set(['run','check','create','edit','undo','read','delete']);
// Phase/tool pairs feed activityVisual() so the card shares the chat pill's icons and colours.
const VISUAL={run:{phase:'tool',tool:'run_shell'},check:{phase:'reviewing',tool:'run_shell'},create:{phase:'writing',tool:'write_file'},
  edit:{phase:'writing',tool:'write_file'},undo:{phase:'writing',tool:'write_file'},delete:{phase:'writing',tool:'write_file'},
  read:{phase:'tool',tool:'read_file'},plan:{phase:'thinking',tool:''},prepare:{phase:'starting',tool:'install_package'},
  check_ui:{phase:'reviewing',tool:'fetch_url'},inspect:{phase:'tool',tool:'list_files'},think:{phase:'thinking',tool:''}};
export const ACTIVITY_ICON={run:'Terminal',check:'Terminal',create:'Pencil',edit:'Pencil',undo:'Refresh',read:'File',delete:'Trash',
  plan:'Layers',prepare:'Cube',check_ui:'Globe',inspect:'Search',think:'Brain'};

export function callCount(job={}){return (job.calls_used||0)+(job.operation_calls||0);}

export function jobElapsed(job={},now=Date.now()){
  const active=!terminalJobStates.has(job.state);
  const contact=timestamp(job.worker_contact_at);
  const live=active?Math.max(job.operation_seconds||0,Number.isFinite(contact)?(now-contact)/1000+(job.operation_seconds||0):0):0;
  return (job.seconds_used||0)+live;
}

// What the agent is doing right now, or null once the job has stopped.
export function liveActivity(job={},now=Date.now()){
  if(terminalJobStates.has(job.state))return null;
  const current=job.progress?.current;
  const verb=current?.verb||'';
  const started=current?timestamp(current.started_at):NaN;
  const elapsed=Number.isFinite(started)?Math.max(0,(now-started)/1000):0;
  if(verb&&VISUAL[verb]){
    const target=TARGETED.has(verb)?String(current.command||current.path||''):'';
    const label=target?LIVE[verb]:PLAIN[verb]||LIVE[verb];
    const detail=verb==='think'?`model call ${callCount(job)}`:'';
    const text=`${label}${target?` ${target}`:''}${detail?` · ${detail}`:''}`;
    return {label,target,detail,text,verb,elapsed,...VISUAL[verb]};
  }
  const text=nowLine(job)||jobActivity(job)||'Working on the project';
  return {label:text,target:'',detail:'',text,verb:'',elapsed,phase:current?'tool':'thinking',tool:''};
}

// Newest first. Pairs tool_call rows with their tool_result by ref; falls back to
// the file records older snapshots carry.
export function activityRows(job={}){
  const recent=list(job.progress?.recent);
  return rowsFromRecords(recent.length?recent:list(job.progress?.files),{active:!terminalJobStates.has(job.state),currentId:job.progress?.current?.id});
}
// Activity records (snapshot recent[] or a paged /events?view=activity batch) to feed rows.
// tool_call rows win; file records only stand in for jobs recorded before tool calls existed.
export function rowsFromRecords(records,{active=false,currentId}={}){
  const rows=list(records).filter(Boolean);
  const calls=rows.filter(row=>row.kind==='tool_call');
  if(calls.length){
    const results=new Map(rows.filter(row=>row.kind==='tool_result'&&row.ref).map(row=>[row.ref,row]));
    return calls.map(row=>{
      const result=results.get(row.id);
      const running=!result&&active&&currentId===row.id;
      return {id:row.id,at:row.at,stage:row.stage||'',tool:row.tool||'',verb:VISUAL[row.verb]?row.verb:'inspect',
        target:String(row.command||row.path||''),outcome:result?result.outcome:running?'running':'unknown',
        exitCode:result?.exit_code??null,running};
    });
  }
  return rows.filter(row=>row.kind==='file'&&row.path).map(row=>({id:row.id,at:row.at,stage:'',tool:'file_editor',
    verb:row.action==='create'?'create':row.action==='delete'?'delete':'edit',target:String(row.path),
    outcome:row.outcome||'unknown',exitCode:null,running:false}));
}

export function activityText(row={}){
  const verb=VISUAL[row.verb]?row.verb:'inspect';
  const label=row.running?(row.target?LIVE[verb]:PLAIN[verb]||LIVE[verb]):DONE[verb];
  const note=row.running?'':row.outcome==='failed'?(['run','check'].includes(verb)&&row.exitCode!=null?`exit ${row.exitCode}`:'failed'):
    row.outcome==='interrupted'?'interrupted':verb==='check'&&row.outcome==='succeeded'?'passed':'';
  return {label,target:TARGETED.has(verb)?row.target:'',note};
}
export function activityIcon(row={}){return ACTIVITY_ICON[row.verb]||'Activity';}

// Seconds spent per rail segment (Plan, Build, Check, Deliver); undefined for legacy rails.
export function stageTimings(job={},now=Date.now()){
  if(job.engine!=='openhands159')return undefined;
  const totals=[0,0,0,0];
  for(const row of list(job.stage_usage)){const index=STAGE_INDEX[row?.stage];if(index!=null)totals[index]+=Number(row.seconds)||0;}
  if(!terminalJobStates.has(job.state)&&!job.operation_accounted){
    const index=STAGE_INDEX[job.operation_kind]??STAGE_INDEX[job.state];
    if(index!=null){
      const contact=timestamp(job.worker_contact_at);
      totals[index]+=(job.operation_seconds||0)+(Number.isFinite(contact)?Math.max(0,(now-contact)/1000):0);
    }
  }
  return totals.map(seconds=>({seconds:Math.round(seconds)}));
}

export function counters(job={},now=Date.now()){
  const calls=callCount(job);
  return [job.model,`${elapsedLabel(jobElapsed(job,now))} elapsed`,
    job.call_limit?`${calls} of ${job.call_limit} model calls`:`${calls} model call${calls===1?'':'s'}`,
    job.inventory?.files?`${job.inventory.files} files`:'',job.revision_id?`revision ${job.revision_id.slice(0,8)}`:''].filter(Boolean);
}

export function filesSummary(job={}){
  const rows=list(job.progress?.files).filter(row=>row&&row.path&&row.outcome!=='failed');
  const changed=new Set(rows.map(row=>row.path)).size;
  return {changed,total:job.inventory?.files||0,last:rows[0]?.path||''};
}

// One sentence plus the first failing check for a stopped job; null while it runs.
export function verdict(job={}){
  if(!terminalJobStates.has(job.state))return null;
  const banner=outcomeExplanation(job);
  if(!banner)return null;
  const checks=checkSummary(job);
  const failing=checks.failing[0]||null;
  const text=failing?String(banner.text||'').replace(/\s*\d+ checks? still failing\./,''):banner.text||'';
  return {tone:banner.tone,title:banner.title,text,failing,passed:checks.passed,failed:checks.failed,advisory:checks.advisory};
}

export function isWebApp(job={}){
  const profiles=list(job.execution_profile?.profiles||job.verification?.profiles);
  if(profiles.some(profile=>profile&&profile.launch))return true;
  if(list(job.checks).some(check=>check&&(check.phase==='launch'||/^(page|form|workflow|interface:labels):/.test(String(check.id||'')))))return true;
  if(job.progress?.ui_preflight||job.progress?.current?.kind==='ui_preflight')return true;
  return !!(job.ui_contract_version||job.ui_required);
}
