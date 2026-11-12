import test from 'node:test';
import assert from 'node:assert/strict';
import {liveActivity,activityRows,activityText,activityIcon,stageTimings,counters,filesSummary,verdict,isWebApp,jobElapsed} from './daedalusActivity.js';

const now=Date.parse('2026-11-11T10:00:30Z');
const running=(current,extra={})=>({engine:'openhands159',state:'coding',calls_used:50,operation_calls:8,progress:{version:1,current,files:[],recent:[]},...extra});

test('the live line names the command, the file or the thinking call',()=>{
  const started='2026-11-11T10:00:18Z';
  let line=liveActivity(running({id:'c1',kind:'command',verb:'run',command:'npm test',started_at:started}),now);
  assert.equal(line.text,'Running npm test');assert.equal(line.target,'npm test');assert.equal(line.elapsed,12);
  assert.equal(line.phase,'tool');assert.equal(line.tool,'run_shell');
  line=liveActivity(running({id:'f1',kind:'file',verb:'edit',path:'public/app.js',action:'edit',started_at:started}),now);
  assert.equal(line.text,'Writing public/app.js');assert.equal(line.phase,'writing');
  line=liveActivity(running({id:'r1',kind:'tool',verb:'read',path:'server.js',started_at:started}),now);
  assert.equal(line.text,'Reading server.js');assert.equal(line.tool,'read_file');
  line=liveActivity(running({id:'m1',kind:'model',verb:'think',started_at:started}),now);
  assert.equal(line.label,'Thinking','the generic label keeps the playful pool');assert.equal(line.text,'Thinking · model call 58');
  line=liveActivity(running({id:'k1',kind:'command',verb:'check',command:'pytest -q',started_at:started}),now);
  assert.equal(line.text,'Checking pytest -q');assert.equal(line.phase,'reviewing');
  line=liveActivity(running({id:'c2',kind:'command',verb:'run',command:'',started_at:started}),now);
  assert.equal(line.text,'Running a command','an emptied command still reads sensibly');
});
test('older snapshots and stopped jobs fall back cleanly',()=>{
  const old=liveActivity(running({id:'x',kind:'command',started_at:1}),now);
  assert.equal(old.text,'Running a command');assert.equal(old.verb,'');
  assert.equal(liveActivity({state:'ready_for_review'}),null);
  assert.equal(liveActivity({state:'coding'}).text,'Creating project files');
});
test('activity rows pair calls with results, mark the live call and fall back to file records',()=>{
  const job=running({id:'op:3',kind:'command',verb:'run',command:'npm test'},{progress:{version:1,current:{id:'op:3',kind:'command',verb:'run',command:'npm test'},files:[],recent:[
    {id:'op:3',kind:'tool_call',verb:'run',tool:'terminal',command:'npm test',stage:'coding',at:3},
    {id:'op:2:done',kind:'tool_result',ref:'op:2',outcome:'failed',exit_code:1,at:2.5},
    {id:'op:2',kind:'tool_call',verb:'run',tool:'terminal',command:'npm run build',stage:'coding',at:2},
    {id:'op:1:done',kind:'tool_result',ref:'op:1',outcome:'succeeded',at:1.5},
    {id:'op:1',kind:'tool_call',verb:'edit',tool:'file_editor',path:'public/app.js',stage:'coding',at:1}]}});
  const rows=activityRows(job);
  assert.deepEqual(rows.map(row=>[row.verb,row.target,row.outcome,row.running]),[['run','npm test','running',true],['run','npm run build','failed',false],['edit','public/app.js','succeeded',false]]);
  assert.deepEqual(activityText(rows[0]),{label:'Running',target:'npm test',note:''});
  assert.deepEqual(activityText(rows[1]),{label:'Ran',target:'npm run build',note:'exit 1'});
  assert.deepEqual(activityText(rows[2]),{label:'Edited',target:'public/app.js',note:''});
  assert.equal(activityIcon(rows[0]),'Terminal');assert.equal(activityIcon(rows[2]),'Pencil');assert.equal(activityIcon({verb:'nonsense'}),'Activity');
  const legacy=activityRows({state:'coding',progress:{version:1,files:[{id:'f',kind:'file',path:'a.py',action:'create',outcome:'succeeded',at:1}]}});
  assert.deepEqual(activityText(legacy[0]),{label:'Created',target:'a.py',note:''});
  assert.deepEqual(activityRows({}),[]);
  const stopped={...job,state:'ready_for_review'};
  assert.equal(activityRows(stopped)[0].outcome,'unknown','a stopped job has no live call');
  assert.deepEqual(activityText({verb:'check',target:'pytest',outcome:'succeeded'}),{label:'Check',target:'pytest',note:'passed'});
});
test('stage timings sum accounted operations and add the live one',()=>{
  const job={engine:'openhands159',state:'checking',operation_kind:'check',operation_seconds:20,operation_accounted:false,
    worker_contact_at:'2026-11-11T10:00:20Z',stage_usage:[{operation_id:'a',stage:'inspect',seconds:5},{operation_id:'b',stage:'code',seconds:100},{operation_id:'c',stage:'code',seconds:40},{operation_id:'d',stage:'package',seconds:0}]};
  assert.deepEqual(stageTimings(job,now).map(row=>row.seconds),[5,140,30,0]);
  assert.deepEqual(stageTimings({...job,state:'ready_for_review'},now).map(row=>row.seconds),[5,140,0,0]);
  assert.deepEqual(stageTimings({...job,operation_kind:undefined,state:'coding'},now).map(row=>row.seconds),[5,170,0,0],'state is the fallback for the live segment');
  assert.equal(stageTimings({state:'coding',stage_usage:[]}),undefined,'legacy rails get no timings');
});
test('counters, files and elapsed read the same fields the old card did',()=>{
  const job={engine:'openhands159',state:'coding',model:'qwen',seconds_used:300,operation_seconds:10,worker_contact_at:'2026-11-11T10:00:20Z',calls_used:60,operation_calls:3,call_limit:120,inventory:{files:8},revision_id:'ffe9f30eabc',
    progress:{files:[{id:'1',path:'b.js',outcome:'succeeded'},{id:'2',path:'a.js',outcome:'failed'},{id:'3',path:'b.js',outcome:'succeeded'}]}};
  assert.equal(jobElapsed(job,now),320);
  assert.deepEqual(counters(job,now),['qwen','5m 20s elapsed','63 of 120 model calls','8 files','revision ffe9f30e']);
  assert.deepEqual(filesSummary(job),{changed:1,total:8,last:'b.js'});
  assert.deepEqual(counters({},now),['0s elapsed','0 model calls']);
});
test('the verdict is one sentence plus the first failing check',()=>{
  const job={engine:'openhands159',state:'ready_for_review',stop_limit:'application_repairs',revision_id:'r1',
    checks:[{id:'.:test:0',phase:'test',passed:false,revision_id:'r1',command:'npm test',log_tail:'Error: expected x\n'},{id:'.:build:0',phase:'build',passed:true,revision_id:'r1'}],
    verification_summary:{revision_id:'r1',failures:[{id:'.:test:0'}]}};
  const result=verdict(job);
  assert.equal(result.title,'Build incomplete');assert.equal(result.tone,'warn');
  assert.doesNotMatch(result.text,/still failing/);assert.match(result.text,/both automatic repair rounds were used/);
  assert.equal(result.failing.label,'Tests');assert.equal(result.failed,1);assert.equal(result.passed,1);
  assert.equal(verdict({state:'coding'}),null);
});
test('web apps are recognised from launch profiles, browser checks or preflight',()=>{
  assert(isWebApp({execution_profile:{profiles:[{cwd:'.',launch:'node server.js'}]}}));
  assert(isWebApp({checks:[{id:'page:app',passed:true}]}));
  assert(isWebApp({checks:[{id:'.:launch:0',phase:'launch'}]}));
  assert(isWebApp({progress:{ui_preflight:{status:'passed'}}}));
  assert(!isWebApp({checks:[{id:'.:test:0',phase:'test'}],execution_profile:{profiles:[{cwd:'.'}]}}));
  assert(!isWebApp({}));
});
