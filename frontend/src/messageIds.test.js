import assert from 'node:assert/strict';
import test from 'node:test';
import { findSavedMessage, withUserId } from './messageIds.js';

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
