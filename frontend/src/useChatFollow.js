import { useCallback, useEffect, useRef } from 'react';
import { INPUT_WINDOW_MS, distanceFromBottom, isUpKey, nextFollow } from './chatScroll.js';

// Keeps the chat list at its newest content while the reader is at the bottom, and leaves it alone once
// they move up. The rules are in chatScroll.js; this file only measures and scrolls.
//
// One ResizeObserver does the pinning. It watches the list (anything that makes it taller or shorter:
// streamed text, tool cards, a diagram drawn late, the reply being rebuilt when it finishes) and the
// scroll container itself (it gets shorter when the composer grows or the phone keyboard opens).
// Observers run after layout and before paint, so the pin never shows one frame late.
//
//   const chat = useChatFollow(chatScrollRef, actId, distance => ...);   // third argument: told the distance
//   from the bottom whenever sizes change (growth fires no scroll event, so jump buttons need this)
//   <div ref={chat.attach} onScroll={e => { const distance = chat.onScroll(e); ... }}>
export function useChatFollow(containerRef, resetKey, onResize) {
  const state = useRef({ following: true, top: 0, inputAt: 0, held: false, touchY: null, detach: null });
  const resized = useRef(onResize);
  resized.current = onResize;

  const setFollowing = useCallback(value => {
    state.current.following = value;
    const el = containerRef.current;
    // The browser's own scroll anchoring fights the pin; keep it only while the reader is elsewhere.
    if (el) el.style.overflowAnchor = value ? 'none' : '';
  }, [containerRef]);

  const pin = useCallback(() => {
    const el = containerRef.current;
    const current = state.current;
    if (!el || !current.following) return;
    if (distanceFromBottom(el) > 1) el.scrollTop = el.scrollHeight;
    current.top = el.scrollTop;
  }, [containerRef]);

  const attach = useCallback(el => {
    const current = state.current;
    current.detach?.();
    current.detach = null;
    containerRef.current = el;
    if (!el) return;
    el.style.overflowAnchor = current.following ? 'none' : '';

    const sizes = new ResizeObserver(() => { pin(); resized.current?.(distanceFromBottom(el)); });
    const watch = () => {
      sizes.disconnect();
      sizes.observe(el);
      for (const child of el.children) sizes.observe(child);
    };
    const children = new MutationObserver(watch);
    children.observe(el, { childList: true });
    watch();

    // An upward gesture releases at once: waiting for the scroll event loses to a pin during fast streaming.
    const up = () => {
      current.inputAt = performance.now();
      if (el.scrollTop > 0) setFollowing(false);
    };
    const wheel = event => { if (event.deltaY < -1 && Math.abs(event.deltaY) >= Math.abs(event.deltaX)) up(); };
    const key = event => { if (isUpKey(event)) up(); };
    const touchStart = event => { current.touchY = event.touches[0]?.clientY ?? null; };
    const touchMove = event => {
      const y = event.touches[0]?.clientY;
      if (current.touchY != null && y != null && y - current.touchY > 6) up();   // finger down = list up
    };
    // A pointer held on the list is the reader dragging the scrollbar or selecting text: scrolls are theirs.
    const press = () => { current.held = true; current.inputAt = performance.now(); };
    // Only a press that began on the list counts: a click in the composer followed by typing is not a scroll.
    const lift = () => { if (current.held) { current.held = false; current.inputAt = performance.now(); } };
    el.addEventListener('pointerdown', press, { passive: true });
    el.addEventListener('mousedown', press, { passive: true });
    window.addEventListener('pointerup', lift, { passive: true });
    window.addEventListener('pointercancel', lift, { passive: true });
    window.addEventListener('mouseup', lift, { passive: true });
    el.addEventListener('wheel', wheel, { passive: true });
    el.addEventListener('keydown', key);
    el.addEventListener('touchstart', touchStart, { passive: true });
    el.addEventListener('touchmove', touchMove, { passive: true });
    current.detach = () => {
      sizes.disconnect();
      children.disconnect();
      el.removeEventListener('pointerdown', press);
      el.removeEventListener('mousedown', press);
      window.removeEventListener('pointerup', lift);
      window.removeEventListener('pointercancel', lift);
      window.removeEventListener('mouseup', lift);
      el.removeEventListener('wheel', wheel);
      el.removeEventListener('keydown', key);
      el.removeEventListener('touchstart', touchStart);
      el.removeEventListener('touchmove', touchMove);
    };
    pin();
  }, [containerRef, pin, setFollowing]);

  // Returns the distance from the bottom so the caller can decide about its jump buttons.
  const onScroll = useCallback(event => {
    const el = event.currentTarget || event.target;
    const current = state.current;
    const distance = distanceFromBottom(el);
    const following = nextFollow({
      following: current.following, top: el.scrollTop, previousTop: current.top, distance,
      userMoved: current.held || performance.now() - current.inputAt < INPUT_WINDOW_MS,
    });
    current.top = el.scrollTop;
    if (following !== current.following) setFollowing(following);
    // The position moved without the reader (a layout clamp): put it back before the frame paints.
    if (following && distance > 1) { pin(); return distanceFromBottom(el); }
    return distance;
  }, [pin, setFollowing]);

  const follow = useCallback(() => {
    setFollowing(true);
    pin();
    requestAnimationFrame(pin);   // the messages just added may not be in the page yet
  }, [pin, setFollowing]);

  const release = useCallback(() => setFollowing(false), [setFollowing]);
  const isFollowing = useCallback(() => state.current.following, []);

  useEffect(() => { follow(); }, [resetKey, follow]);      // a conversation opens at its newest message
  useEffect(() => () => state.current.detach?.(), []);

  return { attach, onScroll, follow, release, isFollowing };
}
