// Drafts: the text and attachments in the message box, kept with the chat they were typed in.
//
// The box used to be one piece of state for the whole app. Text typed in one chat was still in the box
// after switching to another (and could be sent there by mistake), and a reload lost it. Here every chat
// has its own draft in a map; the text is also kept in localStorage per user so it survives a reload.
// Attachments stay in memory only: they hold file contents and upload handles.

export const NEW_CHAT = '~new';                       // the chat that has no id yet
export const draftKey = chatId => chatId || NEW_CHAT;
export const isLocalOnly = key => /^(ghost|l)-/.test(String(key || ''));   // never written to storage
const EMPTY = Object.freeze({ text: '', attachments: Object.freeze([]), at: 0 });
const MAX_STORED = 50;

export const draftOf = (map, key) => (map && map[key]) || EMPTY;

const resolve = (next, previous) => (typeof next === 'function' ? next(previous) : next);

// A draft with nothing in it leaves nothing behind.
function put(map, key, draft) {
  const copy = { ...(map || {}) };
  if (!draft.text && !(draft.attachments || []).length) delete copy[key];
  else copy[key] = draft;
  return copy;
}

export function withText(map, key, next, now = Date.now()) {
  const current = draftOf(map, key);
  const text = String(resolve(next, current.text) ?? '');
  return text === current.text ? (map || {}) : put(map, key, { ...current, text, at: now });
}

export function withAttachments(map, key, next) {
  const current = draftOf(map, key);
  const attachments = resolve(next, current.attachments) || [];
  return attachments === current.attachments ? (map || {}) : put(map, key, { ...current, attachments });
}

export const joinText = (first, second) => [first, second].map(part => String(part || '')).filter(part => part.trim()).join('\n\n');

// What was typed before a chat existed follows it to its id. Text already at the destination is kept.
export function moveDraft(map, from, to, now = Date.now()) {
  const source = draftOf(map, from);
  if (from === to || source === EMPTY) return map || {};
  const target = draftOf(map, to);
  const copy = { ...(map || {}) };
  delete copy[from];
  return put(copy, to, { text: joinText(target.text, source.text), attachments: [...target.attachments, ...source.attachments], at: now });
}

export function dropDraft(map, key) {
  if (!map || !(key in map)) return map || {};
  const copy = { ...map };
  delete copy[key];
  return copy;
}

// Ghost and offline chats disappear without a delete: forget drafts whose chat is gone.
export function pruneLocal(map, liveIds) {
  const live = new Set(liveIds || []);
  const gone = Object.keys(map || {}).filter(key => isLocalOnly(key) && !live.has(key));
  return gone.length ? gone.reduce((copy, key) => dropDraft(copy, key), map) : (map || {});
}

export const storageKey = userId => `hc-chat-drafts::${userId || 'default'}`;

// What storage holds: { [chatId]: { text, at } }. Anything else is ignored.
export function parseStored(raw) {
  let value = raw;
  if (typeof raw === 'string') { try { value = JSON.parse(raw); } catch { return {}; } }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out = {};
  for (const [key, entry] of Object.entries(value)) {
    if (entry && typeof entry.text === 'string' && entry.text) out[key] = { text: entry.text, at: Number(entry.at) || 0 };
  }
  return out;
}

// The stored form after this tab's changes. Only the chats in `changed` are touched, so a second tab's
// drafts are not erased; `skip(key)` keeps a key out (ghost chats); the newest `max` are kept.
export function toStored(stored, map, changed, { skip = isLocalOnly, max = MAX_STORED } = {}) {
  const out = { ...parseStored(stored) };
  for (const key of changed || []) {
    const draft = draftOf(map, key);
    if (draft.text && !skip(key)) out[key] = { text: draft.text, at: draft.at || 0 };
    else delete out[key];
  }
  const newest = Object.entries(out).sort((a, b) => b[1].at - a[1].at).slice(0, max);
  return Object.fromEntries(newest);
}

// The drafts a page starts with: the stored texts, plus the text of messages that were still queued
// when the page was closed (`rescued`: { [chatId]: [text] }), which must not be lost.
export function fromStored(stored, rescued = {}) {
  const map = {};
  for (const [key, entry] of Object.entries(parseStored(stored))) map[key] = { text: entry.text, attachments: [], at: entry.at };
  for (const [key, texts] of Object.entries(rescued || {})) {
    const text = joinText(...[(texts || []).join('\n\n'), map[key]?.text]);
    if (text) map[key] = { text, attachments: [], at: map[key]?.at || Date.now() };
  }
  return map;
}
