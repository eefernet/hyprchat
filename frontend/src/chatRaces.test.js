// Timing bugs found in the 2026-11-16 chat pass. Each test is named after what went wrong.
import test from 'node:test';
import assert from 'node:assert/strict';
import {canAdoptRun, keepsLocalMessages, strandsStream, failedReplyRow, createStreamTurns} from './streamOwner.js';
import {afterDelete, mergeListed, currentChat, isLocalChat} from './chatList.js';
import {createPendingDeletes, withoutPending, restoreAt} from './pendingDeletes.js';
import {createSaveOrder, createLatest} from './saveOrder.js';

test('opening a chat with a research run going does not take over a reply streaming in another chat', () => {
  assert.equal(canAdoptRun('chat-x'), false);
  assert.equal(canAdoptRun(null), true);
  assert.equal(canAdoptRun(undefined), true);
});

test('a chat load that answers just after that chat finished replying does not replace the finished reply', () => {
  // The load was requested at 100, the stream ended at 140, the answer arrives later with the older copy.
  assert.equal(keepsLocalMessages({chatId: 'a', streamingCid: null, loadStartedAt: 100, streamEndedAt: 140}), true);
  // Still streaming: as before.
  assert.equal(keepsLocalMessages({chatId: 'a', streamingCid: 'a', loadStartedAt: 100, streamEndedAt: undefined}), true);
  // The stream ended before the load was requested: the server copy is the newer one.
  assert.equal(keepsLocalMessages({chatId: 'a', streamingCid: null, loadStartedAt: 200, streamEndedAt: 140}), false);
  // Another chat is replying: this one loads normally.
  assert.equal(keepsLocalMessages({chatId: 'a', streamingCid: 'b', loadStartedAt: 100, streamEndedAt: undefined}), false);
});

test('leaving a ghost chat or deleting a chat while it replies is seen as stranding its reply', () => {
  assert.equal(strandsStream('ghost-1', ['ghost-1']), true);
  assert.equal(strandsStream('conv-2', ['ghost-1']), false);
  assert.equal(strandsStream(null, ['ghost-1']), false);
  assert.equal(strandsStream('conv-2', []), false);
});

test('a refused request does not turn the error bubble into a copy of the previous reply', () => {
  const saved = [{id: 1, role: 'user', content: 'hi'}, {id: 2, role: 'assistant', content: 'the earlier answer'}];
  // Refused before anything was saved: the stream announced no row.
  assert.equal(failedReplyRow(saved, null), null);
  assert.equal(failedReplyRow(saved, undefined), null);
  // The announced row is not there, or has no text yet.
  assert.equal(failedReplyRow(saved, 9), null);
  assert.equal(failedReplyRow([...saved, {id: 4, role: 'assistant', content: '  '}], 4), null);
  // The announced row with text is the one that may be shown.
  assert.equal(failedReplyRow([...saved, {id: 4, role: 'assistant', content: 'half an answer'}], 4).id, 4);
  // An id that names a user row is never a reply.
  assert.equal(failedReplyRow(saved, 1), null);
});

test('a reply that ends late cannot release the composer of a reply that started after it', () => {
  const turns = createStreamTurns();
  const first = turns.begin();
  assert.equal(turns.isLatest(first), true);
  const second = turns.begin();
  assert.equal(turns.isLatest(first), false);
  assert.equal(turns.isLatest(second), true);
});

test('deleting the open chat names the next chat to load instead of showing it unloaded', () => {
  const convs = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  assert.deepEqual(afterDelete(convs, 'a', 'a'), {list: [{id: 'b'}, {id: 'c'}], nextId: 'b', changed: true});
  // Deleting another chat leaves the open one alone.
  assert.deepEqual(afterDelete(convs, 'b', 'a'), {list: [{id: 'a'}, {id: 'c'}], nextId: 'a', changed: false});
  // The last chat: nothing to open.
  assert.deepEqual(afterDelete([{id: 'a'}], 'a', 'a'), {list: [], nextId: null, changed: true});
  // A saved chat is preferred over a ghost chat as the next one.
  assert.equal(afterDelete([{id: 'a'}, {id: 'ghost-1', ephemeral: true}, {id: 'b'}], 'a', 'a').nextId, 'b');
});

test('opening the Assistant chat keeps the messages of chats that were already loaded', () => {
  const prev = [
    {id: 'ghost-1', ephemeral: true, messages: [{role: 'user', content: 'temp'}]},
    {id: 'l-9', messages: [{role: 'user', content: 'offline'}]},
    {id: 'a', title: 'Old title', messages: [{id: 1, role: 'user', content: 'hello'}], _local: 1},
    {id: 'gone', messages: []},
  ];
  const merged = mergeListed(prev, [{id: 'a', title: 'New title'}, {id: 'b', title: 'Other'}], row => ({...row, pinned: false}));
  assert.deepEqual(merged.map(c => c.id), ['ghost-1', 'l-9', 'a', 'b']);
  const a = merged.find(c => c.id === 'a');
  assert.equal(a.title, 'New title');
  assert.equal(a.messages.length, 1);
  assert.equal(a._local, 1);
  assert.deepEqual(merged.find(c => c.id === 'b').messages, []);
  assert.equal(merged.find(c => c.id === 'ghost-1').messages.length, 1);
  assert.equal(isLocalChat({id: 'l-9'}), true);
  assert.equal(isLocalChat({id: 'conv-1'}), false);
});

test('the title check after a reply sees a rename made while the reply was streaming', () => {
  const atSend = [{id: 'a', title: 'New Chat'}];
  const now = [{id: 'a', title: 'My own name'}];
  assert.equal(currentChat(now, 'a', atSend[0]).title, 'My own name');
  // The chat is no longer in the list: fall back to the copy the send had.
  assert.equal(currentChat([], 'a', atSend[0]).title, 'New Chat');
});

function fakeTimers() {
  const timers = new Map(); let n = 0;
  return {
    setTimer: fn => { n += 1; timers.set(n, fn); return n; },
    clearTimer: id => timers.delete(id),
    fire: () => { const all = [...timers.values()]; timers.clear(); all.forEach(fn => fn()); },
    count: () => timers.size,
  };
}

test('a message deleted with Undo still pending does not come back when the chat is reloaded', () => {
  const clock = fakeTimers(); const deletes = createPendingDeletes(clock);
  const committed = [];
  deletes.add('t1', {chatId: 'a', message: {id: 5, role: 'assistant', content: 'bye'}, index: 1, commit: () => committed.push(5)});
  const fromServer = [{id: 4, role: 'user', content: 'hi'}, {id: 5, role: 'assistant', content: 'bye'}];
  assert.deepEqual(withoutPending(fromServer, deletes.waiting('a')).map(m => m.id), [4]);
  // Another chat's reload is not touched.
  assert.deepEqual(withoutPending(fromServer, deletes.waiting('b')).map(m => m.id), [4, 5]);
  clock.fire();
  assert.deepEqual(committed, [5]);
  assert.equal(deletes.size, 0);
});

test('a message with no id yet is still hidden on reload, and only one of two identical ones', () => {
  const waiting = [{role: 'user', content: 'yes'}];
  const fromServer = [{id: 1, role: 'user', content: 'yes'}, {id: 2, role: 'assistant', content: 'ok'}, {id: 3, role: 'user', content: 'yes'}];
  assert.deepEqual(withoutPending(fromServer, waiting).map(m => m.id), [2, 3]);
});

test('Undo after a reload does not insert a second copy of the message', () => {
  const removed = {id: 5, role: 'assistant', content: 'bye'};
  const reloaded = [{id: 4, role: 'user', content: 'hi'}, {id: 5, role: 'assistant', content: 'bye'}];
  assert.deepEqual(restoreAt(reloaded, removed, 1).map(m => m.id), [4, 5]);
  assert.deepEqual(restoreAt([{id: 4, role: 'user', content: 'hi'}], removed, 1).map(m => m.id), [4, 5]);
  assert.deepEqual(restoreAt([{id: 4}], removed, 99).map(m => m.id), [4, 5]);
});

test('Undo in time stops the delete; Undo too late says so', () => {
  const clock = fakeTimers(); const deletes = createPendingDeletes(clock);
  let committed = 0;
  deletes.add('t1', {chatId: 'a', message: {id: 5}, index: 0, commit: () => { committed += 1; }});
  assert.equal(deletes.undo('t1').message.id, 5);
  clock.fire();
  assert.equal(committed, 0);
  deletes.add('t2', {chatId: 'a', message: {id: 6}, index: 0, commit: () => { committed += 1; }});
  clock.fire();
  assert.equal(deletes.undo('t2'), null);
  assert.equal(committed, 1);
});

test('closing the page runs waiting deletes at once and leaves no timer behind', () => {
  const clock = fakeTimers(); const deletes = createPendingDeletes(clock);
  const committed = [];
  deletes.add('t1', {chatId: 'a', message: {id: 5}, index: 0, commit: how => committed.push([5, how])});
  deletes.add('t2', {chatId: 'b', message: {id: 6}, index: 0, commit: how => committed.push([6, how])});
  deletes.flush({closing: true});
  assert.deepEqual(committed, [[5, {closing: true}], [6, {closing: true}]]);
  assert.equal(clock.count(), 0);
  clock.fire();
  assert.equal(committed.length, 2);
});

test('a waiting delete never fires under another profile after a profile switch', () => {
  const clock = fakeTimers(); const deletes = createPendingDeletes(clock);
  let committed = 0;
  deletes.add('t1', {chatId: 'a', message: {id: 5}, index: 0, commit: () => { committed += 1; }});
  deletes.clear();
  clock.fire();
  assert.equal(committed, 0);
  assert.equal(deletes.size, 0);
  assert.deepEqual(deletes.waiting('a'), []);
});

test('the retry of a failed rename does not land after a newer rename', () => {
  const order = createSaveOrder();
  const first = order.begin(['conv:a:title']);
  assert.equal(first.isCurrent(), true);
  const second = order.begin(['conv:a:title']);
  assert.equal(first.isCurrent(), false);   // its retry must not be sent
  assert.equal(second.isCurrent(), true);
  // A write to another field or another chat does not cancel it.
  order.begin(['conv:a:model']); order.begin(['conv:b:title']);
  assert.equal(second.isCurrent(), true);
  // A write of two fields is stale as soon as either was written again.
  const both = order.begin(['conv:a:title', 'conv:a:model']);
  order.begin('conv:a:model');
  assert.equal(both.isCurrent(), false);
  // No key: never held back.
  assert.equal(order.begin([]).isCurrent(), true);
});

test('message search shows the newest query, not a slower answer to an older one', () => {
  const latest = createLatest();
  const older = latest.start();
  const newer = latest.start();
  assert.equal(older.isCurrent(), false);
  assert.equal(older.signal.aborted, true);
  assert.equal(newer.isCurrent(), true);
  latest.cancel();
  assert.equal(newer.isCurrent(), false);
  assert.equal(newer.signal.aborted, true);
});
