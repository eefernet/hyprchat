// A deleted message is only hidden for a few seconds (Undo) before the server is asked to delete it.
// This keeps track of the ones still waiting, so a chat reload in that window does not bring them back,
// Undo never inserts a second copy, and leaving the profile or the page does not leave a timer behind.
// No React and no fetch in here: the caller passes `commit`.

const same = (a, b) => a && b && (a.id != null && b.id != null ? a.id === b.id
  : a.role === b.role && String(a.content || '') === String(b.content || ''));

// Messages of one chat without those waiting to be deleted.
export function withoutPending(messages, waiting) {
  if (!waiting || !waiting.length) return messages || [];
  const left = [...waiting];
  return (messages || []).filter(message => {
    const at = left.findIndex(item => same(item, message));
    if (at < 0) return true;
    left.splice(at, 1);   // one waiting delete hides one message
    return false;
  });
}

// Undo: back where it was, unless it is already on screen (a reload showed it again).
export function restoreAt(messages, removed, index) {
  const list = [...(messages || [])];
  if (list.some(message => same(message, removed))) return list;
  list.splice(Math.max(0, Math.min(index, list.length)), 0, removed);
  return list;
}

export function createPendingDeletes({delay = 5000, setTimer = setTimeout, clearTimer = clearTimeout} = {}) {
  const items = new Map();
  const run = (token, how) => {
    const item = items.get(token);
    if (!item) return undefined;
    items.delete(token);
    clearTimer(item.timer);
    return item.commit(how);
  };
  return {
    // `commit` deletes on the server; it runs once, after `delay`, unless undone first.
    add(token, {chatId, message, index, commit}) {
      items.set(token, {chatId, message, index, commit, timer: setTimer(() => run(token), delay)});
    },
    // Returns the entry when Undo came in time, null when the delete already went out.
    undo(token) {
      const item = items.get(token);
      if (!item) return null;
      items.delete(token);
      clearTimer(item.timer);
      return item;
    },
    waiting: chatId => [...items.values()].filter(item => item.chatId === chatId).map(item => item.message),
    // Page close: the user already chose to delete, so do it now. `how` reaches each commit.
    flush(how) { return [...items.keys()].map(token => run(token, how)); },
    // Profile switch: requests of the old profile are aborted, so nothing may fire later under the new
    // one. The messages stay on the server, as if Undo had been pressed.
    clear() { for (const item of items.values()) clearTimer(item.timer); items.clear(); },
    get size() { return items.size; },
  };
}
