// The context pill's numbers. One reading per conversation: { used, working, window, measured, ratio }.
//
//   used     tokens the conversation holds: what is sent again on the next turn
//   working  tokens only in use while a reply runs (thinking, tool results, search excerpts): not kept
//   window   the context window the server ran the model with (0 = not known)
//
// The server works these out (backend/chat_context.py) and saves the final reading on each reply as
// metadata.context. This file restores it when a chat opens, keeps it sensible between events, and never
// lets it fall to zero: before this, the pill was one global number that was cleared on every send, chat
// switch and reload, and it fell after each reply because it counted thinking and tool results.

const DEFAULT_RATIO = 0.75;   // real tokens per estimated token until a reply reports its own
const encoder = new TextEncoder();

// The server's estimator (bytes / 3) scaled by how that compared with real counts so far.
export function estimateTokens(text, ratio = DEFAULT_RATIO) {
  const bytes = encoder.encode(String(text || '')).length;
  return bytes ? Math.ceil((bytes / 3) * (ratio > 0 ? ratio : DEFAULT_RATIO)) : 0;
}

const meta = message => {
  const value = message?.metadata;
  if (value && typeof value === 'object') return value;
  if (typeof value === 'string') { try { return JSON.parse(value) || {}; } catch { return {}; } }
  return {};
};

const number = value => (Number.isFinite(Number(value)) && Number(value) > 0 ? Math.round(Number(value)) : 0);

function saved(message) {
  const context = meta(message).context;
  if (context && number(context.used)) {
    return { used: number(context.used), working: 0, window: number(context.window), measured: !!context.measured, ratio: Number(context.ratio) || DEFAULT_RATIO, fixed: number(context.fixed) };
  }
  // Replies saved before readings existed still carry each model call's real counts.
  const first = (meta(message).usage_calls || [])[0];
  if (first && number(first.prompt_tokens)) {
    return { used: number(first.prompt_tokens) + estimateTokens(message.content), working: 0, window: 0, measured: false, ratio: DEFAULT_RATIO };
  }
  return null;
}

// The reading a chat has when it is opened: the newest reply that carries one, plus anything said since.
export function fromMessages(messages) {
  const list = messages || [];
  for (let index = list.length - 1; index >= 0; index--) {
    if (list[index]?.role !== 'assistant') continue;
    const reading = saved(list[index]);
    if (!reading) continue;
    const later = list.slice(index + 1).reduce((sum, message) => sum + estimateTokens(message?.content, reading.ratio), 0);
    return later ? { ...reading, used: reading.used + later, measured: false } : reading;
  }
  return null;
}

// What to show the moment a reply starts, before the server has said anything. `messages` is the chat as
// sent: after an edit or a regenerate the removed replies are already gone, so the reading goes back with them.
export function atStreamStart(messages, previous) {
  const list = (messages || []).filter(message => !(message?.role === 'assistant' && !String(message.content || '').trim()));
  const restored = fromMessages(list);
  if (restored) return { ...restored, working: 0 };
  const guess = list.reduce((sum, message) => sum + estimateTokens(message?.content), 0);
  return { used: guess, working: 0, window: previous?.window || 0, measured: false, ratio: DEFAULT_RATIO };
}

// A `ctx_update` from the server. A real count replaces the reading; an estimate may raise it, never lower it.
export function applyUpdate(reading, event) {
  const current = reading || { used: 0, working: 0, window: 0, measured: false, ratio: DEFAULT_RATIO };
  if (event?.used == null) return current;
  const used = event.measured || current.compacted ? number(event.used) : Math.max(current.used, number(event.used));
  return { ...current, used, working: number(event.working), window: number(event.window) || current.window, measured: !!event.measured, compacted: event.measured ? false : current.compacted, fixed: number(event.fixed) || current.fixed || 0 };
}

// `done.context`: the reply is finished and what it used while working is gone.
export function settle(reading, context) {
  if (!context || !number(context.used)) return reading ? { ...reading, working: 0 } : reading;
  return { used: number(context.used), working: 0, window: number(context.window) || reading?.window || 0, measured: !!context.measured, ratio: Number(context.ratio) || reading?.ratio || DEFAULT_RATIO, fixed: number(context.fixed) || reading?.fixed || 0 };
}

// `context_compacted`: earlier messages were summarized, so the conversation really is smaller now.
export function compacted(reading, event) {
  if (!reading) return reading;
  return { ...reading, used: Math.max(0, reading.used - number(event?.freed)), compacted: true };
}

// When a chat is compacted. Same rule as backend/chat_context.py compaction_limit(): the chosen share of the
// window, but never so late that the next reply (or the summary) has no room.
export const COMPACT_CHOICES = [85, 90, 95, 98, 99];
const REPLY_RESERVE = 4096;
const RESERVE_SHARE = 0.25;

export function compactionLimit(window, percent) {
  const size = number(window);
  if (!size || !percent) return { limit: 0, capped: false };
  const wanted = Math.round((size * percent) / 100);
  const latest = size - Math.min(REPLY_RESERVE, Math.floor(size * RESERVE_SHARE));
  return { limit: Math.min(wanted, latest), capped: wanted > latest };
}

// HyprChat's own instructions and tool definitions are sent with every request: about 3,500 tokens for
// an ordinary chat. A window that leaves less than this much for the conversation and the reply cuts
// replies off, and no compaction can help (a 4,096 window left about 540: live test 2026-11-13).
export const TYPICAL_FIXED = 3500;
const MIN_ROOM = 2048;

export function roomLeft(window, fixed = TYPICAL_FIXED) {
  const size = number(window);
  if (!size) return { room: 0, tight: false };
  const room = Math.max(0, size - (number(fixed) || TYPICAL_FIXED));
  return { room, tight: room < MIN_ROOM };
}

export const formatTokens = value => (value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(Math.round(value || 0)));

// Everything the pill draws. `fallbackWindow` is the browser's own idea of the window, used until a reply reports one.
// `compactPercent` is the Settings choice, or 0 when auto-compaction does not apply.
export function meterView(reading, fallbackWindow = 0, compactPercent = 0) {
  const used = reading?.used || 0;
  const working = reading?.working || 0;
  const window = reading?.window || number(fallbackWindow);
  const share = value => (window > 0 ? Math.min(1, value / window) : 0);
  const fill = share(used + working);
  const tone = window > 0 ? (fill > 0.85 ? 'full' : fill > 0.6 ? 'high' : 'ok') : 'unknown';
  const parts = [`Conversation: ${used.toLocaleString()} tokens${window > 0 ? ` of ${window.toLocaleString()} (${Math.round(share(used) * 100)}%)` : ''}`];
  if (working > 0) parts.push(`This reply's working context: +${working.toLocaleString()} (thinking, tool results; not kept)`);
  const fixed = reading?.fixed || 0;
  if (fixed) parts.push(`Instructions and tools, sent every turn: ${fixed.toLocaleString()}`);
  const { room, tight } = fixed ? roomLeft(window, fixed) : { room: 0, tight: false };
  const warning = tight ? `Only about ${room.toLocaleString()} tokens are left for the conversation and replies. Replies will be cut off: raise the context size in Settings.` : '';
  if (warning) parts.unshift(warning);
  const { limit } = compactionLimit(window, compactPercent);
  if (limit) parts.push(`Auto-compaction at ${limit.toLocaleString()} tokens`);
  if (reading && !reading.measured) parts.push('Estimate until the model reports its count');
  if (!reading) parts.push('No reply yet');
  return { used, working, window, usedShare: share(used), workingShare: Math.max(0, fill - share(used)), tone: warning ? 'full' : tone, warning, tooltip: parts.join('\n') };
}

// Where the "earlier messages are summarized" line goes: the index of the first message after the cut,
// or -1. Messages without a database id are new, so they are after it.
export function compactionIndex(messages, cursor) {
  const cut = number(cursor);
  if (!cut) return -1;
  let last = -1;
  (messages || []).forEach((message, index) => { const id = number(message?.id); if (id && id <= cut) last = index; });
  return last >= 0 && last < (messages || []).length - 1 ? last + 1 : -1;
}
