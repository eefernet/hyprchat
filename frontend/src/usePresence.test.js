import test from 'node:test';
import assert from 'node:assert/strict';
import {parseMs, motionDuration} from './usePresence.js';

test('parseMs reads CSS time tokens', () => {
  assert.equal(parseMs('120ms'), 120);
  assert.equal(parseMs(' .2s '), 200);
  assert.equal(parseMs('0ms'), 0);
  assert.ok(Number.isNaN(parseMs('')));
  assert.ok(Number.isNaN(parseMs('fast')));
});

test('motionDuration falls back without a document', () => {
  assert.equal(motionDuration('--hc-dur-1', 150), 150);
});
