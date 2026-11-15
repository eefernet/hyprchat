import assert from 'node:assert/strict';
import test from 'node:test';
import { COMPACT_CHOICES, applyUpdate, atStreamStart, compacted, compactionIndex, compactionLimit, estimateTokens, formatTokens, fromMessages, meterView, roomLeft, settle } from './contextMeter.js';

const reply = (content, context, extra = {}) => ({ role: 'assistant', content, metadata: { ...(context ? { context } : {}), ...extra } });
const user = content => ({ role: 'user', content });
const READING = { used: 5800, window: 32768, measured: true, ratio: 0.7 };

test('opening a chat restores the reading its last reply saved', () => {
  // Live 2026-11-13: the pill was cleared on every chat switch and reload and never restored.
  const reading = fromMessages([user('hi'), reply('hello', { used: 900, window: 32768, measured: true, ratio: 0.7 }), user('more'), reply('sure', READING)]);
  assert.deepEqual(reading, { used: 5800, working: 0, window: 32768, measured: true, ratio: 0.7, fixed: 0 });
});

test('metadata stored as a string is read too', () => {
  assert.equal(fromMessages([{ role: 'assistant', content: 'x', metadata: JSON.stringify({ context: READING }) }]).used, 5800);
});

test('replies saved before readings existed fall back to their first call', () => {
  const reading = fromMessages([user('q'), reply('An older answer.', null, { usage_calls: [{ prompt_tokens: 2000, gen_tokens: 900 }, { prompt_tokens: 5000, gen_tokens: 40 }] })]);
  assert.equal(reading.used, 2000 + estimateTokens('An older answer.'));
  assert.equal(reading.measured, false);
  assert.equal(fromMessages([user('q'), reply('no usage at all')]), null);
});

test('sending does not drop the pill to zero', () => {
  // Live 2026-11-13: every send cleared it and it stayed at 0 until the server's first guess.
  const messages = [user('hi'), reply('hello', READING), user('a new question, a little longer than the others'), reply('')];
  const reading = atStreamStart(messages, null);
  assert.equal(reading.used, 5800 + estimateTokens('a new question, a little longer than the others', 0.7));
  assert.equal(reading.measured, false);
  assert.equal(reading.window, 32768);
});

test('regenerating goes back to the reading before the removed reply', () => {
  const earlier = { used: 900, window: 32768, measured: true, ratio: 0.7 };
  const reading = atStreamStart([user('hi'), reply('hello', earlier), user('again'), reply('')], { used: 5800, window: 32768 });
  assert.equal(reading.used, 900 + estimateTokens('again', 0.7));
});

test('a first message in a new chat starts from an estimate', () => {
  const reading = atStreamStart([user('hello there'), reply('')], null);
  assert.equal(reading.used, estimateTokens('hello there'));
  assert.equal(reading.measured, false);
});

test('an estimate can raise the reading but never lower it; a real count replaces it', () => {
  const start = { used: 6000, working: 0, window: 32768, measured: false, ratio: 0.7 };
  const guessed = applyUpdate(start, { used: 5200, working: 300, window: 32768, measured: false });
  assert.equal(guessed.used, 6000);
  assert.equal(guessed.working, 300);
  const real = applyUpdate(guessed, { used: 5650, working: 1400, window: 32768, measured: true });
  assert.deepEqual([real.used, real.working, real.measured], [5650, 1400, true]);
  assert.equal(applyUpdate(real, { used: 5700, working: 0, measured: true }).window, 32768);
});

test('events from an older server change nothing', () => {
  const start = { used: 6000, working: 0, window: 32768, measured: true, ratio: 0.7 };
  assert.equal(applyUpdate(start, { prompt_tokens: 100, gen_tokens: 5 }), start);
});

test('when the reply finishes the working part goes and the number stays', () => {
  // Live 2026-11-13: the pill fell after every reply because it had counted thinking and tool results.
  const during = { used: 6100, working: 2400, window: 32768, measured: true, ratio: 0.7 };
  const after = settle(during, { used: 6180, working_peak: 2400, window: 32768, measured: true, ratio: 0.71 });
  assert.deepEqual(after, { used: 6180, working: 0, window: 32768, measured: true, ratio: 0.71, fixed: 0 });
  assert.equal(settle(during, null).working, 0);
  assert.equal(settle(during, null).used, 6100);
});

test('compaction lowers the reading and lets the next estimate through', () => {
  const start = { used: 26000, working: 0, window: 32768, measured: true, ratio: 0.7 };
  const smaller = compacted(start, { freed: 18000 });
  assert.equal(smaller.used, 8000);
  assert.equal(applyUpdate(smaller, { used: 7600, working: 0, window: 32768, measured: false }).used, 7600);
});

test('the pill view splits conversation and working and picks a tone', () => {
  const view = meterView({ used: 16384, working: 8192, window: 32768, measured: true }, 0);
  assert.equal(view.usedShare, 0.5);
  assert.equal(view.workingShare, 0.25);
  assert.equal(view.tone, 'high');
  assert.match(view.tooltip, /16,384 tokens of 32,768 \(50%\)/);
  assert.match(view.tooltip, /\+8,192/);
  assert.equal(meterView({ used: 30000, working: 0, window: 32768, measured: true }).tone, 'full');
  assert.equal(meterView({ used: 100, working: 0, window: 0, measured: true }, 0).tone, 'unknown');
  assert.equal(meterView(null, 32768).window, 32768);
  assert.match(meterView({ used: 5, working: 0, window: 100, measured: false }).tooltip, /Estimate/);
});

test('token counts are short', () => {
  assert.equal(formatTokens(262144), '262.1k');
  assert.equal(formatTokens(950), '950');
});

test('the summary line sits before the first message after the cut', () => {
  const messages = [{ id: 11 }, { id: 12 }, { id: 13 }, { id: 14 }, {}];
  assert.equal(compactionIndex(messages, 12), 2);
  assert.equal(compactionIndex(messages, 0), -1);
  assert.equal(compactionIndex(messages, 5), -1);            // nothing in view is summarized
  assert.equal(compactionIndex([{ id: 11 }, { id: 12 }], 12), -1);
});

test('compaction fires at the chosen share but always leaves room for a reply', () => {
  // The same table as backend/tests/test_chat_context_unit.py: the two sides must agree.
  const limits = window => COMPACT_CHOICES.map(percent => compactionLimit(window, percent).limit);
  assert.deepEqual(limits(262144), [222822, 235930, 249037, 256901, 258048]);
  assert.deepEqual(limits(32768), [27853, 28672, 28672, 28672, 28672]);
  assert.deepEqual(limits(8192), [6144, 6144, 6144, 6144, 6144]);
  assert.equal(compactionLimit(262144, 98).capped, false);
  assert.equal(compactionLimit(262144, 99).capped, true);
  assert.deepEqual(compactionLimit(0, 95), { limit: 0, capped: false });
});

test('the pill tooltip says when compaction fires, only while it is switched on', () => {
  const reading = { used: 5000, working: 0, window: 262144, measured: true };
  assert.match(meterView(reading, 0, 85).tooltip, /Auto-compaction at 222,822 tokens/);
  assert.doesNotMatch(meterView(reading, 0, 0).tooltip, /Auto-compaction/);
});

test('a window too small for the fixed prompt is called out', () => {
  // Live 2026-11-13: 3,554 tokens of instructions and tools in a 4,096 window; replies were cut off mid-sentence.
  const view = meterView({ used: 3735, working: 0, window: 4096, measured: true, fixed: 3554 }, 0, 98);
  assert.match(view.warning, /Only about 542 tokens are left/);
  assert.equal(view.tone, 'full');
  assert.ok(view.tooltip.startsWith('Only about 542'));
  assert.match(view.tooltip, /Instructions and tools, sent every turn: 3,554/);
  assert.equal(meterView({ used: 5000, working: 0, window: 32768, measured: true, fixed: 3554 }).warning, '');
  assert.equal(meterView({ used: 5000, working: 0, window: 4096, measured: true }).warning, '');     // fixed cost not known yet
  assert.deepEqual(roomLeft(4096), { room: 596, tight: true });
  assert.deepEqual(roomLeft(8192), { room: 4692, tight: false });
  assert.deepEqual(roomLeft(0), { room: 0, tight: false });
});

test('the fixed cost travels with the reading', () => {
  const saved = fromMessages([{ role: 'assistant', content: 'x', metadata: { context: { used: 3700, window: 4096, measured: true, ratio: 0.7, fixed: 3554 } } }]);
  assert.equal(saved.fixed, 3554);
  assert.equal(applyUpdate(saved, { used: 3720, working: 10, window: 4096, measured: true }).fixed, 3554);
  assert.equal(settle(saved, { used: 3800, window: 4096, measured: true }).fixed, 3554);
});
