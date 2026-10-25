import test from 'node:test';
import assert from 'node:assert/strict';
import {formatDocumentTitle} from './documentTitle.js';

test('app name alone when there is no title', () => {
  assert.equal(formatDocumentTitle({}), 'HyprChat');
  assert.equal(formatDocumentTitle(), 'HyprChat');
  assert.equal(formatDocumentTitle({title: null}), 'HyprChat');
  assert.equal(formatDocumentTitle({title: '   '}), 'HyprChat');
});

test('title precedes the app name with a middle dot', () => {
  assert.equal(formatDocumentTitle({title: 'Weekly plan'}), 'Weekly plan · HyprChat');
  assert.equal(formatDocumentTitle({title: '  Trimmed  '}), 'Trimmed · HyprChat');
  assert.equal(formatDocumentTitle({title: 42}), '42 · HyprChat');
});

test('streaming adds the dot prefix with or without a title', () => {
  assert.equal(formatDocumentTitle({title: 'Weekly plan', streaming: true}), '● Weekly plan · HyprChat');
  assert.equal(formatDocumentTitle({streaming: true}), '● HyprChat');
  assert.equal(formatDocumentTitle({title: 'x', streaming: false}), 'x · HyprChat');
});
