import test from 'node:test';
import assert from 'node:assert/strict';
import {shortAgo, fullWhen} from './relativeTime.js';

const NOW = new Date(2026, 10, 19, 15, 0, 0).getTime();          // Thu 19 Nov 2026, 15:00 local
const ago = ms => NOW - ms;

test('a chat row shows how long ago in a few characters', () => {
  assert.equal(shortAgo(ago(20e3), NOW), 'now');
  assert.equal(shortAgo(NOW + 5e3, NOW), 'now');                   // a clock slightly ahead is not "-1m"
  assert.equal(shortAgo(ago(12 * 60e3), NOW), '12m');
  assert.equal(shortAgo(ago(59 * 60e3 + 59e3), NOW), '59m');
  assert.equal(shortAgo(ago(3 * 3600e3), NOW), '3h');
  assert.equal(shortAgo(ago(23 * 3600e3), NOW), '23h');
});

test('older chats show the weekday, then the date, then the year', () => {
  assert.equal(shortAgo(new Date(2026, 10, 17, 9).getTime(), NOW), 'Tue');
  assert.equal(shortAgo(new Date(2026, 10, 13, 9).getTime(), NOW), 'Fri');      // six days ago
  assert.equal(shortAgo(new Date(2026, 10, 3, 9).getTime(), NOW), '3 Nov');
  assert.equal(shortAgo(new Date(2025, 11, 24, 9).getTime(), NOW), '24 Dec 2025');
});

test('a row without a usable time shows nothing', () => {
  for (const bad of [NaN, 0, null, undefined, 'x']) assert.equal(shortAgo(bad, NOW), '');
  assert.equal(fullWhen(NaN), '');
  assert.ok(fullWhen(NOW).length > 8);
});
