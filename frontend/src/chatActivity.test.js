import test from 'node:test';
import assert from 'node:assert/strict';
import {chatActivity,activityDetail} from './chatActivity.js';

const e=(type,data={},timestamp)=>({type,data,...(timestamp==null?{}:{timestamp})});
const think=(text,type='thinking')=>e(type,{detail:JSON.stringify({thinking:text}),status:text});
const tool=(type,name='read_file',extra={})=>e(type,{tool:name,...extra});
const state=events=>chatActivity({events,live:true});

test('initial connection is replaced by thinking and actual answer output',()=>{
  const events=[tool('tool_start','processing'),think('checking')];
  assert.equal(state(events).phase,'thinking');
  events.push(e('answer_token'));
  assert.equal(state(events).phase,'writing');
  assert.equal(state(events).tools,0);
  assert.equal(state(events).entries.length,1);
});
test('generating progress is never a tool and plain completed answers have no row',()=>{
  const events=[tool('tool_start','processing'),tool('tool_start','generating'),tool('tool_progress','generating'),e('complete')];
  assert.equal(state(events).phase,'writing');
  assert.equal(chatActivity({events}).visible,false);
});
test('each reasoning cycle survives and late thought completion does not replace a tool',()=>{
  const events=[think('first'),think('first complete','thought_done'),tool('tool_start'),think('first complete with detail','thought_done')];
  assert.equal(state(events).phase,'tool');
  assert.equal(state(events).entries.filter(r=>r.kind==='reasoning').length,1);
  events.push(tool('tool_end'),think('second'),think('second complete','thought_done'));
  assert.equal(state(events).phase,'writing');
  assert.equal(activityDetail(state(events).entries.at(-1).event).thinking,'second complete');
  assert.equal(state(events).entries.filter(r=>r.kind==='reasoning').length,2);
});
test('latest final reasoning enriches the same entry without losing a longer trace',()=>{
  const result=state([think('a long live snapshot'),think('short','thought_done'),think('the full final reasoning snapshot','thought_done')]);
  assert.equal(result.entries.length,1);
  assert.equal(activityDetail(result.entries[0].event).thinking,'the full final reasoning snapshot');
});
test('tool progress and nested identified starts update one invocation',()=>{
  const result=state([tool('tool_start'),tool('tool_start','read_file',{run_id:'r1'}),tool('tool_status','read_file',{run_id:'r1',status:'Reading'}),tool('tool_end','read_file',{run_id:'r1',detail:'result'})]);
  assert.equal(result.entries.length,1);
  assert.equal(result.entries[0].state,'complete');
});
test('repeated uncorrelated calls remain separate, even with identical payloads',()=>{
  const result=state([tool('tool_start'),tool('tool_end'),tool('tool_start'),tool('tool_end')]);
  assert.equal(result.tools,2);
});
test('identified overlapping calls complete the correct entries',()=>{
  const result=state([tool('tool_start','read_file',{tool_call_id:'a'}),tool('tool_start','read_file',{tool_call_id:'b'}),tool('tool_end','read_file',{tool_call_id:'a'})]);
  assert.deepEqual(result.entries.map(r=>r.state),['complete','running']);
  assert.equal(result.phase,'tool');
});
test('saved and live event overlap does not duplicate tool history',()=>{
  const events=[e('tool_start',{tool:'read_file'},1),e('tool_done',{tool:'read_file'},2)];
  assert.equal(chatActivity({savedEvents:events,events:JSON.parse(JSON.stringify(events))}).tools,1);
});
test('progress never erases an existing tool detail',()=>{
  const result=state([tool('tool_start','read_file',{detail:'file contents'}),tool('tool_progress')]);
  assert.equal(activityDetail(result.entries[0].event),'file contents');
});
test('review remains the current phase while the candidate thinks and writes',()=>{
  const events=[tool('tool_start','refinement',{round:1,total:3}),think('review analysis'),e('answer_token')];
  assert.equal(state(events).label,'Reviewing 1/3');
  events.push(tool('tool_start','refinement',{round:2,total:3}));
  assert.equal(state(events).label,'Reviewing 2/3');
  const completed=chatActivity({events,status:'completed'});
  assert.ok(completed.entries.filter(r=>r.kind==='review').every(r=>r.state==='complete'));
});
test('Stop and failure override stale progress without fabricating tool success',()=>{
  const events=[tool('tool_start'),think('working')];
  for(const status of ['stopped','interrupted','failed']){
    const result=chatActivity({events,live:true,status});
    assert.equal(result.active,false);
    assert.ok(result.entries.every(r=>r.state==='interrupted'));
    assert.notEqual(result.phase,'complete');
  }
  assert.equal(chatActivity({events,status:'completed'}).entries[0].state,'unknown');
});
test('incomplete or failed reviews never become successful after answer completion',()=>{
  const start=tool('tool_start','refinement',{round:1,total:1});
  const retained=tool('tool_done','refinement',{status:'Previous answer retained',outcome:'retained'});
  const result=chatActivity({events:[start,retained,{...retained}],status:'completed'});
  assert.equal(result.entries.length,1);
  assert.equal(result.entries[0].state,'retained');
  assert.equal(chatActivity({events:[start,tool('tool_error','refinement')],status:'failed'}).entries[0].state,'failed');
});
test('old histories with several completed reasoning cycles retain each cycle',()=>{
  const result=chatActivity({events:[think('one','thought_done'),tool('tool_done'),think('two full','thought_done')]});
  assert.equal(result.entries.filter(r=>r.kind==='reasoning').length,2);
});
test('standalone errors stay visible and specialized document events are excluded',()=>{
  assert.equal(chatActivity({events:[e('error',{status:'Connection lost'})]}).phase,'failed');
  assert.equal(chatActivity({events:[tool('tool_start','document_read')]}).visible,false);
});
