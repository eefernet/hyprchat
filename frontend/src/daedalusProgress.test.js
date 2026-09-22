import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stageRail,loopLabel,planOutcomes,planSteps,checkSummary,outcomeExplanation,nowLine,requestSummary} from './daedalusProgress.js';

// Trimmed snapshots of real jobs from the 2026-09-18/19 evaluation runs.
const jobs=JSON.parse(readFileSync(new URL('./daedalusProgress.fixtures.json',import.meta.url)));

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
