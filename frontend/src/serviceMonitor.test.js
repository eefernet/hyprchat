import test from 'node:test';
import assert from 'node:assert/strict';
import {paddedDays, barTone, barTitle, serviceState, MONITOR_DAYS} from './serviceMonitor.js';

const now = new Date('2026-11-19T03:30:00Z');

test('the strip always has ninety days, oldest first, ending today', () => {
  const days = paddedDays([], now);
  assert.equal(days.length, MONITOR_DAYS);
  assert.equal(days.at(-1).day, '2026-11-19');
  assert.equal(days[0].day, '2026-08-22');
  assert.ok(days.every((day, i) => i === 0 || day.day > days[i - 1].day));
});

test('a day the server sent keeps its numbers and a missing day is "no data"', () => {
  const sent = {day: '2026-11-18', ok_pct: 99, degraded_pct: 1, error_pct: 0, total_checks: 288, avg_ms: 4};
  const days = paddedDays([sent], now);
  assert.equal(days.at(-2), sent);
  assert.equal(days.at(-1).total_checks, 0);
  assert.equal(barTone(days.at(-1)), 'none');
});

test('days are UTC dates whatever the local time zone', () => {
  // 23:30 UTC on the 18th is already the 19th east of UTC; the strip must still end on the 18th.
  assert.equal(paddedDays([], new Date('2026-11-18T23:30:00Z')).at(-1).day, '2026-11-18');
});

test('days older than the strip are left out', () => {
  const old = {day: '2026-01-01', ok_pct: 100, degraded_pct: 0, error_pct: 0, total_checks: 10, avg_ms: 1};
  assert.ok(!paddedDays([old], now).includes(old));
});

test('bar colours follow how bad the day was', () => {
  const day = (error_pct, degraded_pct, total_checks = 100) => ({day: 'd', ok_pct: 100 - error_pct - degraded_pct, error_pct, degraded_pct, total_checks, avg_ms: 3});
  assert.equal(barTone(day(0, 0)), 'ok');
  assert.equal(barTone(day(0, 5)), 'some-degraded');
  assert.equal(barTone(day(0, 30)), 'some-degraded');
  assert.equal(barTone(day(0, 31)), 'degraded');
  assert.equal(barTone(day(1, 60)), 'some-down');
  assert.equal(barTone(day(50, 0)), 'some-down');
  assert.equal(barTone(day(51, 0)), 'down');
  assert.equal(barTone(day(80, 0, 0)), 'none');
  assert.equal(barTone(undefined), 'none');
});

test('bar tooltips', () => {
  assert.equal(barTitle({day: '2026-11-18', total_checks: 0}), '2026-11-18\nNo data');
  assert.equal(barTitle({day: '2026-11-18', ok_pct: 98, degraded_pct: 1, error_pct: 1, total_checks: 288, avg_ms: 7}),
    '2026-11-18\n98% ok, 1% degraded, 1% down\n288 checks, avg 7ms');
});

test('a service state has a tone and a word', () => {
  assert.deepEqual(serviceState('ok'), {tone: 'ok', label: 'Operational'});
  assert.deepEqual(serviceState('degraded'), {tone: 'degraded', label: 'Degraded'});
  assert.deepEqual(serviceState('error'), {tone: 'down', label: 'Down'});
  assert.deepEqual(serviceState(undefined), {tone: 'down', label: 'Down'});
  assert.deepEqual(serviceState('ok', true), {tone: 'degraded', label: 'Rate limited'});
});
