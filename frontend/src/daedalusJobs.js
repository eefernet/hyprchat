export const terminalJobStates=new Set(['completed','cancelled','blocked','waiting_for_input','ready_for_review']);
// Completed work never changes again. (A cancelled policy-7 job can be continued from another tab, so it keeps its slow poll.)
export const finishedJobStates=new Set(['completed']);

export function backgroundJob(workflows=[]){
  const jobs=workflows.filter(job=>job.workflow_version===3);
  return jobs.find(job=>!terminalJobStates.has(job.state))||jobs[0]||null;
}

export function jobStatusLabel(job){
  if(job.state==='completed')return job.artifact&&job.artifact_status==='delivered'?'Complete':job.answer?'Review complete':'Needs attention';
  if(job.state==='ready_for_review')return job.candidate_artifact?.metadata?.runnable?'Ready for review':'Build incomplete';
  if(job.state==='coding')return job.repair_round>0?`Repairing · attempt ${job.repair_round} of 2`:'Building';
  return ({queued:'Planning',inspecting:'Planning',baselining:'Checking',planning:'Planning',
    checking:'Checking',auditing:'Checking',reviewing:'Reviewing',accepting:'Reviewing',visual_review:'Reviewing',
    packaging:'Saving checkpoint',candidate_packaging:'Saving checkpoint',cancelling:'Stopping',
    cancelled:'Stopped',blocked:'Needs attention',waiting_for_input:'Needs attention'})[job.state]||'Needs attention';
}

export function isDaedalusPersona(conversation,pending=null){
  const name=conversation?conversation.persona_name:pending?.persona_name;
  return /daedalus|coder/i.test(name||'');
}
export function codingRequestFields(options,conversation,pending=null){
  if(!isDaedalusPersona(conversation,pending))return {};
  return Object.fromEntries(Object.entries(options||{}).filter(([key])=>['daedalus_new_project','daedalus_project_id','daedalus_visual_review'].includes(key)));
}
export function workflowIds(metadata={},events=[]){
  return [...new Set([...(metadata.workflow_ids||[]),...(metadata.saved_events||[]).concat(events).map(e=>e.data?.workflow_id)].filter(Boolean))];
}
export function jobActivity(job){
  const compact=value=>{const text=String(value).replace(/\s+/g,' ').trim();return text.length>140?`${text.slice(0,137)}…`:text;};
  if(job.presentation?.activity)return compact(job.presentation.activity);
  if(job.state==='coding'){
    if(job.narrowed_targets?.length)return compact(`Updating ${job.narrowed_targets[0]}`);
    return job.last_patch?.category==='output_limit'?'Splitting work into smaller file edits':compact(job.brief?.batches?.[job.batch||0]?.task||'Creating project files');
  }
  return ({queued:'Waiting for the coding worker',inspecting:'Inspecting project files',baselining:'Checking the original project',
    planning:'Preparing an implementation plan',checking:'Running project checks',reviewing:'Preparing independent checks',
    auditing:'Testing the current revision',accepting:'Reviewing the execution evidence',visual_review:'Reviewing the interface',
    packaging:'Publishing the accepted revision',candidate_packaging:'Saving the current candidate',
    cancelling:'Waiting for the worker to acknowledge Stop',cancelled:'Work stopped at a saved checkpoint',
    completed:job.artifact?'Accepted revision published':'Review finished',ready_for_review:'Review the saved candidate and its checks',
    blocked:'Open details to inspect the checkpoint',waiting_for_input:'The job needs your attention'})[job.state]||'Loading job status';
}
// Limits Continue cannot lift. A spent model-call allowance is not one: Continue grants another.
export const durableLimits=new Set(['application_repairs','audit_corrections','no_progress']);
export function continueReason(job){
  if(job.state==='cancelled'&&(job.policy_version||1)<7)return 'This older workflow cannot resume after Stop. Start a new request from its saved project.';
  return job.presentation?.actions?.resume?.disabled_reason||(durableLimits.has(job.stop_limit)?(job.blocker||'Retained limits prevent further progress on this request.'):'');
}
// The chat Stop button stops work that is RUNNING. A parked job (blocked, ready for review) has nothing
// to stop; cancelling it turned a saved candidate into "Stopped" and lost its place for Continue.
export function cancellableWorkflowIds(workflows=[],events=[],conversationId=null){
  const known=new Map(workflows.map(workflow=>[workflow.id,workflow]));
  const ids=new Set(events.map(event=>event.data?.workflow_id).filter(id=>id&&!(known.has(id)&&terminalJobStates.has(known.get(id).state))));
  workflows.filter(workflow=>workflow.workflow_version===3&&workflow.conversation_id===conversationId&&!terminalJobStates.has(workflow.state))
    .forEach(workflow=>ids.add(workflow.id));
  return [...ids];
}
export function stageIndex(job){
  return ({queued:0,inspecting:0,planning:0,baselining:0,coding:1,checking:2,auditing:2,reviewing:3,accepting:3,visual_review:3,packaging:3,completed:4})[job.state]??-1;
}
export function elapsedLabel(seconds){
  seconds=Math.max(0,Math.floor(seconds));return seconds<60?`${seconds}s`:`${Math.floor(seconds/60)}m ${seconds%60}s`;
}
export function timestamp(value){return typeof value==='number'?value*1000:Date.parse(value?.endsWith('Z')||/[+-]\d\d:\d\d$/.test(value||'')?value:`${value||''}Z`);}

export function resumeJobBody(job,visual='unchanged'){
  return {...(visual!=='unchanged'?{visual_review:visual==='on'}:{}),
    ...(job.candidate_artifact?{candidate_revision:job.candidate_artifact.metadata?.revision_id}: {})};
}

export function jobBlockerMessage(value){
  const message=String(value||'');
  if(message.includes('Model-call allowance exhausted'))return 'The model-call allowance is used up. Continue from checkpoint for another allowance, or adjust it in Settings → Daedalus.';
  if(message.includes('Execution allowance exhausted'))return 'The execution allowance is used up. Continue from checkpoint for more time, or adjust it in Settings → Daedalus.';
  if(/schema|validation|JSON|parse|ValueError|Traceback/i.test(message))return 'A check could not be completed. Open details for the diagnostic and saved checkpoint.';
  return message.split('\n\nConversation logs are stored at:')[0];
}

export function applyJobEvent(job,event,lastSequence=0){
  if(!event||event.seq<=lastSequence)return {job,lastSequence};
  return {job:{...job,...event.data,event_sequence:event.seq},lastSequence:event.seq};
}

export function consumeSseFrames(buffer){
  const frames=buffer.replace(/\r\n/g,'\n').split('\n\n');
  const remainder=frames.pop();
  const events=[];
  for(const frame of frames){
    const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
    if(data){try{events.push(JSON.parse(data));}catch{}}
  }
  return {events,remainder};
}
