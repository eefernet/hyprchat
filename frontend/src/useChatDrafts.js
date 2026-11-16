import { useEffect, useRef, useState } from 'react';
import { NEW_CHAT, draftKey, draftOf, dropDraft, fromStored, isLocalOnly, moveDraft, pruneLocal, storageKey, toStored, withAttachments, withText } from './chatDrafts.js';

// The message box as "the draft of the chat on screen" (chatDrafts.js). `text`, `attachments` and their
// setters have the shapes the root component always used, so its call sites did not change.
//
// `ephemeral`: Ghost mode is on, so even the not-yet-created chat's draft must not be written to storage.

export const queueRescueKey = userId => `hc-chat-queue::${userId || 'default'}`;
const SAVE_AFTER_MS = 400;

const read = (store, key) => { try { return store?.getItem(key) || ''; } catch { return ''; } };
const write = (store, key, value) => { try { store?.setItem(key, value); } catch { /* storage full or blocked: the draft stays in memory */ } };

function initial(userId, storage, session) {
  let rescued = {};
  try { rescued = JSON.parse(read(session, queueRescueKey(userId)) || '{}') || {}; session?.removeItem(queueRescueKey(userId)); } catch { rescued = {}; }
  const map = fromStored(read(storage, storageKey(userId)), rescued);
  return { userId, map, changed: new Set(Object.keys(rescued)) };
}

export function useChatDrafts({ chatId, userId, ephemeral = false, storage = globalThis.localStorage, session = globalThis.sessionStorage }) {
  const [held, setHeld] = useState(() => initial(userId, storage, session));
  let state = held;
  if (held.userId !== userId) {                  // another profile: never show the previous one's drafts
    state = initial(userId, storage, session);
    setHeld(state);
  }
  // A chat created by the first send has its id here before React has rendered it.
  const adopted = useRef(null);
  if (chatId) adopted.current = null;
  const key = chatId ? chatId : (adopted.current || NEW_CHAT);
  const keyRef = useRef(key);
  keyRef.current = key;
  const flags = useRef({ ephemeral });
  flags.current.ephemeral = ephemeral;

  const change = (target, fn) => setHeld(current => {
    const map = fn(current.map);
    if (map === current.map) return current;
    current.changed.add(target);
    return { ...current, map };
  });
  const api = useRef(null);
  if (!api.current) api.current = {
    setText: next => { const target = keyRef.current; change(target, map => withText(map, target, next)); },
    setAttachments: next => { const target = keyRef.current; change(target, map => withAttachments(map, target, next)); },
    // For work that finishes later (a PDF being read, a project upload): the chat it was started in.
    setAttachmentsFor: (id, next) => { const target = id ? id : (adopted.current || NEW_CHAT); change(target, map => withAttachments(map, target, next)); },
    adopt: id => {
      if (!id) return;
      adopted.current = id; keyRef.current = id;
      setHeld(current => { current.changed.add(NEW_CHAT); current.changed.add(id); return { ...current, map: moveDraft(current.map, NEW_CHAT, id) }; });
    },
    drop: id => change(draftKey(id), map => dropDraft(map, draftKey(id))),
    clearAll: () => setHeld(current => { Object.keys(current.map).forEach(target => current.changed.add(target)); return { ...current, map: {} }; }),
    pruneLocal: ids => setHeld(current => { const map = pruneLocal(current.map, ids); return map === current.map ? current : { ...current, map }; }),
  };

  // Text goes to storage a moment after typing stops, and at once when the page is hidden or closed.
  const latest = useRef(state);
  latest.current = state;
  const save = useRef(null);
  if (!save.current) save.current = () => {
    const now = latest.current;
    if (!now.changed.size) return;
    const skip = target => isLocalOnly(target) || (target === NEW_CHAT && flags.current.ephemeral);
    write(storage, storageKey(now.userId), JSON.stringify(toStored(read(storage, storageKey(now.userId)), now.map, [...now.changed], { skip })));
    now.changed.clear();
  };
  useEffect(() => {
    const timer = setTimeout(save.current, SAVE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [state.map]);
  useEffect(() => {
    const flush = () => save.current();
    const hidden = () => { if (document.visibilityState === 'hidden') flush(); };
    globalThis.addEventListener?.('pagehide', flush);
    globalThis.document?.addEventListener?.('visibilitychange', hidden);
    return () => { flush(); globalThis.removeEventListener?.('pagehide', flush); globalThis.document?.removeEventListener?.('visibilitychange', hidden); };
  }, [userId]);

  const draft = draftOf(state.map, key);
  return { key, text: draft.text, attachments: draft.attachments, ...api.current };
}
