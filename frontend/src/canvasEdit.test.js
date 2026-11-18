import assert from 'node:assert/strict';
import test from 'node:test';
import { locateEdit } from './canvasEdit.js';

const edit = { from: 6, to: 11, original: 'world' };

test('an untouched document takes the edit at its saved place', () => {
  assert.deepEqual(locateEdit('hello world!', edit), { from: 6, to: 11 });
});

test('text typed above the selection moves the edit with it', () => {
  assert.deepEqual(locateEdit('A new line.\nhello world!', edit), { from: 18, to: 23 });
});

test('a changed or duplicated target is refused instead of overwriting something else', () => {
  assert.equal(locateEdit('hello there!', edit), null);                 // the target was rewritten
  assert.equal(locateEdit('XX hello world, world', edit), null);        // moved, and now there are two
  assert.equal(locateEdit('abc', { from: 9, to: 9, original: '' }), null);
});

test('a target still at its saved place wins even when the same text appears elsewhere', () => {
  assert.deepEqual(locateEdit('hello world, world', edit), { from: 6, to: 11 });
  assert.deepEqual(locateEdit('alpha, then more', { from: 0, to: 5, original: 'alpha' }), { from: 0, to: 5 });
});
