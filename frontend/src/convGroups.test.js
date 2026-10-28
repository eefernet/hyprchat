import test from 'node:test';
import assert from 'node:assert/strict';
import {groupByRecency, RECENCY_GROUPS} from './convGroups.js';

// Fixed "now": a local Wednesday at 15:30 so every boundary is unambiguous.
const now = new Date(2026, 9, 21, 15, 30).getTime();
const at = (daysAgo, hour = 10) => {
  const d = new Date(now);
  d.setDate(d.getDate() - daysAgo);
  d.setHours(hour, 0, 0, 0);
  return d.getTime();
};
const conv = (id, ms) => ({id, ms});
const group = (items, key) => items.find(g => g.key === key);
const ids = g => (g ? g.items.map(c => c.id) : []);

test('items land in Today / Yesterday / Previous 7 days / Older by local calendar day', () => {
  const items = [
    conv('t1', at(0, 9)), conv('t2', at(0, 0)),           // earlier today, local midnight
    conv('y1', at(1, 23)), conv('y2', at(1, 0)),          // yesterday evening, yesterday midnight
    conv('w1', at(2, 12)), conv('w2', at(7, 8)),          // 2 days ago, exactly 7 days ago (still in the week)
    conv('o1', at(8, 12)), conv('o2', at(40, 12)),        // 8 days ago, 40 days ago
  ];
  const groups = groupByRecency(items, now, c => c.ms);
  assert.deepEqual(groups.map(g => g.key), ['today', 'yesterday', 'week', 'older']);
  assert.deepEqual(groups.map(g => g.label), ['Today', 'Yesterday', 'Previous 7 days', 'Older']);
  assert.deepEqual(ids(group(groups, 'today')), ['t1', 't2']);
  assert.deepEqual(ids(group(groups, 'yesterday')), ['y1', 'y2']);
  assert.deepEqual(ids(group(groups, 'week')), ['w1', 'w2']);
  assert.deepEqual(ids(group(groups, 'older')), ['o1', 'o2']);
});

test('only non-empty groups are returned, in the fixed order', () => {
  const groups = groupByRecency([conv('o', at(30)), conv('t', at(0))], now, c => c.ms);
  assert.deepEqual(groups.map(g => g.key), ['today', 'older']);
  assert.deepEqual(groupByRecency([], now, c => c.ms), []);
  assert.deepEqual(RECENCY_GROUPS.map(([k]) => k), ['today', 'yesterday', 'week', 'older']);
});

test('order is preserved within a group', () => {
  const items = [conv('a', at(0, 14)), conv('b', at(0, 8)), conv('c', at(0, 11))];
  assert.deepEqual(ids(group(groupByRecency(items, now, c => c.ms), 'today')), ['a', 'b', 'c']);
});

test('unparsable, NaN and zero times fall into Older', () => {
  const items = [conv('nan', NaN), conv('zero', 0), conv('str', 'not a date'), conv('undef', undefined), conv('t', at(0))];
  const groups = groupByRecency(items, now, c => c.ms);
  assert.deepEqual(ids(group(groups, 'older')), ['nan', 'zero', 'str', 'undef']);
  assert.deepEqual(ids(group(groups, 'today')), ['t']);
});

test('a time one millisecond before local midnight is yesterday; future times count as today', () => {
  const midnight = new Date(2026, 9, 21).getTime();
  const groups = groupByRecency([conv('late', midnight - 1), conv('future', now + 3600_000)], now, c => c.ms);
  assert.deepEqual(ids(group(groups, 'yesterday')), ['late']);
  assert.deepEqual(ids(group(groups, 'today')), ['future']);
});
