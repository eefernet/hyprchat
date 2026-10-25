import test from 'node:test';
import assert from 'node:assert/strict';
import {TONES, SOURCE_KIND, READINESS, JOB_STATUS, JOB_KIND, REPORT_STATUS, humanize, statusInfo} from './workspaceLabels.js';

const tonedMaps = {readiness: READINESS, 'job-status': JOB_STATUS, 'report-status': REPORT_STATUS};
const labelMaps = {'source-kind': SOURCE_KIND, 'job-kind': JOB_KIND};

test('every known toned value maps to its label and a tone from the set', () => {
  for (const [kind, map] of Object.entries(tonedMaps)) {
    for (const [value, [label, tone]] of Object.entries(map)) {
      assert.deepEqual(statusInfo(kind, value), {label, tone}, `${kind}:${value}`);
      assert.ok(TONES.includes(tone), `${kind}:${value} tone ${tone}`);
    }
  }
});

test('label-only maps return the label with the muted tone', () => {
  for (const [kind, map] of Object.entries(labelMaps)) {
    for (const [value, label] of Object.entries(map)) assert.deepEqual(statusInfo(kind, value), {label, tone: 'mut'}, `${kind}:${value}`);
  }
});

test('the backend readiness and job vocabularies are all covered', () => {
  for (const v of ['ready', 'partial', 'indexing', 'needs attention', 'unindexed', 'empty', 'unknown', 'queued', 'running', 'failed', 'interrupted', 'cancelled', 'unsupported — original available']) assert.ok(READINESS[v], v);
  for (const v of ['active', 'queued', 'running', 'cancelling', 'succeeded', 'partial', 'failed', 'cancelled', 'interrupted']) assert.ok(JOB_STATUS[v], v);
  for (const v of ['research', 'coding', 'coding_run', 'document', 'image', 'indexing', 'scheduled']) assert.ok(JOB_KIND[v], v);
  assert.equal(JOB_STATUS.succeeded[1], 'ok');
  assert.equal(JOB_STATUS.failed[1], 'err');
  assert.equal(JOB_STATUS.interrupted[1], 'err');
  assert.equal(JOB_STATUS.cancelled[1], 'mut');
  for (const v of ['queued', 'partial', 'cancelling']) assert.equal(JOB_STATUS[v][1], 'warm', v);
  for (const v of ['active', 'running']) assert.equal(JOB_STATUS[v][1], 'acc', v);
});

test('lookups ignore case and surrounding whitespace', () => {
  assert.deepEqual(statusInfo('report-status', ' Complete '), {label: 'Complete', tone: 'ok'});
  assert.deepEqual(statusInfo('readiness', 'READY'), {label: 'Ready', tone: 'ok'});
});

test('unknown values humanize with the muted tone', () => {
  assert.deepEqual(statusInfo('job-status', 'ready_for_review'), {label: 'Ready for review', tone: 'mut'});
  assert.deepEqual(statusInfo('readiness', 'something-odd'), {label: 'Something odd', tone: 'mut'});
  assert.deepEqual(statusInfo('not-a-kind', 'x'), {label: 'X', tone: 'mut'});
});

test('job phases are humanized', () => {
  assert.deepEqual(statusInfo('job-phase', 'visual_review'), {label: 'Visual review', tone: 'mut'});
  assert.deepEqual(statusInfo('job-phase', 'planning'), {label: 'Planning', tone: 'mut'});
  assert.ok(TONES.includes(statusInfo('job-phase', 'candidate_packaging').tone));
});

test('null and undefined give an empty label', () => {
  assert.deepEqual(statusInfo('readiness', null), {label: '', tone: 'mut'});
  assert.deepEqual(statusInfo('job-status', undefined), {label: '', tone: 'mut'});
});

test('humanize replaces underscores and hyphens and capitalises the first letter', () => {
  assert.equal(humanize('needs_attention'), 'Needs attention');
  assert.equal(humanize('coding-run'), 'Coding run');
  assert.equal(humanize('  ready  '), 'Ready');
  assert.equal(humanize(''), '');
  assert.equal(humanize(null), '');
});
