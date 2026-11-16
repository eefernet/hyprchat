// Drafts that stay with their chat (2026-11-16). The message box was one piece of state for the whole
// app: text typed in one chat was still there in the next, and a reload lost it.
import assert from 'node:assert/strict';
import test from 'node:test';
import { NEW_CHAT, draftKey, draftOf, dropDraft, fromStored, isLocalOnly, joinText, moveDraft, parseStored, pruneLocal, storageKey, toStored, withAttachments, withText } from './chatDrafts.js';

const typed = (map, key, text, at = 1) => withText(map, key, text, at);

test('a draft typed in one chat is not shown in another', () => {
  const map = typed({}, 'conv-a', 'half a thought');
  assert.equal(draftOf(map, 'conv-a').text, 'half a thought');
  assert.equal(draftOf(map, 'conv-b').text, '');
  assert.deepEqual(draftOf(map, 'conv-b').attachments, []);
});

test('switching back to a chat shows the draft left there', () => {
  let map = typed({}, 'conv-a', 'first');
  map = typed(map, 'conv-b', 'second');
  assert.equal(draftOf(map, 'conv-a').text, 'first');
  assert.equal(draftOf(map, 'conv-b').text, 'second');
});

test('attachments stay with the chat they were added in', () => {
  const file = { name: 'a.pdf', type: 'pdf', content: 'text' };
  let map = withAttachments({}, 'conv-a', previous => [...previous, file]);
  map = typed(map, 'conv-b', 'unrelated');
  assert.deepEqual(draftOf(map, 'conv-a').attachments, [file]);
  assert.deepEqual(draftOf(map, 'conv-b').attachments, []);
});

test('text added by an updater lands in the chat it was started in', () => {
  // Voice input and quotes append to "the previous text": that must be the same chat's.
  let map = typed({}, 'conv-a', 'one');
  map = typed(map, 'conv-b', 'other');
  map = withText(map, 'conv-a', previous => `${previous} two`, 2);
  assert.equal(draftOf(map, 'conv-a').text, 'one two');
  assert.equal(draftOf(map, 'conv-b').text, 'other');
});

test('what was typed before a chat existed follows it to its new id', () => {
  let map = typed({}, draftKey(null), 'my first message');
  map = withAttachments(map, NEW_CHAT, [{ name: 'x.txt', content: 'x' }]);
  map = moveDraft(map, NEW_CHAT, 'conv-new');
  assert.equal(draftOf(map, 'conv-new').text, 'my first message');
  assert.equal(draftOf(map, 'conv-new').attachments.length, 1);
  assert.equal(NEW_CHAT in map, false);
});

test('a move never drops text already at the destination', () => {
  let map = typed({}, 'conv-a', 'already here');
  map = typed(map, NEW_CHAT, 'arriving');
  assert.equal(draftOf(moveDraft(map, NEW_CHAT, 'conv-a'), 'conv-a').text, 'already here\n\narriving');
  assert.equal(moveDraft(map, 'conv-none', 'conv-a'), map);            // nothing to move
  assert.equal(joinText('', 'only'), 'only');
});

test('sending clears only the chat that was sent from', () => {
  let map = typed({}, 'conv-a', 'send me');
  map = typed(map, 'conv-b', 'keep me');
  map = withAttachments(withText(map, 'conv-a', ''), 'conv-a', []);
  assert.equal('conv-a' in map, false);                                 // an emptied draft leaves nothing behind
  assert.equal(draftOf(map, 'conv-b').text, 'keep me');
});

test('draft text survives a reload, attachments are never written', () => {
  let map = typed({}, 'conv-a', 'come back to this', 50);
  map = withAttachments(map, 'conv-a', [{ name: 'big.pdf', content: 'x'.repeat(5000) }]);
  const stored = JSON.stringify(toStored('', map, ['conv-a']));
  assert.equal(stored.includes('big.pdf'), false);
  const reloaded = fromStored(stored);
  assert.equal(draftOf(reloaded, 'conv-a').text, 'come back to this');
  assert.deepEqual(draftOf(reloaded, 'conv-a').attachments, []);
});

test('a ghost chat\'s draft is never written to storage', () => {
  let map = typed({}, 'ghost-123', 'private');
  map = typed(map, 'l-9', 'offline');
  map = typed(map, NEW_CHAT, 'typed in Ghost mode before the chat exists');
  const skip = key => isLocalOnly(key) || key === NEW_CHAT;            // what the hook passes while Ghost mode is on
  assert.deepEqual(toStored('', map, Object.keys(map), { skip }), {});
  assert.deepEqual(Object.keys(toStored('', map, Object.keys(map))), [NEW_CHAT]);   // an ordinary new chat's draft is kept
});

test('two tabs do not erase each other\'s drafts', () => {
  const other = JSON.stringify({ 'conv-other': { text: 'typed in the other tab', at: 9 } });
  const mine = typed({}, 'conv-mine', 'typed here', 10);
  const stored = toStored(other, mine, ['conv-mine']);
  assert.deepEqual(Object.keys(stored).sort(), ['conv-mine', 'conv-other']);
  // Clearing my draft removes mine only.
  assert.deepEqual(Object.keys(toStored(JSON.stringify(stored), {}, ['conv-mine'])), ['conv-other']);
});

test('only the newest fifty drafts are kept', () => {
  let map = {};
  for (let index = 0; index < 60; index++) map = typed(map, `conv-${index}`, `draft ${index}`, index);
  const stored = toStored('', map, Object.keys(map));
  assert.equal(Object.keys(stored).length, 50);
  assert.equal('conv-59' in stored && !('conv-0' in stored), true);
});

test('corrupt stored drafts do not break the composer', () => {
  for (const junk of ['{not json', '[]', 'null', '"text"', JSON.stringify({ a: 5, b: { text: 7 }, c: { text: '' } })]) {
    assert.deepEqual(parseStored(junk), {});
    assert.deepEqual(fromStored(junk), {});
  }
});

test('one profile\'s drafts are stored apart from another\'s', () => {
  assert.notEqual(storageKey('user-a'), storageKey('user-b'));
  assert.equal(storageKey(''), 'hc-chat-drafts::default');
});

test('deleting a chat forgets its draft, and vanished ghost chats are forgotten', () => {
  let map = typed({}, 'conv-a', 'x');
  map = typed(map, 'ghost-1', 'y');
  map = typed(map, 'ghost-2', 'z');
  assert.equal('conv-a' in dropDraft(map, 'conv-a'), false);
  const pruned = pruneLocal(map, ['ghost-2']);
  assert.deepEqual(Object.keys(pruned).sort(), ['conv-a', 'ghost-2']);   // a saved chat's draft is kept whatever the list says
  assert.equal(pruneLocal(pruned, ['ghost-2']), pruned);
});

test('queued text comes back as a draft after a reload', () => {
  const stored = JSON.stringify({ 'conv-a': { text: 'was typing this', at: 3 } });
  const map = fromStored(stored, { 'conv-a': ['queued one', 'queued two'], 'conv-b': ['only queued'] });
  assert.equal(draftOf(map, 'conv-a').text, 'queued one\n\nqueued two\n\nwas typing this');
  assert.equal(draftOf(map, 'conv-b').text, 'only queued');
});
