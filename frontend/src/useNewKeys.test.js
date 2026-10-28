import test from 'node:test';
import assert from 'node:assert/strict';
import {nextEntering} from './useNewKeys.js';

const fresh = {resetKey: undefined, keys: [], entered: new Map()};
const flagged = s => [...s.entered.keys()];

test('a conversation load marks everything as seen without flagging rows', () => {
  const s = nextEntering(fresh, ['a', 'b', 'c'], 'conv1', 1000);
  assert.deepEqual(flagged(s), []);
  assert.deepEqual(s.keys, ['a', 'b', 'c']);
});

test('appended rows are flagged, kept for the ttl, then dropped', () => {
  let s = nextEntering(fresh, ['a', 'b'], 'conv1', 1000);
  s = nextEntering(s, ['a', 'b', 'c'], 'conv1', 1100);
  assert.deepEqual(flagged(s), ['c']);
  s = nextEntering(s, ['a', 'b', 'c'], 'conv1', 1300);
  assert.deepEqual(flagged(s), ['c'], 'still within ttl on a re-render');
  s = nextEntering(s, ['a', 'b', 'c'], 'conv1', 1600);
  assert.deepEqual(flagged(s), []);
});

test('a key change on an existing row (server id arrives) does not re-flag it', () => {
  let s = nextEntering(fresh, ['a'], 'conv1', 1000);
  s = nextEntering(s, ['a', 'conv1:2'], 'conv1', 1100);
  assert.deepEqual(flagged(s), ['conv1:2']);
  s = nextEntering(s, ['a', 'conv1:msg-9'], 'conv1', 1700);
  assert.deepEqual(flagged(s), []);
});

test('prepending older rows does not flag the newest rows again', () => {
  let s = nextEntering(fresh, ['x', 'y'], 'conv1', 1000);
  s = nextEntering(s, ['o1', 'o2', 'o3', 'x', 'y'], 'conv1', 1100);
  assert.deepEqual(flagged(s), []);
});

test('switching conversations resets the baseline', () => {
  let s = nextEntering(fresh, ['a'], 'conv1', 1000);
  s = nextEntering(s, ['p', 'q'], 'conv2', 1100);
  assert.deepEqual(flagged(s), []);
  s = nextEntering(s, ['p', 'q', 'r'], 'conv2', 1200);
  assert.deepEqual(flagged(s), ['r']);
});
