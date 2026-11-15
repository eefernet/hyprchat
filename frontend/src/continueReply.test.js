import assert from 'node:assert/strict';
import test from 'node:test';
import { CONTINUE_WORK, continueKind, continueText, cutOffLine, roundLimit, withFailureNote } from './continueReply.js';
import { looksBinary } from './fileKinds.js';
import { runIsLive } from './runState.js';

const reply = (content, metadata = {}, extra = {}) => ({ role: 'assistant', content, metadata, ...extra });

test('a reply that hit the output limit, was stopped or failed part-way can be continued', () => {
  assert.equal(continueKind(reply('Half an ans', { truncated: true }), true), 'length');
  assert.equal(continueKind(reply('Half an ans', { delivery_status: 'interrupted' }), true), 'interrupted');   // could not be continued before
  assert.equal(continueKind(reply('Half an ans', {}, { _activityStatus: 'stopped' }), true), 'interrupted');
  assert.equal(continueKind(reply('Half\n\n_[interrupted: Context window full]_', { delivery_status: 'failed' }), true), 'interrupted');
  assert.equal(continueKind(reply('x', JSON.stringify({ truncated: true })), true), 'length');
});

test('nothing is offered when there is nothing to carry on from', () => {
  assert.equal(continueKind(reply('A whole answer.', { delivery_status: 'completed', truncated: false }), true), '');
  assert.equal(continueKind(reply('Half', { truncated: true }), false), '');                 // not the latest reply
  assert.equal(continueKind(reply('Half', { truncated: true }, { isS: true }), true), '');   // still being written
  assert.equal(continueKind(reply('_[No response — model crashed]_', { delivery_status: 'failed' }), true), '');
  assert.equal(continueKind(reply('', { delivery_status: 'interrupted' }), true), '');
  assert.equal(continueKind({ role: 'user', content: 'hi', metadata: { truncated: true } }, true), '');
});

test('after a Continue that finished, the button does not come back', () => {
  // The browser kept the old flag in its copy of the metadata; the finished state now overrides it.
  const finished = reply('Half an answer, now whole.', { truncated: false, delivery_status: 'completed' }, { _activityStatus: 'completed' });
  assert.equal(continueKind(finished, true), '');
});

test('the model carries on from the reply, not from the note under it', () => {
  assert.equal(continueText('Half an answer\n\n_[interrupted: Context window full: about 3,369 tokens]_'), 'Half an answer');
  assert.equal(continueText('Half\n\n```\n⚠ connection lost\n```'), 'Half');
  assert.equal(continueText('Keep _[interrupted: this]_ in the middle'), 'Keep _[interrupted: this]_ in the middle');
  assert.equal(continueText(null), '');
  assert.match(cutOffLine('length'), /output limit/);
  assert.match(cutOffLine('interrupted'), /stopped before/);
  assert.equal(cutOffLine(''), '');
});

test('an error is shown once, in the words the server saved', () => {
  // The server sends an empty reply's note as text; the browser used to add its own ⚠ block under it.
  const note = '_[No response — model crashed]_';
  assert.equal(withFailureNote(note, 'model crashed', true), note);
  assert.equal(withFailureNote('Half an answer \n', 'model  crashed\nhard', true), 'Half an answer\n\n_[interrupted: model crashed hard]_');
  assert.equal(withFailureNote('', 'boom', true), '');
  // An older server says nothing about its note: keep the old block.
  assert.equal(withFailureNote('Half', 'boom', false), 'Half\n\n```\n⚠ boom\n```');
  assert.equal(withFailureNote('', 'boom', undefined), '```\n⚠ boom\n```');
});

test('a file that is not text is recognised before it is read as text', () => {
  const bytes = text => new TextEncoder().encode(text);
  assert.equal(looksBinary(bytes('name,total\nAda,3\n')), false);
  assert.equal(looksBinary(bytes('héllo wörld — ünïcode ✓\ttabbed\r\n')), false);
  assert.equal(looksBinary(new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00])), true);    // a zip
  assert.equal(looksBinary(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00])), true);
  assert.equal(looksBinary(new Uint8Array(40).fill(1)), true);
  assert.equal(looksBinary(new Uint8Array()), false);
});

test('only a run of the old kind holds the composer after a reload', () => {
  assert.equal(runIsLive({ status: 'running' }), true);
  assert.equal(runIsLive({ status: 'queued', workflow_id: 'wf-1' }), true);
  assert.equal(runIsLive({ status: 'running', workflow_id: 'cw3-abc' }), false);     // a Daedalus job has its own Stop
  assert.equal(runIsLive({ status: 'succeeded' }), false);
  assert.equal(runIsLive(null), false);
});

test('a reply that stopped at the round limit offers Continue as a new turn', () => {
  // Live 2026-11-14: the reply ended on "Ran out of round budget" and nothing offered to carry on.
  const reply = meta => ({ role: 'assistant', content: 'Here is what I found so far.', metadata: { delivery_status: 'completed', ...meta } });
  assert.equal(continueKind(reply({ round_limit: 40 }), true), 'rounds');
  assert.equal(cutOffLine('rounds', { round_limit: 40 }), 'This reply stopped at the limit of 40 tool rounds.');
  assert.equal(continueKind(reply({ round_limit: 0 }), true), '');
  assert.equal(continueKind(reply({ round_limit: 40 }), false), '');              // only the last reply
  // Replies saved before the limit was recorded this way.
  assert.equal(continueKind(reply({ max_rounds_reached: true, round: 12 }), true), 'rounds');
  assert.equal(roundLimit({ max_rounds_reached: true, round: 12 }), 12);
  // The output limit and a stopped reply come first: those are half-written text.
  assert.equal(continueKind(reply({ round_limit: 40, truncated: true }), true), 'length');
  assert.equal(continueKind(reply({ round_limit: 40, delivery_status: 'interrupted' }), true), 'interrupted');
  assert.match(CONTINUE_WORK, /Continue/);
});
