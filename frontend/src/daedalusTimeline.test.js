import test from 'node:test';
import assert from 'node:assert/strict';

import {
  _eventsHaveDaedalusFullBuild,
  _isDaedalusFullBuildOutput,
  _isDaedalusOutput,
} from './daedalusTimeline.js';

test('full build events activate the Daedalus timeline classifier', () => {
  const savedEvents = [
    { type: 'tool_start', data: { tool: 'plan_project' } },
    { type: 'tool_end', data: { tool: 'generate_code', run_id: 'run-builder' } },
  ];

  assert.equal(_eventsHaveDaedalusFullBuild(savedEvents), true);
  assert.equal(_isDaedalusFullBuildOutput({ savedEvents }), true);
  assert.equal(_isDaedalusOutput({ savedEvents }), true);
});

test('run roles and live build workflows count as full builds', () => {
  assert.equal(
    _isDaedalusFullBuildOutput({ meta: { run_roles: ['reviewer', 'builder.scaffold'] } }),
    true,
  );
  assert.equal(
    _isDaedalusFullBuildOutput({
      meta: { in_progress: true },
      workflows: [{ mode: 'build_from_prompt', state: 'running' }],
    }),
    true,
  );
});

test('ask_project follow-up output stays off the full-build timeline', () => {
  const savedEvents = [
    { type: 'tool_start', data: { tool: 'ask_project', run_id: 'run-qa' } },
    { type: 'tool_end', data: { tool: 'ask_project', run_id: 'run-qa' } },
  ];
  const meta = { run_ids: ['run-qa'], run_roles: ['qa'], has_full_product_build: false };

  assert.equal(_isDaedalusOutput({ meta, savedEvents, runIds: ['run-qa'] }), true);
  assert.equal(_isDaedalusFullBuildOutput({ meta, savedEvents, runIds: ['run-qa'] }), false);
});

test('download_project and normal follow-up events do not count as full builds', () => {
  assert.equal(
    _isDaedalusFullBuildOutput({
      savedEvents: [{ type: 'tool_end', data: { tool: 'download_project', run_id: 'run-package' } }],
      runIds: ['run-package'],
    }),
    false,
  );
  assert.equal(
    _isDaedalusFullBuildOutput({
      savedEvents: [{ type: 'tool_end', data: { tool: 'fetch_url' } }],
      runIds: [],
    }),
    false,
  );
});

test('a persistent v3 job owns its message in every state, not only legacy-active ones', () => {
  // 2026-09-19: the in-chat job card vanished once a job reached coding/checking or stopped.
  for (const state of ['coding', 'checking', 'auditing', 'cancelled', 'ready_for_review', 'completed']) {
    assert.equal(_isDaedalusOutput({ workflows: [{ id: 'cw3-x', workflow_version: 3, state }] }), true, state);
  }
  assert.equal(_isDaedalusOutput({ workflows: [{ id: 'cw-legacy', state: 'completed' }] }), false);
  assert.equal(_isDaedalusOutput({ workflows: [] }), false);
});

test('documents never become Daedalus from run IDs, live events or saved metadata',()=>{
  const events=[{type:'tool_start',data:{tool:'document_create',run_id:'doc-1',run_role:'documents'}}];
  for(const input of [
    {liveEvents:events,runIds:['doc-1']},
    {savedEvents:events,meta:{run_ids:['doc-1']}},
    {savedEvents:[...events,{data:{tool:'download_file'}}]},
    {meta:{run_ids:['doc-1','doc-2','doc-3'],run_roles:['documents']},runIds:['doc-1','doc-2','doc-3']},
  ]){
    assert.equal(_isDaedalusOutput(input),false);
    assert.equal(_isDaedalusFullBuildOutput(input),false);
  }
  assert.equal(_isDaedalusOutput({runIds:['unknown-run']}),false);
  assert.equal(_isDaedalusOutput({meta:{run_roles:['builder.feature']}}),true);
});

test('mixed document and coding events preserve genuine Daedalus classification',()=>{
  assert.equal(_isDaedalusOutput({savedEvents:[{data:{tool:'document_create',run_id:'doc-a'}},{data:{tool:'generate_code',run_id:'run-b'}}]}),true);
});
