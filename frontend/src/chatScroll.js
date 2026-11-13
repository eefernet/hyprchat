// Rules for keeping the chat list at the newest content. No DOM access: the hook in useChatFollow.js
// feeds these measurements in, so the rules can be tested without a browser.

export const BOTTOM_SLACK = 48;      // px: this close to the end counts as "at the bottom"
export const INPUT_WINDOW_MS = 400;  // a scroll this soon after a wheel/touch/key is the user's own
export const JUMP_BUTTON_LIVE = 120; // px from the bottom before "scroll to bottom" shows while a reply streams
export const JUMP_BUTTON_IDLE = 400;

export function distanceFromBottom(el) {
  if (!el) return 0;
  return Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight);
}

// Whether the list should keep following after one scroll event.
//   following    current state
//   top          scrollTop now; previousTop: scrollTop at the last event or pin
//   distance     px from the bottom now
//   userMoved    the reader is driving this scroll: a wheel, touch drag or PageUp/ArrowUp/Home just before
//                it, or a pointer held down on the list (scrollbar drag, selecting text)
//
// Only the reader releases. The position also moves up on its own: the browser clamps it when the list
// gets shorter or the viewport taller, and the composer does exactly that on every keystroke of a
// multi-line draft (it collapses for a moment to measure itself), leaving a gap once it grows back.
// Live 2026-11-13: treating "moved up and now far from the bottom" as the reader's doing dropped the view
// out of following the moment a second line was typed.
export function nextFollow({ following, top, previousTop, distance, userMoved = false }) {
  const movedUp = top < previousTop - 1;
  // Landing exactly at the bottom is a clamp even mid-click (collapsing a card shortens the list).
  if (movedUp) return userMoved && distance > 1 ? false : following;
  if (distance <= BOTTOM_SLACK) return true;
  return following;
}

export function showJumpButton(distance, streaming) {
  return distance > (streaming ? JUMP_BUTTON_LIVE : JUMP_BUTTON_IDLE);
}

const UP_KEYS = new Set(['PageUp', 'ArrowUp', 'Home']);

export function isUpKey(event) {
  if (!event || event.ctrlKey || event.altKey) return false;
  if (event.key === ' ') return !!event.shiftKey;            // Shift+Space pages up
  if (event.key === 'ArrowUp' && event.metaKey) return true;  // Cmd+Up jumps to the top
  return UP_KEYS.has(event.key) && !event.metaKey;
}
