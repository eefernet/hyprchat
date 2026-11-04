import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stageRail,loopLabel,planOutcomes,planSteps,checkSummary,outcomeExplanation,nowLine,requestSummary,fileActivity} from './daedalusProgress.js';
import {jobStatusLabel} from './daedalusJobs.js';

// Trimmed snapshots of real jobs from the 2026-09-18/19 evaluation runs.
const jobs=JSON.parse(readFileSync(new URL('./daedalusProgress.fixtures.json',import.meta.url)));

test('modern runnable candidates with unverified requirements use revision evidence without upgrading checks',()=>{
  const job={engine:'openhands159',state:'ready_for_review',revision_id:'current',ui_contract_version:1,
    candidate_artifact:{id:'candidate'},verification_summary:{revision_id:'current',checks_ok:true,exercised:true,
      checks_passed:false,failures:[],criteria:[{status:'unverified'}]}};
  const original=JSON.stringify(job);
  assert.equal(jobStatusLabel(job),'Ready for review');
  assert.equal(outcomeExplanation(job).title,'Ready for your review');
  assert.equal(outcomeExplanation(job).tone,'warn');
  assert.equal(JSON.stringify(job),original);
  for(const change of [{revision_id:'old'},{revision_id:null},{failures:[{id:'broken'}]},{checks_ok:false},{incomplete_reason:'Generation stopped'}]){
    const invalid={...job,verification_summary:{...job.verification_summary,...change}};
    assert.match(jobStatusLabel(invalid),/^Build incomplete/);
    assert.equal(outcomeExplanation(invalid).title,'Build incomplete');
  }
  const unexercised={...job,verification_summary:{...job.verification_summary,exercised:false}};
  assert.equal(jobStatusLabel(unexercised),'Built, not exercised');
  assert.equal(outcomeExplanation(unexercised).title,'Built, not exercised');
  const oldBadge={...job,candidate_artifact:{metadata:{checks_passed:true}}};
  assert.equal(jobStatusLabel(oldBadge),'Ready for review');
  assert.equal(outcomeExplanation(oldBadge).title,'Ready for your review');
});

test('unverified and failed UI contracts cannot inherit a green artifact badge',()=>{
  for(const status of ['failed','unverified']){
    const job={state:'ready_for_review',ui_contract_version:1,
      candidate_artifact:{metadata:{runnable:true,checks_passed:true}},
      verification_summary:{checks_ok:true,exercised:true,checks_passed:false,criteria:[{id:'ui',status}]}};
    assert.equal(outcomeExplanation(job).tone,'warn');
    assert.equal(outcomeExplanation(job).title,'Ready for your review');
  }
});

test('an accepted build shows every stage done, every outcome passed and a plain acceptance',()=>{
  const job=jobs.accepted_java;
  assert.deepEqual(stageRail(job).map(s=>s.state),Array(6).fill('done'));
  assert.ok(planOutcomes(job).every(row=>row.status==='passed'&&!row.missing.length));
  const steps=planSteps(job);
  assert.equal(steps.length,3);
  assert.ok(steps.every(step=>step.state==='done'),JSON.stringify(steps));
  assert.ok(steps[0].files.some(file=>file.path==='pom.xml'&&file.written));
  const banner=outcomeExplanation(job);
  assert.equal(banner.tone,'good');assert.match(banner.text,/verified by executed checks/);
  assert.equal(checkSummary(job).failed,0);
});

test('a withheld job says why in plain language and names what is still open',()=>{
  const job=jobs.withheld_csharp;
  const banner=outcomeExplanation(job);
  assert.equal(banner.tone,'warn');
  assert.match(banner.text,/independent audit could not be completed after two corrections/);
  assert.match(banner.text,/o1 needs behavior evidence/);
  assert.equal(planOutcomes(job)[0].status,'unverified');
  assert.deepEqual(planOutcomes(job)[0].missing,['behavior']);
  assert.match(loopLabel(job),/Audit correction 2 of 2/);
  const rail=stageRail(job);
  assert.equal(rail[0].state,'done');assert.ok(rail.some(s=>s.state==='stopped'));
});

test('checks collapse to the latest row per id and failing ones get a one-line headline',()=>{
  const job=jobs.no_progress_c,summary=checkSummary(job);
  assert.equal(new Set(summary.rows.map(row=>row.id)).size,summary.rows.length);
  assert.ok(summary.failed>=1);
  const failing=summary.failing[0];
  assert.ok(failing.headline.length>0&&failing.headline.length<=200&&!failing.headline.includes('\n'));
  assert.equal(failing.label,'Tests');
  assert.match(outcomeExplanation(job).text,/editor stopped making changes/);
  const audit=checkSummary(jobs.node_syntax_error).failing.find(row=>row.id==='audit-metadata');
  assert.equal(audit.label,'Independent audit');assert.match(audit.headline,/not executable evidence/);
});

test('a running agent build reports its pass, its repair round and the files still to write',()=>{
  const job={state:'coding',builder:'sdk',build_continuations:1,repair_round:1,batch:2,
    brief:{outcomes:[{id:'o1',text:'CLI works',evidence_types:['behavior','tests']}],
      batches:[{task:'Core',files:['server.js','database.js']},{task:'UI',files:['public/app.js','public/style.css']}]},
    last_patch:{changed:['server.js'],source_hashes:{'server.js':'h','database.js':'h'}}};
  assert.equal(loopLabel(job),'Agent builder · pass 2 of 6 · Repair 1 of 2');
  const steps=planSteps(job);
  assert.equal(steps[0].state,'done');assert.equal(steps[1].state,'current');
  assert.equal(nowLine(job),'Writing public/app.js, public/style.css');
  assert.deepEqual(stageRail(job).map(s=>s.state),['done','current','pending','pending','pending','pending']);
  assert.equal(planOutcomes(job)[0].status,'pending');
  assert.equal(outcomeExplanation(job),null);
  assert.equal(nowLine({state:'coding',narrowed_targets:['a.py','b.py']}),'Editing a.py');
});

test('older and empty jobs render without throwing',()=>{
  for(const job of [{},{state:'cancelled'},{state:'planning',brief:null},{state:'ready_for_review',brief:{outcomes:[{id:'o1',text:'x'}]}},
      {state:'cancelled',policy_version:6,brief:{outcomes:[{id:'o1',text:'Policy 6 brief has no batches',evidence_types:['behavior']}]},checks:[{id:'.:test:0',passed:false}]}]){
    stageRail(job);loopLabel(job);planOutcomes(job);planSteps(job);checkSummary(job);outcomeExplanation(job);nowLine(job);
  }
  assert.equal(outcomeExplanation({state:'cancelled',inventory:{files:3}}).text,'Work stopped at a saved checkpoint with 3 files.');
  assert.equal(requestSummary('**Tech Stack:**\n- Node.js + `Express`'),'Tech Stack: - Node.js + Express');
});

test('a job with stage progress shows its phase on the strip and the card', () => {
  const job={state:'coding',builder:'sdk',build_continuations:0,repair_round:1,
    stage_progress:{phase:1,of:4,label:'foundation',blocked_by:['launch:app'],complete:false}};
  assert.equal(loopLabel(job),'Phase 1/4 · foundation · Agent builder · pass 1 of 6 · Repair 1 of 2');
  assert.equal(loopLabel({state:'completed',artifact:{},stage_progress:{phase:4,of:4,label:'delivery'}}),'');
  assert.equal(loopLabel({state:'checking'}),'');
});

test('passing checks, an unexercised build and the two completion kinds explain themselves',()=>{
  const ready={state:'ready_for_review',candidate_artifact:{metadata:{runnable:true}},checks:[{id:'test',phase:'test',passed:true}]};
  const passed=outcomeExplanation({...ready,verification_summary:{checks_passed:true}});
  assert.equal(passed.title,'Checks passed — please review');assert.match(passed.text,/Accept it or ask for changes/);
  assert.equal(outcomeExplanation({...ready,verification_summary:{checks_ok:true,exercised:false}}).title,'Built, not exercised');
  const done={state:'completed',artifact:{id:'a'}};
  assert.match(outcomeExplanation({...done,accepted_by:'user'}).text,/You accepted this exact revision/);
  assert.match(outcomeExplanation({...done,acceptance:{kind:'examples'},verification_summary:{confirmed_examples:3}}).text,/confirmed passed \(3\)/);
});

test('a rolled-back change explains the rollback instead of a generic limit',()=>{
  const job={state:'ready_for_review',stop_limit:'application_repairs',candidate_artifact:{metadata:{runnable:true}},
    blocker:'The change broke tests that passed before: test_total (failed). The last version that kept them passing was restored for review.',
    verification_summary:{rolled_back:true,checks_passed:false}};
  const banner=outcomeExplanation(job);
  assert.equal(banner.title,'Change rolled back');assert.match(banner.text,/test_total \(failed\)/);
});

test('modern stage rail keeps preparation failure at Check',()=>{
  assert.deepEqual(stageRail({engine:'openhands159',state:'blocked',failure:{stage:'checking'}}).map(x=>[x.label,x.state]),
    [['Plan','done'],['Build','done'],['Check','stopped'],['Deliver','pending']]);
});
test('modern tasks are agent reported and do not imply verification',()=>{
  const job={engine:'openhands159',state:'coding',progress:{version:1,plan:[{title:'Create UI',status:'done'}]}};
  assert.equal(planSteps(job)[0].state,'done');
  assert.equal(outcomeExplanation(job),null);
  assert.equal(nowLine(job),'Preparing the next model action');
});


test('file activity is active only during its action and retains the last update',()=>{
  const job={state:'coding',progress:{version:1,current:{kind:'file',path:'app.js',action:'create'},files:[{path:'last.js',outcome:'succeeded'}]}};
  assert.deepEqual(fileActivity(job),{label:'Creating',path:'app.js'});
  assert.deepEqual(fileActivity({...job,state:'cancelled'}),{label:'Last updated:',path:'last.js'});
  assert.equal(fileActivity({state:'coding'}).label,'No file changes yet');
});

test('modern delivery retains failed and unverified revision checks',()=>{
  const job={engine:'openhands159',state:'ready_for_review',revision_id:'r2',candidate_artifact:{id:'candidate'},
    verification_summary:{revision_id:'r2',checks_passed:false,failures:[{id:'setup'}]}};
  assert.equal(stageRail(job)[2].state,'failed');
  assert.equal(stageRail(job)[3].state,'delivered');
  assert.equal(stageRail({...job,state:'completed'})[2].state,'failed');
  assert.equal(stageRail({...job,revision_id:'r3'})[2].state,'unverified');
  assert.equal(stageRail({...job,state:'blocked',failure:{category:'environment',stage:'checking'}})[2].state,'blocked');
});

test('modern check list requires evidence bound to the current revision',()=>{
  const job={engine:'openhands159',revision_id:'new',checks:[{id:'test',passed:true,revision_id:'old'},{id:'unbound',passed:true}]};
  assert.equal(checkSummary(job).rows.length,0);
});

test('modern failed lint is advisory only when the controller marks it optional',()=>{
  const job={engine:'openhands159',revision_id:'r',checks:[{id:'lint',phase:'lint',revision_id:'r',passed:false}]};
  assert.equal(checkSummary(job).failed,1);
  assert.equal(checkSummary({...job,checks:[{...job.checks[0],optional:true}]}).advisory,1);
});

test('historical workflow advisories never count as passed, even after owner acceptance',()=>{
  const job={engine:'openhands159',state:'completed',revision_id:'r',checks:[
    ...Array.from({length:8},(_,i)=>({id:`check:${i}`,passed:true,revision_id:'r'})),
    ...Array.from({length:2},(_,i)=>({id:`workflow:${i}`,passed:true,workflow_status:'advisory',revision_id:'r',log_tail:'Inconclusive'}))]};
  const before=JSON.stringify(job);let counts=checkSummary(job);
  assert.equal(counts.passed,8);assert.equal(counts.advisory,2);assert.equal(counts.failed,0);
  assert.equal(JSON.stringify(job),before);
  job.checks.push({id:'extra',passed:true,revision_id:'r'},{id:'workflow:extra',passed:true,workflow_status:'advisory',revision_id:'r'});
  counts=checkSummary(job);assert.equal(counts.passed,9);assert.equal(counts.advisory,3);
});
test('unreconciled agent plan is not active after coding and is never auto-completed',()=>{
  const job={state:'ready_for_review',progress:{version:1,plan:[{title:'Done',status:'done'},{title:'Old',status:'in_progress'},{title:'Pending',status:'todo'}]}};
  assert.deepEqual(planSteps(job).map(s=>s.state),['done','not_updated','not_updated']);
  assert.deepEqual(planSteps({...job,state:'coding'}).map(s=>s.state),['done','current','pending']);
});
test('outcome categories are disjoint and only the latest current-revision row counts',()=>{
 const job={engine:'openhands159',revision_id:'new',checks:[{id:'a',passed:true,revision_id:'old'},{id:'b',passed:true,revision_id:'new'},
 {id:'b',passed:false,revision_id:'new'},{id:'c',passed:false,optional:true,revision_id:'new'},
 {id:'d',skipped:true,revision_id:'new'},{id:'e',status:'unverified',revision_id:'new'}]};
 const summary=checkSummary(job);assert.deepEqual([summary.passed,summary.failed,summary.advisory,summary.skipped,summary.unverified],[0,1,1,1,1]);
});

test('advisory-only historical evidence does not turn Check green',()=>{
 const job={engine:'openhands159',state:'ready_for_review',revision_id:'r',verification_summary:{revision_id:'r',checks_passed:true},checks:[{id:'probe',revision_id:'r',passed:true,workflow_status:'advisory'}]};
 assert.equal(stageRail(job)[2].state,'unverified');assert.notEqual(outcomeExplanation(job).title,'Checks passed — please review');
});

test('UI preflight progress is diagnostic and never a delivered passing check',()=>{
  const job={engine:'openhands159',state:'coding',progress:{version:1,current:{kind:'ui_preflight'},ui_preflight:{passed:true,diagnostic_only:true}},checks:[]};
  assert.equal(nowLine(job),'Checking UI flows');
  assert.equal(checkSummary(job).passed,0);
  assert.equal(outcomeExplanation(job),null);
});
