import test from 'node:test';
import assert from 'node:assert/strict';
import {taskBody} from './taskForm.js';
test('editing a scheduled task preserves execution settings and delivery extensions',()=>{
  const body=taskBody({title:'Project brief',prompt:'Summarize',task_type:'llm',schedule_kind:'weekly',weekday:'2',time:'09:00',model:'local',tool_ids:['documents'],workspace_id:'ws',timezone:'America/Phoenix',delivery:{ntfy:true,email:false,extension:'keep'},notify:true});
  assert.deepEqual(body.schedule_json,{time:'09:00',weekday:2});
  assert.equal(body.workspace_id,'ws');assert.equal(body.delivery_json.extension,'keep');
  assert.equal(body.delivery_json.conversation,true);assert.deepEqual(body.tool_ids,['documents']);
});
test('assistant-managed check-ins retain their type and webhook edits retain their trigger',()=>{
  assert.equal(taskBody({task_type:'check_in',schedule_kind:'daily',time:'08:00'}).task_type,undefined);
  assert.deepEqual(taskBody({task_type:'llm',schedule_kind:'webhook'}).schedule_json,{});
});
