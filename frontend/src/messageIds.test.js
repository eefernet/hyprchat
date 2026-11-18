import assert from 'node:assert/strict';
import test from 'node:test';
import { findSavedMessage, savedRange, withUserId } from './messageIds.js';

const user = (content, id) => ({ role: 'user', content, ...(id == null ? {} : { id }) });
const reply = (content, id) => ({ role: 'assistant', content, ...(id == null ? {} : { id }) });

test('the message just sent gets its saved id from the start of the reply', () => {
  // It had none, so deleting it only hid it: after a reload it was back.
  const messages = [user('first', 1), reply('answer', 2), user('second'), reply('')];
  const next = withUserId(messages, 3);
  assert.equal(next[2].id, 3);
  assert.equal(next[0].id, 1);
  assert.notEqual(next, messages);
  assert.equal(withUserId(next, 3), next);              // nothing to change: same list
  assert.equal(withUserId(messages, null), messages);
  assert.equal(withUserId(messages, 0), messages);      // the server sends 0 when nothing was saved
  assert.deepEqual(withUserId([], 5), []);
});

test('a message is matched to its saved row by role and text, not by where it sits', () => {
  // The chat on screen starts with a greeting that was never saved, so every position is off by one.
  const local = [reply('Hello, I am Sage.'), user('What is a monad?'), reply('A monad is…')];
  const saved = [user('What is a monad?', 11), reply('A monad is…', 12)];
  assert.equal(findSavedMessage(saved, local[1], 1, local.length), 11);
  assert.equal(findSavedMessage(saved, local[2], 2, local.length), 12);
  assert.equal(findSavedMessage(saved, local[0], 0, local.length), null);   // was: row 11, the user's question
});

test('its own id wins, and the nearest of two identical messages is chosen', () => {
  assert.equal(findSavedMessage([user('yes', 5)], user('yes', 9), 0, 1), 9);
  const saved = [user('yes', 5), reply('ok', 6), user('yes', 7), reply('done', 8)];
  assert.equal(findSavedMessage(saved, user('yes'), 2, 4), 7);
  assert.equal(findSavedMessage(saved, user('yes'), 0, 4), 5);
});

test('position is the last resort and only when both lists line up', () => {
  // A reply whose text differs from the saved one (persona placeholders are filled in on screen).
  const saved = [user('q', 1), reply('Hi {{user}}', 2)];
  assert.equal(findSavedMessage(saved, reply('Hi Ethan'), 1, 2), 2);
  assert.equal(findSavedMessage(saved, reply('Hi Ethan'), 1, 3), null);
  assert.equal(findSavedMessage(saved, user('Hi Ethan'), 1, 2), null);     // wrong role at that position
  assert.equal(findSavedMessage([], reply('x'), 0, 1), null);
  assert.equal(findSavedMessage(saved, null, 0, 2), null);
});

// 2026-11-16, found while checking the Retry button: the sixteenth message of a chat was a reply stopped
// before it had any text or id. An earlier reply (row 8) had been stopped the same way, so "same role,
// same text" matched it, and Regenerate sent DELETE …/messages?from_id=8: every later turn was removed on
// the server while the screen still showed them.
const longChat = () => [
  user('one', 1), reply('first answer', 2), user('two', 3), reply('second answer', 4),
  user('three', 5), reply('third answer', 6), user('harbour', 7), reply('', 8),
  user('four', 9), reply('ok', 10), user('five', 11), reply('fine', 12),
  user('six', 13), reply('partly written', 14),
];

test('retrying a reply stopped before any text does not delete an earlier empty reply and the turns after it', () => {
  const saved = [...longChat(), user('mountain hut', 15)];             // the stopped reply was never saved
  const onScreen = [...longChat(), user('mountain hut'), reply('')];   // Stop came before the ids arrived
  assert.equal(findSavedMessage(saved, onScreen[15], 15, onScreen.length, onScreen), null);   // was: row 8
  assert.equal(findSavedMessage(saved, onScreen[14], 14, onScreen.length, onScreen), 15);
  // The server did save the empty reply: that row, never the earlier one.
  const withRow = [...saved, reply('', 16)];
  assert.equal(findSavedMessage(withRow, onScreen[15], 15, onScreen.length, onScreen), 16);
});

test('a saved row that another message on screen already is can never be chosen', () => {
  const saved = [user('yes', 1), reply('', 2), user('yes', 3)];
  const onScreen = [user('yes', 1), reply('', 2), user('yes', 3), reply('')];
  assert.equal(findSavedMessage(saved, onScreen[3], 3, onScreen.length, onScreen), null);     // row 2 is message 1
  assert.deepEqual(savedRange(saved, onScreen, 3), { after: 2, before: 3 });
  assert.deepEqual(savedRange(saved, onScreen, 1), { after: 0, before: 2 });
  assert.deepEqual(savedRange(saved, null, 1), { after: -1, before: 3 });
});

test('a reply the server saved with its own note is still found when it is the only one it can be', () => {
  // On screen: a greeting that was never saved (the lists do not line up) and a stopped reply with no text.
  const saved = [user('q', 1), reply('a', 2), user('again', 3), reply('_[interrupted: stopped]_', 4)];
  const onScreen = [reply('Hello, I am Sage.'), user('q', 1), reply('a', 2), user('again'), reply('')];
  assert.equal(findSavedMessage(saved, onScreen[4], 4, onScreen.length, onScreen), 4);
  assert.equal(findSavedMessage(saved, onScreen[3], 3, onScreen.length, onScreen), 3);
  // Two replies without an id in that stretch: it cannot be told which one the row is.
  const twoReplies = [user('q', 1), reply('a', 2), user('again'), reply('x'), reply('')];
  assert.equal(findSavedMessage(saved, twoReplies[4], 4, twoReplies.length + 1, twoReplies), null);
});
