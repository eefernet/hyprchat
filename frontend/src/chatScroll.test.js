import assert from 'node:assert/strict';
import test from 'node:test';
import { BOTTOM_SLACK, distanceFromBottom, isUpKey, nextFollow, showJumpButton } from './chatScroll.js';

test('distance from the bottom is never negative', () => {
  assert.equal(distanceFromBottom({ scrollHeight: 2000, scrollTop: 1200, clientHeight: 600 }), 200);
  assert.equal(distanceFromBottom({ scrollHeight: 500, scrollTop: 10, clientHeight: 600 }), 0);
  assert.equal(distanceFromBottom(null), 0);
});

test('a tall block arriving does not stop following', () => {
  // Live 2026-11-13: a table taller than the old 200px threshold dropped the view out of auto-scroll for good.
  // Growth moves no scroll position, so any event that does arrive has the same top.
  assert.equal(nextFollow({ following: true, top: 1000, previousTop: 1000, distance: 900 }), true);
});

test('scrolling up to read releases the view', () => {
  assert.equal(nextFollow({ following: true, top: 700, previousTop: 1000, distance: 300, userMoved: true }), false);
});

test('a small wheel-up inside the slack still releases', () => {
  assert.equal(nextFollow({ following: true, top: 990, previousTop: 1000, distance: 10, userMoved: true }), false);
});

test('the browser clamping the position after the list shrinks keeps following', () => {
  // The reply is rebuilt when it finishes and the list gets shorter for a moment.
  assert.equal(nextFollow({ following: true, top: 940, previousTop: 1000, distance: 0 }), true);
});

test('typing a second line in the composer does not release the view', () => {
  // Live 2026-11-13: the composer collapses to measure itself, the list is clamped up, then the composer
  // grows back and leaves an 81px gap. Nobody scrolled.
  assert.equal(nextFollow({ following: true, top: 5297, previousTop: 5378, distance: 81 }), true);
});

test('collapsing a card by clicking it keeps following', () => {
  assert.equal(nextFollow({ following: true, top: 880, previousTop: 1000, distance: 0, userMoved: true }), true);
});

test('a position that moves up on its own does not start following either', () => {
  assert.equal(nextFollow({ following: false, top: 940, previousTop: 1000, distance: 0 }), false);
});

test('scrolling back down to the end follows again', () => {
  assert.equal(nextFollow({ following: false, top: 1000, previousTop: 700, distance: BOTTOM_SLACK }), true);
  assert.equal(nextFollow({ following: false, top: 800, previousTop: 700, distance: 200 }), false);
});

test('reading higher up stays released while content grows below', () => {
  assert.equal(nextFollow({ following: false, top: 400, previousTop: 400, distance: 5000 }), false);
});

test('the jump button shows sooner while a reply is streaming', () => {
  assert.equal(showJumpButton(150, true), true);
  assert.equal(showJumpButton(150, false), false);
  assert.equal(showJumpButton(500, false), true);
});

test('only plain upward keys count as moving up', () => {
  assert.equal(isUpKey({ key: 'PageUp' }), true);
  assert.equal(isUpKey({ key: 'ArrowUp', metaKey: true }), true);
  assert.equal(isUpKey({ key: ' ', shiftKey: true }), true);
  assert.equal(isUpKey({ key: ' ' }), false);
  assert.equal(isUpKey({ key: 'PageUp', ctrlKey: true }), false);
  assert.equal(isUpKey({ key: 'ArrowDown' }), false);
});
