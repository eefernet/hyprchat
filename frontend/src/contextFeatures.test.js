// Browser side of the stage 2 context features (2026-11-15 audit): the too-big notice and draft hint, and
// the actions in the context breakdown.
import assert from 'node:assert/strict';
import test from 'node:test';
import { attachmentChars, draftFit, draftLine, overflowLine, overflowWarning } from './contextFit.js';
import { anyAction, availableActions, compactLine, forgetLine } from './contextActions.js';
import { fromMessages, meterView, settle } from './contextMeter.js';
import { breakdownView } from './contextBreakdown.js';

const PROSE = 'In district nine the survey team recorded steady output and two minor faults. ';

// ── A request that did not fit ─────────────────────────────────────────────────

test('a reply whose request was cut says how much was needed', () => {
  // Measured: a message of twice an 8,192 window was read as 4,098 tokens and nothing said so.
  const line = overflowLine({ needed: 14070, window: 8192 });
  assert.match(line, /about 14,070 of 8,192 tokens/);
  assert.match(line, /read only part of it/);
  assert.equal(overflowLine(null), '');
  assert.equal(overflowLine({ needed: 0, window: 8192 }), '');
  assert.equal(overflowLine({ needed: 9000 }), '');
});

test('the pill warns after a request that did not fit, also after a reload', () => {
  const reading = fromMessages([{ role: 'user', content: 'x' }, { role: 'assistant', content: 'y', metadata: { context: { used: 14100, window: 8192, measured: true, ratio: 0.7, overflow: 14070 } } }]);
  assert.equal(reading.overflow, 14070);
  const view = meterView(reading, 8192, 85);
  assert.equal(view.tone, 'full');
  assert.match(view.warning, /needed about 14,070 tokens and did not fit/);
  assert.equal(view.percent, 100);                       // more than the window is still drawn as full
  assert.match(breakdownView(reading, { fallbackWindow: 8192 }).warning, /did not fit/);
  assert.equal(settle({ used: 1, working: 0, window: 8192 }, { used: 14100, window: 8192, overflow: 14070 }).overflow, 14070);
  assert.equal('overflow' in settle({ used: 1, working: 0, window: 8192 }, { used: 5000, window: 8192 }), false);
  assert.equal(overflowWarning(0), '');
});

test('a draft that cannot fit is flagged before it is sent', () => {
  const big = PROSE.repeat(770);                         // about 60,000 characters: twice an 8K window
  const fit = draftFit({ text: big, window: 8192, fixed: 3300 });
  assert.ok(fit && fit.window === 8192 && fit.room === 8192 - 3300 - 2048);
  assert.ok(fit.tokens > 13000 && fit.tokens < 16000, `${fit.tokens}`);
  assert.match(draftLine(fit), /about 14,[0-9]{3} tokens/);
  assert.match(draftLine(fit), /read only part of it/);
  assert.match(draftLine(fit), /has room for about 2,844/);
  // A 4,096 window is nearly used up by the instructions and tools alone.
  assert.match(draftLine(draftFit({ text: big, window: 4096, fixed: 3400 })), /has almost no room left for a message/);
});

test('a draft that fits, or a model whose window is not known, gets no note', () => {
  assert.equal(draftFit({ text: 'Hello there', window: 8192 }), null);
  assert.equal(draftFit({ text: PROSE.repeat(100), window: 8192, fixed: 3300 }), null);      // 7,800 characters
  assert.equal(draftFit({ text: PROSE.repeat(770), window: 262144 }), null);
  assert.equal(draftFit({ text: PROSE.repeat(770), window: 0 }), null);                      // a cloud model
  assert.equal(draftFit(), null);
  assert.equal(draftLine(null), '');
});

test('a draft right at the edge is not flagged: the figure is rough', () => {
  // 2,844 tokens of room (8,192 - 3,300 - 2,048). 11,000 characters are about 2,700 tokens of prose.
  assert.equal(draftFit({ text: 'a'.repeat(11000), window: 8192, fixed: 3300 }), null);
  assert.ok(draftFit({ text: 'a'.repeat(16000), window: 8192, fixed: 3300 }));
});

test('attached text counts toward the draft, pictures and staged data do not', () => {
  const pdf = { type: 'pdf', name: 'a.pdf', content: PROSE.repeat(700) };
  assert.ok(draftFit({ text: 'Summarize this', attachments: [pdf], window: 8192 }));
  assert.equal(attachmentChars({ type: 'image', dataUrl: 'x'.repeat(90000) }), 0);
  assert.equal(attachmentChars({ type: 'data', content: 'x'.repeat(90000), sandboxPath: '/root/f.csv' }), 0);
  assert.equal(attachmentChars({ type: 'pdf', error: 'unreadable', content: 'x'.repeat(90000) }), 0);
  assert.equal(attachmentChars({ name: 'notes.txt', content: 'abc' }), 3);
  assert.equal(attachmentChars(null), 0);
});

test('the figure shown uses the chat\'s own token rate when it has one', () => {
  const text = 'a'.repeat(60000);
  assert.equal(draftFit({ text, window: 8192, ratio: 0.6 }).tokens, 12000);
  assert.equal(draftFit({ text, window: 8192 }).tokens, 14600);
  assert.equal(draftFit({ text, window: 8192, ratio: 5 }).tokens, 14600);
});

// ── Actions in the breakdown ───────────────────────────────────────────────────

test('compact now is offered where auto-compaction applies and no reply is running', () => {
  const reading = { used: 9000, window: 32768, parts: { messages: 5000, kept_tools: 400 } };
  assert.deepEqual(availableActions({ reading, live: false, compactPercent: 85, saved: true, summarized: true }), { compact: true, forget: true, summary: true });
  assert.equal(availableActions({ reading, live: true, compactPercent: 85, saved: true }).compact, false);      // a reply is being written
  assert.equal(availableActions({ reading, live: false, compactPercent: 0, saved: true }).compact, false);      // cloud, coding, or switched off
  assert.equal(availableActions({ reading, live: false, compactPercent: 85, saved: false }).compact, false);    // a ghost chat
  assert.equal(availableActions({ reading: null, compactPercent: 85, saved: true }).compact, false);
  assert.equal(anyAction(availableActions({ reading: null, saved: true })), false);
  assert.equal(anyAction(availableActions()), false);
});

test('forgetting tool results is offered only when the chat keeps some', () => {
  assert.equal(availableActions({ reading: { used: 9000, parts: { messages: 9000 } }, saved: true }).forget, false);
  assert.equal(availableActions({ reading: { used: 9000 }, saved: true }).forget, false);
  assert.equal(availableActions({ reading: { used: 9000, parts: { kept_tools: 1 } }, saved: true, live: true }).forget, false);
  assert.equal(availableActions({ reading: { used: 1 }, saved: true, summarized: true, live: true }).summary, true);   // reading it is always safe
});

test('each action says what it did', () => {
  assert.equal(compactLine({ compacted: true, summarized: 14, freed: 9230 }), 'Summarized 14 earlier messages: about 9.2k tokens freed.');
  assert.equal(compactLine({ compacted: true, summarized: 1, freed: 0 }), 'Summarized 1 earlier message.');
  assert.equal(compactLine({ compacted: false, reason: 'There is not enough earlier conversation to summarize yet.' }), 'There is not enough earlier conversation to summarize yet.');
  assert.equal(compactLine({ compacted: false }), 'Nothing was summarized.');
  assert.equal(forgetLine({ removed: 12, freed: 1900 }), 'Forgot 12 kept tool results: about 1.9k tokens freed.');
  assert.equal(forgetLine({ removed: 1, freed: 80 }), 'Forgot 1 kept tool result: about 80 tokens freed.');
  assert.equal(forgetLine({ removed: 0, freed: 0 }), 'This chat keeps no tool results.');
  assert.equal(compactLine(null), '');
});

test('the too-big warning counts bytes, as the server does', async () => {
  const { draftFit } = await import('./contextFit.js');
  const latin = 'a'.repeat(9000), chinese = '汉'.repeat(9000);        // 9,000 bytes against 27,000
  assert.equal(draftFit({ text: latin, window: 8192 }), null);
  const fit = draftFit({ text: chinese, window: 8192 });
  assert.ok(fit && fit.tokens > fit.room, 'a draft three times the size in bytes must be flagged');
});

