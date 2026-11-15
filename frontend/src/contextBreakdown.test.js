import assert from 'node:assert/strict';
import test from 'node:test';
import { breakdownView, percentText } from './contextBreakdown.js';
import { applyUpdate, atStreamStart, compacted, fromMessages, meterView, settle } from './contextMeter.js';

const KEPT = { instructions: 2300, tools: 1700, memory: 0, skills: 0, summary: 0, messages: 1148 };
const READING = { used: 5148, working: 0, window: 262144, measured: true, ratio: 0.78, fixed: 4000, parts: KEPT,
  peak: { total: 15069, parts: { ...KEPT, excerpts: 2676, tool_calls: 6991, reply: 254 } } };
const LIVE = { ...READING, working: 9921, parts: { ...KEPT, excerpts: 2676, tool_calls: 6991, reply: 254 } };
const tokens = view => Object.fromEntries(view.rows.map(row => [row.key, row.tokens]));
const sum = view => view.rows.reduce((total, row) => total + row.tokens, 0);

test('the rows add up to the number in the header', () => {
  const idle = breakdownView(READING, { compactPercent: 85 });
  assert.equal(idle.total, 5148);
  assert.equal(sum(idle), 5148);
  assert.deepEqual(idle.rows.map(row => row.key), ['instructions', 'tools', 'messages']);      // empty rows are left out
  assert.equal(idle.free, 262144 - 5148);
  assert.equal(idle.compactAt, 222822);
  assert.equal(idle.legacy, false);
});

test('while a reply runs the header is everything in the window, tool work included', () => {
  // Live 2026-11-14: twelve code runs, and the pill said 5.1k with a small "+9.9k" beside it.
  const view = breakdownView(LIVE, { live: true });
  assert.equal(view.total, 15069);
  assert.equal(sum(view), 15069);
  assert.equal(tokens(view).tool_calls, 6991);
  assert.equal(view.rows.find(row => row.key === 'tool_calls').kept, false);
  assert.equal(view.peak, null);                       // the peak line is for after the reply
  assert.equal(meterView(LIVE).total, 15069);
});

test('after the reply the tool work is gone from the rows and named in the peak line', () => {
  const view = breakdownView(READING);
  assert.equal(tokens(view).tool_calls, undefined);
  assert.equal(view.peak.total, 15069);
  assert.equal(view.peak.dropped, 15069 - 5148);
  assert.deepEqual(view.peak.rows.map(row => [row.key, row.tokens]), [['excerpts', 2676], ['tool_calls', 6991]]);
  // A category that barely registered is not listed.
  assert.deepEqual(breakdownView({ ...READING, peak: { total: 6100, parts: { ...KEPT, tool_calls: 40, reply: 912 } } }).peak.rows, []);
  // A reply that only wrote its answer has nothing worth a line.
  assert.equal(breakdownView({ ...READING, peak: { total: 5210, parts: { ...KEPT, reply: 62 } } }).peak, null);
});

test('tool results kept from earlier replies have their own row', () => {
  // Since 2026-11-15 a reply's tool results are sent again on later turns, so they stay in the number.
  const view = breakdownView({ ...READING, used: 8148, parts: { ...KEPT, kept_tools: 3000 } });
  assert.equal(tokens(view).kept_tools, 3000);
  assert.equal(view.rows.find(row => row.key === 'kept_tools').kept, true);
  assert.equal(sum(view), 8148);
});

test('a message typed since the reading was saved goes to Messages', () => {
  const view = breakdownView({ ...READING, used: 5400, measured: false });
  assert.equal(sum(view), 5400);
  assert.equal(tokens(view).messages, 1148 + 252);
  assert.equal(view.estimate, true);
});

test('a split that no longer fits is scaled rather than shown wrong', () => {
  const view = breakdownView({ ...READING, used: 3000 });
  assert.equal(sum(view), 3000);
  assert.ok(view.rows.every(row => row.tokens >= 0));
});

test('a reply saved before the split existed shows what is known', () => {
  const view = breakdownView({ used: 5800, working: 0, window: 32768, measured: true, fixed: 3554 });
  assert.equal(view.legacy, true);
  assert.deepEqual(tokens(view), { fixed: 3554, conversation: 2246 });
  const running = breakdownView({ used: 5800, working: 1400, window: 32768, measured: true, fixed: 3554 }, { live: true });
  assert.deepEqual(tokens(running), { fixed: 3554, conversation: 2246, working: 1400 });
  assert.equal(breakdownView(null, { fallbackWindow: 32768 }).empty, true);
});

test('a cloud model shows tokens and no percentages', () => {
  const view = breakdownView({ ...READING, window: 0 }, { compactPercent: 85 });
  assert.equal(view.percent, null);
  assert.equal(view.free, 0);
  assert.equal(view.compactAt, 0);
});

test('a window too small for the standing prompt is called out', () => {
  const view = breakdownView({ used: 3735, working: 0, window: 4096, measured: true, fixed: 3554,
    parts: { instructions: 2000, tools: 1554, messages: 181 } });
  assert.match(view.warning, /Only about 542 tokens are left/);
  assert.equal(breakdownView(READING).warning, '');
});

test('small shares are still readable', () => {
  assert.equal(percentText(0.0004), '<0.1%');
  assert.equal(percentText(0.0088), '0.9%');
  assert.equal(percentText(0.943), '94%');
});

test('the split travels with the reading and is dropped when it stops describing it', () => {
  const message = { role: 'assistant', content: 'x', metadata: { context: READING } };
  const restored = fromMessages([message]);
  assert.deepEqual(restored.parts, KEPT);
  assert.equal(restored.peak.total, 15069);
  assert.equal(atStreamStart([message, { role: 'user', content: 'next' }], null).peak, undefined);   // that was the previous reply
  const during = applyUpdate(restored, { used: 5200, working: 3000, window: 262144, measured: true, parts: LIVE.parts });
  assert.equal(during.parts.tool_calls, 6991);
  assert.equal(applyUpdate(during, { used: 5200, working: 10, measured: true }).parts, undefined);     // an event without a split
  assert.deepEqual(settle(during, READING).parts, KEPT);
  assert.equal(settle(during, null).parts, undefined);                                               // a stopped reply
});

test('room made by shortening tool results does not lower the conversation', () => {
  const reading = { used: 26000, working: 4000, window: 32768, measured: true };
  assert.equal(compacted(reading, { summarized: 0, freed: 2000, freed_history: 0, freed_working: 2000 }), reading);
  assert.equal(compacted(reading, { summarized: 0, freed: 2000 }), reading);
  assert.equal(compacted(reading, { summarized: 3, freed: 9000, freed_history: 7000, freed_working: 2000 }).used, 19000);
  assert.equal(compacted(reading, { freed: 18000 }).used, 8000);                                      // an older server
});
