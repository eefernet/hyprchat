// Messages typed while a reply is being written (2026-11-16). The box used to be locked and Enter did nothing.
import assert from 'node:assert/strict';
import test from 'node:test';
import { dispatched, dropChat, enqueue, finished, hold, holdLine, leave, nextToSend, pruneLocal, queueFor, queuedLabel, release, removeItem, renameChat, rescueTexts, settle, submitAction } from './messageQueue.js';
import { replyOutcome } from './runState.js';

const item = (id, text = `message ${id}`, attachments = []) => ({ id, text, attachments });
const queued = (...ids) => ids.reduce((state, id) => enqueue(state, 'conv-a', item(id)), {});

test('Enter while this chat is replying queues the message instead of dropping it', () => {
  assert.equal(submitAction({ streamingHere: true, hasText: true }), 'queue');
  assert.equal(submitAction({ streamingHere: false, hasText: true }), 'send');
  assert.equal(submitAction({ streamingHere: true, hasText: false, attachments: [{ name: 'a.csv' }] }), 'queue');
  // A queued message that is being prepared (files uploading) counts too: the next Enter goes behind it.
  assert.equal(submitAction({ dispatching: true, hasText: true }), 'queue');
});

test('another chat replying does not queue here, and an empty box does nothing', () => {
  assert.equal(submitAction({ streamingHere: false, hasText: true }), 'send');     // send() shows "another chat is still replying"
  assert.equal(submitAction({ streamingHere: true, hasText: false }), 'none');
  assert.equal(submitAction(), 'none');
});

test('a council chat never queues, and a file still being read is not queued', () => {
  assert.equal(submitAction({ streamingHere: true, council: true, hasText: true }), 'none');
  assert.equal(submitAction({ streamingHere: true, hasText: true, attachments: [{ name: 'a.pdf', loading: true }] }), 'wait');
  assert.equal(submitAction({ streamingHere: true, hasText: true, uploading: true }), 'wait');
});

test('queued messages keep their order and belong to their chat', () => {
  let state = queued('1', '2', '3');
  state = enqueue(state, 'conv-b', item('9'));
  assert.deepEqual(queueFor(state, 'conv-a').items.map(entry => entry.id), ['1', '2', '3']);
  assert.deepEqual(queueFor(state, 'conv-b').items.map(entry => entry.id), ['9']);
  assert.deepEqual(queueFor(state, 'conv-c').items, []);
  assert.equal(enqueue(state, 'conv-a', { id: 'x', text: '   ' }), state);          // nothing to send
});

test('the next queued message goes out when the reply finishes normally', () => {
  const state = settle(queued('1', '2'), 'conv-a', replyOutcome({ ended: true }), true);
  assert.equal(nextToSend(state, 'conv-a').id, '1');
});

test('one finished reply sends one message', () => {
  let state = settle(queued('1', '2'), 'conv-a', 'completed', true);
  state = dispatched(state, 'conv-a', '1');
  assert.equal(nextToSend(state, 'conv-a'), null);                       // not twice, and not the second one yet
  state = removeItem(state, 'conv-a', '1');                              // accepted: it is in the chat now
  state = finished(state, 'conv-a', '1', true);
  assert.equal(nextToSend(state, 'conv-a'), null);                       // the second waits for the first one's reply
  state = settle(state, 'conv-a', 'completed', true);
  assert.equal(nextToSend(state, 'conv-a').id, '2');
});

test('nothing is sent after Stop', () => {
  let state = hold(queued('1'), 'conv-a', 'stopped');                    // stop() does this before it aborts
  state = settle(state, 'conv-a', replyOutcome({ stopped: true }), true);
  assert.equal(nextToSend(state, 'conv-a'), null);
  assert.equal(queueFor(state, 'conv-a').hold, 'stopped');
});

test('Stop pressed just as the reply finished still holds the queue', () => {
  let state = hold(queued('1'), 'conv-a', 'stopped');
  state = settle(state, 'conv-a', 'completed', true);                    // the stream had in fact just ended
  assert.equal(nextToSend(state, 'conv-a'), null);
});

test('nothing is sent after a reply that did not really finish', () => {
  const endings = { failed: { failed: true, ended: true }, interrupted: {}, rounds: { ended: true, roundLimit: 40 }, cutoff: { ended: true, truncated: true }, job: { ended: true, startedJob: true } };
  for (const [reason, ending] of Object.entries(endings)) {
    const state = settle(queued('1'), 'conv-a', replyOutcome(ending), true);
    assert.equal(nextToSend(state, 'conv-a'), null, reason);
    assert.equal(queueFor(state, 'conv-a').hold, reason);
    assert.match(holdLine(reason), /queue is waiting/);
  }
  assert.equal(queueFor(settle(queued('1'), 'conv-a', 'unknown', true), 'conv-a').hold, 'unknown');   // a run found at load
});

test('a reply that finished in another chat waits for Send now', () => {
  const state = settle(queued('1'), 'conv-a', 'completed', false);
  assert.equal(nextToSend(state, 'conv-a'), null);
  assert.equal(queueFor(state, 'conv-a').hold, 'away');
});

test('a chat left before its message went out waits for Send now', () => {
  let state = settle(queued('1'), 'conv-a', 'completed', true);
  state = leave(state, 'conv-a');
  assert.equal(nextToSend(state, 'conv-a'), null);
  assert.equal(queueFor(state, 'conv-a').hold, 'away');
  assert.equal(leave(queued('1'), 'conv-a').hold, undefined);            // nothing was about to go: unchanged
});

test('a held queue stays held after a later reply, until Send now', () => {
  let state = settle(queued('1', '2'), 'conv-a', 'failed', true);
  state = settle(state, 'conv-a', 'completed', true);                    // for example after Continue
  assert.equal(nextToSend(state, 'conv-a'), null);
  state = release(state, 'conv-a');
  assert.equal(nextToSend(state, 'conv-a').id, '1');
  state = removeItem(dispatched(state, 'conv-a', '1'), 'conv-a', '1');
  state = settle(finished(state, 'conv-a', '1', true), 'conv-a', 'completed', true);
  assert.equal(nextToSend(state, 'conv-a').id, '2');                     // the rest follow
});

test('a message that could not be sent stays in the queue', () => {
  let state = dispatched(settle(queued('1', '2'), 'conv-a', 'completed', true), 'conv-a', '1');
  state = finished(state, 'conv-a', '1', false);
  assert.deepEqual(queueFor(state, 'conv-a').items.map(entry => entry.id), ['1', '2']);
  assert.equal(queueFor(state, 'conv-a').hold, 'unsent');
  assert.equal(nextToSend(state, 'conv-a'), null);                       // no loop
});

test('removing a message keeps the others in order, and the last one clears the hold', () => {
  let state = hold(queued('1', '2', '3'), 'conv-a', 'stopped');
  state = removeItem(state, 'conv-a', '2');
  assert.deepEqual(queueFor(state, 'conv-a').items.map(entry => entry.id), ['1', '3']);
  state = removeItem(removeItem(state, 'conv-a', '1'), 'conv-a', '3');
  assert.equal('conv-a' in state, false);
  assert.equal(nextToSend(enqueue(state, 'conv-a', item('4')), 'conv-a'), null);    // a new queue starts unheld and not ready
  assert.equal(queueFor(enqueue(state, 'conv-a', item('4')), 'conv-a').hold, '');
});

test('a deleted chat\'s queue is forgotten, a renamed chat keeps its queue', () => {
  let state = enqueue(queued('1'), 'ghost-1', item('g'));
  assert.equal('conv-a' in dropChat(state, 'conv-a'), false);
  assert.deepEqual(Object.keys(pruneLocal(state, [])), ['conv-a']);
  state = renameChat(state, 'conv-a', 'conv-z');
  assert.deepEqual(queueFor(state, 'conv-z').items.map(entry => entry.id), ['1']);
});

test('a queued message shows as one line, with its files counted', () => {
  assert.equal(queuedLabel(item('1', 'first line\nsecond   line')), 'first line second line');
  assert.equal(queuedLabel(item('1', 'x'.repeat(200))).length, 90);
  assert.equal(queuedLabel(item('1', '', [{ name: 'a.csv' }, { name: 'b.csv' }])), '2 attachments');
});

test('queued text of saved chats is kept for a reload, a ghost chat\'s is not', () => {
  let state = queued('1', '2');
  state = enqueue(state, 'ghost-1', item('g', 'private'));
  state = enqueue(state, 'conv-b', item('f', '', [{ name: 'only-a-file.csv' }]));
  assert.deepEqual(rescueTexts(state), { 'conv-a': ['message 1', 'message 2'] });
});
