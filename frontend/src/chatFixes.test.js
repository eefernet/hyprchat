// Browser-side fixes from the 2026-11-15 chat audit: the window the context pill shows, how a reply's
// stream ended, history trimming, editing a message with a PDF, placeholder titles, refused requests.
import assert from 'node:assert/strict';
import test from 'node:test';
import { chatWindow, forDisplay, forRequest, fromMessages, atStreamStart, keptReading, liveSpeed, meterView, settle, staleWindow, applyUpdate } from './contextMeter.js';
import { activityStatus, deliveryStatus, replyOutcome } from './runState.js';
import { roughTokens, trimHistory } from './historyTrim.js';
import { attachmentText, editedMessage } from './editedMessage.js';
import { cleanTitle, needsTitle, provisionalTitle, renameTo } from './chatTitles.js';
import { continueKind, cutOffLine, needsRetry, requestError } from './continueReply.js';

const user = (content, extra = {}) => ({ role: 'user', content, ...extra });
const reply = (content, metadata = {}) => ({ role: 'assistant', content, metadata });

// ── The window the pill shows ──────────────────────────────────────────────────

test('the pill shows the new model\'s window after a switch', () => {
  // The reading's own window won over the setting, so a chat moved from a 262K model to a 32K one
  // (or to a cloud model) kept showing 262K until the next reply.
  const taken = fromMessages([user('hi'), reply('hello', { model: 'qwen3.8:27b', context: { used: 5000, window: 262144, asked: 262144, measured: true, ratio: 0.7 } })]);
  assert.equal(taken.model, 'qwen3.8:27b');
  assert.equal(meterView(forDisplay(taken, { model: 'qwen3.8:27b', asked: 262144 }), 262144).window, 262144);
  assert.equal(meterView(forDisplay(taken, { model: 'gemma-3:27b', asked: 32768 }), 32768).window, 32768);
  const cloud = meterView(forDisplay(taken, { model: 'anthropic:claude-opus-5', asked: 0 }), 0);
  assert.equal(cloud.window, 0);
  assert.equal(cloud.percent, null);
  assert.equal(cloud.used, 5000);                       // what the chat holds does not change with the model
});

test('the pill follows a changed context size for the same model', () => {
  const taken = fromMessages([user('hi'), reply('hello', { model: 'm', context: { used: 5000, window: 32768, asked: 32768, measured: true, ratio: 0.7 } })]);
  assert.equal(staleWindow(taken, { model: 'm', asked: 32768 }), false);
  assert.equal(staleWindow(taken, { model: 'm', asked: 131072 }), true);
  assert.equal(meterView(forDisplay(taken, { model: 'm', asked: 131072 }), 131072).window, 131072);
});

test('a model with a smaller limit keeps its real window', () => {
  // Asked 32,768, the model has 8,192: the reply ran at 8,192 and the setting did not change.
  const taken = fromMessages([user('hi'), reply('hello', { model: 'small', context: { used: 3000, window: 8192, asked: 32768, measured: true, ratio: 0.7 } })]);
  assert.equal(meterView(forDisplay(taken, { model: 'small', asked: 32768 }), 8192).window, 8192);
  assert.deepEqual(chatWindow('32768', 262144, 8192), { asked: 32768, window: 8192 });
});

test('a reading without a model or an asked size is shown as it is', () => {
  const old = fromMessages([user('hi'), reply('hello', { context: { used: 900, window: 32768, measured: true, ratio: 0.7 } })]);
  assert.equal('model' in old || 'asked' in old, false);
  assert.equal(forDisplay(old, { model: 'anything', asked: 8192 }), old);
  assert.equal(staleWindow(old, null), false);
  assert.equal(staleWindow(null, { model: 'm' }), false);
  // "auto" is routed to some model on the server: its name never matches, and that is not a switch.
  const routed = fromMessages([user('hi'), reply('hello', { model: 'qwen3.8:27b', context: { used: 900, window: 32768, measured: true } })]);
  assert.equal(staleWindow(routed, { model: 'auto', asked: 0 }), false);
});

test('the pill and the request read the per-model context size the same way', () => {
  // The pill used parseInt with no fallback (an unreadable value gave it no window); the request used
  // parseFloat and fell back to nothing.
  assert.deepEqual(chatWindow(undefined, 262144), { asked: 262144, window: 262144 });
  assert.deepEqual(chatWindow('', 262144), { asked: 262144, window: 262144 });
  assert.deepEqual(chatWindow('default', 32768), { asked: 32768, window: 32768 });
  assert.deepEqual(chatWindow('16384', 262144), { asked: 16384, window: 16384 });
  assert.deepEqual(chatWindow('nonsense', 32768), { asked: 32768, window: 32768 });
  assert.deepEqual(chatWindow('-5', 32768), { asked: 32768, window: 32768 });
  assert.deepEqual(chatWindow('8192', 0, 0), { asked: 8192, window: 8192 });
  assert.deepEqual(chatWindow(undefined, 0), { asked: 0, window: 0 });
});

test('a reply about to run with another model forgets the old window but not what the chat holds', () => {
  const taken = { used: 5000, working: 0, window: 262144, measured: true, ratio: 0.7, model: 'big', asked: 262144 };
  const next = forRequest(taken, { model: 'small', asked: 32768 });
  assert.equal(next.window, 0);
  assert.equal(next.used, 5000);
  assert.equal(next.model, 'small');
  assert.equal(next.asked, 32768);
  assert.equal(forRequest(taken, { model: 'big', asked: 262144 }).window, 262144);
  assert.equal(forRequest(undefined, { model: 'x' }), undefined);
  // The first real count of the new reply brings its own window.
  assert.equal(applyUpdate(next, { used: 5100, working: 300, window: 32768, measured: true }).window, 32768);
});

// ── Endings without a final reading ────────────────────────────────────────────

test('the pill does not dip on the next send after a stopped reply', () => {
  // The browser's copy of a stopped (or ghost) reply had no reading, so the next send started from a
  // guess of the message text alone: about 3k tokens of instructions and tools vanished for a moment.
  const live = applyUpdate(atStreamStart([user('hi'), reply('')], null), { used: 4200, working: 900, window: 32768, measured: true, fixed: 3300 });
  const kept = keptReading(settle(live, null));
  assert.deepEqual(kept, { used: 4200, window: 32768, measured: false, ratio: 0.75, fixed: 3300 });
  const next = atStreamStart([user('hi'), reply('half an answer', { context: kept }), user('and?'), reply('')], null);
  assert.ok(next.used >= 4200, `${next.used} dipped below what the chat already held`);
  assert.equal(keptReading(null), null);
  assert.equal(keptReading({ used: 0, window: 8192 }), null);
});

test('a stopped reply leaves no working context or live split behind', () => {
  const live = applyUpdate({ used: 4000, working: 0, window: 32768, measured: true, ratio: 0.7 }, { used: 4200, working: 2500, window: 32768, measured: true, parts: { messages: 4200, tool_calls: 2500 } });
  const settled = settle(live, null);
  assert.equal(settled.working, 0);
  assert.equal('parts' in settled, false);
  assert.equal(settled.used, 4200);
});

test('a finished reading keeps the model and the size it was taken with', () => {
  const settled = settle({ used: 1, working: 5, window: 0, measured: false, ratio: 0.75, model: 'm', asked: 32768 }, { used: 4300, window: 8192, asked: 32768, measured: true, ratio: 0.7 });
  assert.equal(settled.model, 'm');
  assert.equal(settled.asked, 32768);
  assert.equal(settled.window, 8192);
});

test('tokens per second are shown while the reply streams', () => {
  // The pill's speed was set only when the reply ended, the moment the pill stops showing it.
  let sample = liveSpeed(null, 10, 1000);
  assert.equal(sample.speed, 0);
  sample = liveSpeed(sample, 30, 1400);                 // under a second: keep waiting
  assert.equal(sample.speed, 0);
  sample = liveSpeed(sample, 150, 3000);
  assert.equal(sample.speed, 70);
  sample = liveSpeed(sample, 157, 4000);
  assert.equal(sample.speed, 7);
  sample = liveSpeed(sample, 168, 6000);                // slow models keep one decimal
  assert.equal(sample.speed, 5.5);
  assert.equal(liveSpeed(sample, 4, 6200).speed, 0);    // a new model call counts from zero again
});

// ── How the stream ended ───────────────────────────────────────────────────────

test('a reply cut by a lost connection is not shown as finished', () => {
  // The stream closed with no `done` (a restart mid-reply): it was saved as completed, with no Continue.
  const outcome = replyOutcome({ stopped: false, failed: false, ended: false });
  assert.equal(outcome, 'interrupted');
  assert.equal(deliveryStatus(outcome), 'interrupted');
  assert.equal(activityStatus(outcome), 'interrupted');
  const cut = { role: 'assistant', content: 'Half an answer', _activityStatus: activityStatus(outcome), metadata: { delivery_status: deliveryStatus(outcome) } };
  assert.equal(continueKind(cut, true), 'interrupted');
});

test('each ending of a reply has its own outcome', () => {
  assert.equal(replyOutcome({ stopped: true, ended: true }), 'stopped');
  assert.equal(replyOutcome({ failed: true, ended: true }), 'failed');
  assert.equal(replyOutcome({ ended: true }), 'completed');
  assert.equal(replyOutcome({ ended: true, roundLimit: 40 }), 'rounds');
  assert.equal(replyOutcome({ ended: true, truncated: true }), 'cutoff');
  assert.equal(replyOutcome({ ended: true, startedJob: true }), 'job');
  assert.equal(replyOutcome(), 'interrupted');
  assert.equal(deliveryStatus('stopped'), 'interrupted');
  assert.equal(deliveryStatus('failed'), 'failed');
  for (const finished of ['completed', 'rounds', 'cutoff', 'job']) {
    assert.equal(deliveryStatus(finished), 'completed');
    assert.equal(activityStatus(finished), 'completed');
  }
  assert.equal(activityStatus('stopped'), 'stopped');
});

// ── Trimming with compaction off ───────────────────────────────────────────────

test('trimming never leaves a reply without its question', () => {
  const chat = [user('a'.repeat(400)), reply('b'.repeat(400)), user('c'.repeat(400)), reply('d'.repeat(400)), user('e'.repeat(40))];
  const kept = trimHistory(chat, 300);                  // 410 rough tokens in all
  assert.equal(kept[0].role, 'user');
  assert.deepEqual(kept.map(message => message.content[0]), ['c', 'd', 'e']);
  assert.ok(roughTokens(kept) <= 300);
});

test('a chat that fits is sent whole and the last exchange is never dropped', () => {
  const chat = [user('one'), reply('two'), user('three')];
  assert.equal(trimHistory(chat, 1000), chat);
  assert.equal(trimHistory(chat, 0), chat);
  const huge = [user('x'.repeat(4000)), reply('y'.repeat(4000)), user('z'.repeat(4000))];
  assert.equal(trimHistory(huge, 10).length >= 1, true);
  assert.equal(trimHistory(huge, 10).at(-1), huge.at(-1));
  assert.deepEqual(trimHistory([], 10), []);
});

// ── Editing a message that had a PDF ───────────────────────────────────────────

test('editing a message keeps its PDF', () => {
  // The bubble kept the PDF chip while the model no longer got the text.
  const pdf = '\n\n📄 **report.pdf** (3 pages):\nRevenue rose 4% in Q3.';
  const original = { role: 'user', content: 'Summarize this', _fullContent: `Summarize this${pdf}`, metadata: { pdfs: [{ name: 'report.pdf', pages: 3 }] } };
  const edited = editedMessage(original, 'Summarize this in two lines');
  assert.equal(edited.content, 'Summarize this in two lines');
  assert.equal(edited._fullContent, `Summarize this in two lines${pdf}`);
  assert.deepEqual(edited.metadata, original.metadata);
});

test('after a reload the PDF text comes from the saved copy', () => {
  const original = { role: 'user', content: 'Check', metadata: { pdfs: [{ name: 'a.pdf', pages: 1 }], model_content: 'Check\n\n📄 **a.pdf** (1 pages):\nBody' } };
  assert.equal(editedMessage(original, 'Check again')._fullContent, 'Check again\n\n📄 **a.pdf** (1 pages):\nBody');
});

test('a message that was only a PDF keeps it when text is added', () => {
  const original = { role: 'user', content: '', _fullContent: '📄 **a.pdf** (1 pages):\nBody', metadata: { pdfs: [{ name: 'a.pdf', pages: 1 }] } };
  assert.equal(editedMessage(original, 'What is this?')._fullContent, 'What is this?\n\n📄 **a.pdf** (1 pages):\nBody');
});

test('an ordinary message is edited to exactly the new text', () => {
  const plain = editedMessage({ role: 'user', content: 'Hello' }, 'Hello there');
  assert.deepEqual(plain, { role: 'user', content: 'Hello there' });
  const images = editedMessage({ role: 'user', content: 'Look', _fullContent: 'Look', _images: ['abc'], metadata: { images: [{ name: 'x.png' }] } }, 'Look closer');
  assert.deepEqual(images, { role: 'user', content: 'Look closer', metadata: { images: [{ name: 'x.png' }] }, _images: ['abc'] });
  assert.equal(attachmentText({ content: 'Changed since', _fullContent: 'Something else entirely' }), '');
  assert.equal(attachmentText(null), '');
});

// ── Titles ─────────────────────────────────────────────────────────────────────

test('a chat whose first reply was stopped still gets a title later', () => {
  const first = user('How do I rotate a PDF in the terminal on macOS Sequoia?');
  const chat = { title: provisionalTitle(first), messages: [first, reply('half'), user('go on'), reply('done')] };
  assert.equal(chat.title.length, 40);
  assert.equal(needsTitle(chat), true);
  assert.equal(needsTitle({ ...chat, title: 'Rotating PDFs on macOS' }), false);
  assert.equal(needsTitle({ title: 'New Chat', messages: [] }), true);
  assert.equal(needsTitle({ title: '', messages: [] }), true);
  assert.equal(needsTitle(undefined), true);
});

test('a chat named after a PDF is still a placeholder, and a long chat is left alone', () => {
  const first = user('', { metadata: { pdfs: [{ name: 'q3-report.pdf' }, { name: 'notes.pdf' }] } });
  assert.equal(provisionalTitle(first), 'q3-report.pdf, notes.pdf');
  assert.equal(needsTitle({ title: 'q3-report.pdf, notes.pdf', messages: [first, reply('ok')] }), true);
  const long = { title: 'hello', messages: [user('hello'), ...Array.from({ length: 10 }, (_, index) => (index % 2 ? user('q') : reply('a')))] };
  assert.equal(needsTitle(long), false);                // a name the user may simply like: stop trying
});

// ── A refused request ──────────────────────────────────────────────────────────

test('a refused request shows the server\'s words, not a line of JSON', () => {
  assert.equal(requestError(404, '{"detail":"Conversation not found"}'), 'Conversation not found (HTTP 404)');
  assert.equal(requestError(422, '{"detail":[{"msg":"Field required","loc":["body","model"]}]}'), 'Field required (HTTP 422)');
  assert.equal(requestError(502, 'Bad Gateway'), 'Bad Gateway (HTTP 502)');
  assert.equal(requestError(500, ''), 'The server refused the request (HTTP 500)');
  assert.ok(requestError(500, 'x'.repeat(2000)).length < 330);
});

// ── Rename, and Retry (stage 3) ────────────────────────────────────────────────

test('a chat can be renamed, and an empty or unchanged name saves nothing', () => {
  const chat = { id: 'c', title: 'Old name' };
  assert.deepEqual(renameTo(chat, '  Trip   planning  '), { title: 'Trip planning' });
  assert.equal(renameTo(chat, '   '), null);
  assert.equal(renameTo(chat, 'Old name'), null);
  assert.equal(cleanTitle('x'.repeat(200)).length, 80);
  // A renamed chat no longer looks like a placeholder, so the title model leaves it alone.
  const first = user('How do I rotate a PDF in the terminal on macOS Sequoia?');
  assert.equal(needsTitle({ title: 'Rotating PDFs', messages: [first, reply('ok')] }), false);
});

test('a reply that failed with nothing written offers Retry', () => {
  // It showed nothing at all: Continue needs text, and Regenerate was in the toolbar that appears on hover.
  const empty = { role: 'assistant', content: '', metadata: { delivery_status: 'interrupted' } };
  assert.equal(continueKind(empty, true), '');
  assert.equal(needsRetry(empty, true), true);
  assert.equal(needsRetry({ role: 'assistant', content: '_[No response — model stopped]_', metadata: { delivery_status: 'failed' } }, true), true);
  assert.equal(cutOffLine('retry'), 'This reply ended before anything was written.');
});

test('Retry is not offered where Continue is, on a finished reply, or further up the chat', () => {
  const partial = { role: 'assistant', content: 'Half an answer', metadata: { delivery_status: 'interrupted' } };
  assert.equal(needsRetry(partial, true), false);                         // Continue covers it
  assert.equal(needsRetry({ role: 'assistant', content: 'Done.', metadata: { delivery_status: 'completed' } }, true), false);
  assert.equal(needsRetry({ role: 'assistant', content: '', metadata: { delivery_status: 'failed' } }, false), false);
  assert.equal(needsRetry({ role: 'assistant', content: '', isS: true, metadata: { delivery_status: 'running' } }, true), false);
  assert.equal(needsRetry({ role: 'user', content: '' }, true), false);
});
