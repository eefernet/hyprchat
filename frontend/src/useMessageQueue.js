import { useEffect, useRef, useState } from 'react';
import { dispatched, dropChat, enqueue, finished, hold, leave, nextToSend, pruneLocal, queueFor, release, removeItem, renameChat, rescueTexts, settle } from './messageQueue.js';
import { queueRescueKey } from './useChatDrafts.js';

// The queue of messages typed while a reply runs (messageQueue.js), and the one place that sends the
// next one. `busy`: something is running or being asked (a reply, a council, loading, a confirm dialog).
// `canSend()`: no send or regenerate is in flight right now. `send(item)`: the root component's send,
// given the queued message; it calls `item.accepted()` at the point where the message is in the chat.
export function useMessageQueue({ chatId, userId, busy, canSend, send, session = globalThis.sessionStorage }) {
  const [state, setState] = useState({});
  const stateRef = useRef(state); stateRef.current = state;
  const sendRef = useRef(send); sendRef.current = send;
  const canSendRef = useRef(canSend); canSendRef.current = canSend;
  const inFlight = useRef(null);
  const counter = useRef(0);
  const [attempts, setAttempts] = useState(0);     // bumped when a send attempt ends, so the next one is looked at again

  const api = useRef(null);
  if (!api.current) api.current = {
    add: (id, item) => setState(s => enqueue(s, id, { ...item, id: `q${Date.now().toString(36)}${(counter.current += 1)}` })),
    remove: (id, itemId) => setState(s => removeItem(s, id, itemId)),
    // Take a message back out (to edit it). Returns it, or null when it is already going out.
    take: (id, itemId) => {
      const entry = queueFor(stateRef.current, id);
      const item = entry.items.find(candidate => candidate.id === itemId);
      if (!item || entry.sending === itemId) return null;
      setState(s => removeItem(s, id, itemId));
      return item;
    },
    release: id => setState(s => release(s, id)),
    hold: (id, reason) => setState(s => hold(s, id, reason)),
    settle: (id, outcome, onScreen) => setState(s => settle(s, id, outcome, onScreen)),
    rename: (from, to) => setState(s => renameChat(s, from, to)),
    drop: id => setState(s => dropChat(s, id)),
    pruneLocal: ids => setState(s => pruneLocal(s, ids)),
    reset: () => setState({}),
  };

  // Another profile never sees this one's queue.
  const owner = useRef(userId);
  useEffect(() => { if (owner.current !== userId) { owner.current = userId; setState({}); } }, [userId]);

  // The next message goes out after React has committed the finished reply (the send builds the history
  // from what is rendered), and after the previous send has released its own guards: hence the timer.
  useEffect(() => {
    if (busy || !nextToSend(state, chatId)) return undefined;
    const timer = setTimeout(() => {
      const item = nextToSend(stateRef.current, chatId);
      if (!item || inFlight.current) return;
      if (!canSendRef.current()) { setState(s => hold(s, chatId, 'unsent')); return; }
      inFlight.current = item.id;
      let accepted = false;
      setState(s => dispatched(s, chatId, item.id));
      Promise.resolve()
        .then(() => sendRef.current({ ...item, queueId: item.id, chatId, accepted: () => { accepted = true; setState(s => removeItem(s, chatId, item.id)); } }))
        .catch(() => {})
        .finally(() => { inFlight.current = null; setState(s => finished(s, chatId, item.id, accepted)); setAttempts(n => n + 1); });
    }, 0);
    return () => clearTimeout(timer);
  }, [state, chatId, busy, attempts]);

  // Leaving a chat whose next message has not gone out yet: it waits there for "Send now".
  useEffect(() => () => setState(s => leave(s, chatId)), [chatId]);

  // Queued text survives a reload as a draft (useChatDrafts reads this once at start).
  useEffect(() => {
    try {
      const texts = rescueTexts(state);
      if (Object.keys(texts).length) session?.setItem(queueRescueKey(userId), JSON.stringify(texts));
      else session?.removeItem(queueRescueKey(userId));
    } catch { /* storage blocked: the queue still works for this page */ }
  }, [state, userId]);

  return { queue: queueFor(state, chatId), all: state, ...api.current };
}
