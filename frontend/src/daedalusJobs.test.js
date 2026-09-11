import test from 'node:test';
import assert from 'node:assert/strict';
import {applyJobEvent,consumeSseFrames,terminalJobStates} from './daedalusJobs.js';

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
