import test from 'node:test';
import assert from 'node:assert/strict';
import {applyJobEvent,consumeSseFrames,terminalJobStates,backgroundJob,cancellableWorkflowIds} from './daedalusJobs.js';

test('replayed and out-of-order progress cannot regress the job',()=>{
 const job={id:'job',state:'checking'};
 assert.equal(applyJobEvent(job,{seq:4,data:{state:'coding'}},5).job,job);
 const next=applyJobEvent(job,{seq:6,data:{state:'accepting'}},5);
 assert.equal(next.job.state,'accepting');assert.equal(next.job.id,'job');assert.equal(next.lastSequence,6);
});
test('SSE parsing retains split events and ignores heartbeats',()=>{
 const first=consumeSseFrames(': heartbeat\n\ndata: {"seq":1,"data":{"state":"checking"}}\n\ndata: {"seq":2');
 assert.equal(first.events[0].seq,1);assert.equal(first.events.length,1);
 const second=consumeSseFrames(first.remainder+',"data":{"state":"completed"}}\n\n');
 assert.equal(second.events[0].data.state,'completed');assert.equal(second.remainder,'');
});
test('blocked checkpoints stop streaming but cancelling stays active',()=>{
 assert(terminalJobStates.has('blocked'));assert(!terminalJobStates.has('cancelling'));
});
test('a later completed source question cannot hide the active background writer',()=>{
 const writer={id:'writer',workflow_version:3,state:'coding'};
 assert.equal(backgroundJob([{id:'question',workflow_version:3,state:'completed'},writer]),writer);
});

import {jobStatusLabel,resumeJobBody} from './daedalusJobs.js';
test('review candidates pause streaming and continuation pins the candidate revision',()=>{
 const job={policy_version:6,state:'ready_for_review',revision_id:'accepted-old',candidate_artifact:{metadata:{revision_id:'candidate-current'}}};
 assert(terminalJobStates.has(job.state));
 assert.equal(jobStatusLabel(job),'Build incomplete');
 job.candidate_artifact.metadata.runnable=true;assert.equal(jobStatusLabel(job),'Ready for review');
 assert.deepEqual(resumeJobBody(job),{candidate_revision:'candidate-current'});
 assert.deepEqual(resumeJobBody(job,'off'),{candidate_revision:'candidate-current',visual_review:false});
 assert.deepEqual(resumeJobBody({state:'blocked'}),{});
 assert.equal(jobStatusLabel({policy_version:6,state:'coding',repair_round:1}),'Repairing · attempt 1 of 2');
});

import {isDaedalusPersona,codingRequestFields,workflowIds,continueReason} from './daedalusJobs.js';
test('CodeAgent capability does not expose persona-specific controls or request fields',()=>{
 const assistant={persona_name:'Personal Assistant',tool_ids:['codeagent']};
 assert.equal(isDaedalusPersona(assistant),false);
 assert.deepEqual(codingRequestFields({daedalus_project_id:'stale',daedalus_visual_review:true},assistant),{});
 assert.equal(isDaedalusPersona(null,{persona_name:'Daedalus'}),true);
 assert.equal(isDaedalusPersona(assistant,{persona_name:'Daedalus'}),false);
 assert.deepEqual(codingRequestFields({daedalus_project_id:'selected',irrelevant:true},{persona_name:'Daedalus'}),{daedalus_project_id:'selected'});
});
test('only published accepted artifacts can display Complete',()=>{
 assert.equal(jobStatusLabel({state:'completed'}),'Needs attention');
 assert.equal(jobStatusLabel({state:'completed',artifact:{id:'a'}}),'Needs attention');
 assert.equal(jobStatusLabel({state:'completed',artifact:{id:'a'},artifact_status:'delivered'}),'Complete');
 assert.equal(jobStatusLabel({state:'candidate_packaging'}),'Saving checkpoint');
 assert.equal(jobStatusLabel({state:'ready_for_review',candidate_artifact:{metadata:{runnable:false}}}),'Build incomplete');
 assert.equal(continueReason({stop_limit:'audit_corrections',blocker:'Audit correction limit reached'}),'Audit correction limit reached');
});
test('workflow association survives event trimming and arbitrary tool names',()=>{
 assert.deepEqual(workflowIds({workflow_ids:['old'],saved_events:[{type:'tool_end',data:{tool:'new_tool',workflow_id:'saved'}}]},[{data:{workflow_id:'live'}},{data:{workflow_id:'old'}}]),['old','saved','live']);
});
test('chat Stop cancels running jobs only, never a parked candidate or blocked job',()=>{
 const workflows=[{id:'run',workflow_version:3,conversation_id:'c',state:'coding'},{id:'parked',workflow_version:3,conversation_id:'c',state:'ready_for_review'},
  {id:'blocked',workflow_version:3,conversation_id:'c',state:'blocked'},{id:'other',workflow_version:3,conversation_id:'x',state:'coding'},
  {id:'legacy',workflow_version:2,conversation_id:'c',state:'reviewing'}];
 const events=[{data:{workflow_id:'parked'}},{data:{workflow_id:'unknown-live'}},{data:{}}];
 assert.deepEqual(cancellableWorkflowIds(workflows,events,'c').sort(),['run','unknown-live']);
});
test('a spent model-call allowance leaves Continue enabled; durable repair limits do not',()=>{
 assert.equal(continueReason({stop_limit:'model_calls',blocker:'Model-call allowance exhausted'}),'');
 assert.equal(continueReason({stop_limit:'no_progress',blocker:'The editor made no source changes'}),'The editor made no source changes');
});
