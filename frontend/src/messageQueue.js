// Messages typed while a reply is still being written.
//
// The message box used to be locked for as long as a reply ran, and Enter did nothing. Now a message sent
// meanwhile is queued for that chat and goes out when the reply has finished. One finished reply sends
// one message; each waits for its own reply.
//
// The queue goes on only after a reply that really finished (runState.replyOutcome === 'completed') in
// the chat on screen. After anything else it is held until "Send now": the user stopped the reply, it
// failed or lost its connection, it stopped at the tool-round or output limit (sending on would take
// away its Continue), it handed work to a background job, or it finished while another chat was open.
//
// State: { [chatId]: { items: [{ id, text, attachments }], hold: '', ready: false, sending: null } }

const EMPTY = Object.freeze({ items: Object.freeze([]), hold: '', ready: false, sending: null });

export const queueFor = (state, chatId) => (state && chatId && state[chatId]) || EMPTY;

function put(state, chatId, entry) {
  const copy = { ...(state || {}) };
  if (!entry || !entry.items.length) delete copy[chatId];      // an empty queue has nothing to hold
  else copy[chatId] = entry;
  return copy;
}

export function enqueue(state, chatId, item) {
  if (!chatId || !item || (!String(item.text || '').trim() && !(item.attachments || []).length)) return state || {};
  const current = queueFor(state, chatId);
  return put(state, chatId, { ...current, items: [...current.items, { id: item.id, text: String(item.text || ''), attachments: item.attachments || [] }] });
}

export function removeItem(state, chatId, id) {
  const current = queueFor(state, chatId);
  if (!current.items.some(item => item.id === id)) return state || {};
  return put(state, chatId, { ...current, items: current.items.filter(item => item.id !== id), sending: current.sending === id ? null : current.sending });
}

// A reply of this chat ended. `outcome` is runState.replyOutcome's; `onScreen`: the chat is the one shown.
export function settle(state, chatId, outcome, onScreen) {
  const current = queueFor(state, chatId);
  if (!current.items.length || current.hold) return state || {};      // a held queue stays held until "Send now"
  if (outcome === 'completed' && onScreen) return put(state, chatId, { ...current, ready: true });
  return put(state, chatId, { ...current, ready: false, hold: outcome === 'completed' ? 'away' : outcome || 'unknown' });
}

export function hold(state, chatId, reason) {
  const current = queueFor(state, chatId);
  return current.items.length ? put(state, chatId, { ...current, ready: false, hold: reason || 'unknown' }) : (state || {});
}

// "Send now".
export function release(state, chatId) {
  const current = queueFor(state, chatId);
  return current.items.length ? put(state, chatId, { ...current, hold: '', ready: true }) : (state || {});
}

// The chat was left before its next message went out: it waits for "Send now" there.
export function leave(state, chatId) {
  const current = queueFor(state, chatId);
  return current.ready && !current.sending ? put(state, chatId, { ...current, ready: false, hold: 'away' }) : (state || {});
}

export function nextToSend(state, chatId) {
  const current = queueFor(state, chatId);
  return current.ready && !current.sending && !current.hold ? current.items[0] || null : null;
}

export function dispatched(state, chatId, id) {
  const current = queueFor(state, chatId);
  return current.items.some(item => item.id === id) ? put(state, chatId, { ...current, ready: false, sending: id }) : (state || {});
}

// The send attempt is over. A message that was not accepted stays queued, held, so it cannot loop.
export function finished(state, chatId, id, accepted) {
  const current = queueFor(state, chatId);
  if (current.sending !== id) return state || {};
  return put(state, chatId, { ...current, sending: null, ...(accepted ? {} : { hold: 'unsent', ready: false }) });
}

export function renameChat(state, from, to) {
  if (!state || !state[from] || from === to) return state || {};
  const copy = { ...state };
  const moved = copy[from];
  delete copy[from];
  copy[to] = copy[to] ? { ...copy[to], items: [...copy[to].items, ...moved.items] } : moved;
  return copy;
}

export function dropChat(state, chatId) {
  if (!state || !(chatId in state)) return state || {};
  const copy = { ...state };
  delete copy[chatId];
  return copy;
}

export function pruneLocal(state, liveIds) {
  const live = new Set(liveIds || []);
  const gone = Object.keys(state || {}).filter(key => /^(ghost|l)-/.test(key) && !live.has(key));
  return gone.length ? gone.reduce((copy, key) => dropChat(copy, key), state) : (state || {});
}

// What pressing Enter or Send does right now.
//   'queue'  this chat is replying (or a queued message is going out): add to the queue
//   'wait'   ...but an attachment is still being read
//   'send'   everything else: the ordinary send, with all its own checks and notices
//   'none'   nothing to send, or a council chat that is busy (councils have no queue)
export function submitAction({ streamingHere, dispatching, council, hasText, attachments, uploading } = {}) {
  const files = attachments || [];
  if (!hasText && !files.length) return 'none';
  if (!(streamingHere || dispatching)) return 'send';
  if (council) return 'none';
  if (uploading || files.some(file => file?.loading)) return 'wait';
  return 'queue';
}

const REASONS = {
  stopped: 'You stopped the reply, so the queue is waiting.',
  failed: 'The reply failed, so the queue is waiting.',
  interrupted: 'The connection was lost, so the queue is waiting.',
  rounds: 'The reply stopped at its tool-round limit, so the queue is waiting.',
  cutoff: 'The reply was cut off at the output limit, so the queue is waiting.',
  job: 'The reply started a background job, so the queue is waiting.',
  away: 'The reply finished while another chat was open, so the queue is waiting.',
  unsent: 'A queued message could not be sent, so the queue is waiting.',
};
export const holdLine = reason => REASONS[reason] || 'The queue is waiting.';

export function queuedLabel(item, max = 90) {
  const text = String(item?.text || '').replace(/\s+/g, ' ').trim();
  const files = (item?.attachments || []).length;
  const shown = text.length > max ? `${text.slice(0, max - 1)}…` : text;
  return shown || (files ? `${files} attachment${files === 1 ? '' : 's'}` : '');
}

// The text of queued messages of saved chats, so a reload turns them back into drafts instead of losing them.
export function rescueTexts(state) {
  const out = {};
  for (const [chatId, entry] of Object.entries(state || {})) {
    if (/^(ghost|l)-/.test(chatId)) continue;
    const texts = entry.items.map(item => item.text).filter(text => String(text || '').trim());
    if (texts.length) out[chatId] = texts;
  }
  return out;
}
