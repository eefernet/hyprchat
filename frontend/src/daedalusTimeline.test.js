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
