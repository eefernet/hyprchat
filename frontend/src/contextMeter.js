// The context pill's numbers. One reading per conversation: { used, working, window, measured, ratio }.
//
//   used     tokens the conversation holds: what is sent again on the next turn
//   working  tokens only in use while a reply runs (thinking, tool results, search excerpts): not kept
//   window   the context window the server ran the model with (0 = not known)
//   parts    the same tokens by category, when the server sent a split (contextBreakdown.js draws it):
//            while a reply runs they add up to used + working, afterwards to used
//   peak     { total, parts } of the fullest moment of the last reply
//   model    the model that wrote the reply the reading came from, and
//   asked    the context size the chat was set to then: when either no longer matches the chat, the
//            window above is not the one the next reply will get (forDisplay)
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

const isSplit = value => !!value && typeof value === 'object' && !Array.isArray(value);
// The category split and the peak, only when the server sent them (older replies have neither).
const split = context => ({
  ...(isSplit(context?.parts) ? { parts: context.parts } : {}),
  ...(isSplit(context?.peak) && isSplit(context.peak.parts) ? { peak: context.peak } : {}),
});
const without = (reading, ...keys) => { const copy = { ...reading }; keys.forEach(key => delete copy[key]); return copy; };
// What a reading was taken with, only when known (readings are compared key by key in the tests).
const takenWith = (model, asked) => ({ ...(model ? { model: String(model) } : {}), ...(number(asked) ? { asked: number(asked) } : {}) });
// The request of the reply did not fit the window: how many tokens it needed (backend/chat_overflow.py).
const cut = context => (number(context?.overflow) ? { overflow: number(context.overflow) } : {});

function saved(message) {
  const context = meta(message).context;
  if (context && number(context.used)) {
    return { used: number(context.used), working: 0, window: number(context.window), measured: !!context.measured, ratio: Number(context.ratio) || DEFAULT_RATIO, fixed: number(context.fixed), ...split(context), ...takenWith(meta(message).model, context.asked), ...cut(context) };
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
  if (restored) return { ...without(restored, 'peak'), working: 0 };      // the peak was the previous reply's
  const guess = list.reduce((sum, message) => sum + estimateTokens(message?.content), 0);
  return { used: guess, working: 0, window: previous?.window || 0, measured: false, ratio: DEFAULT_RATIO };
}

// A `ctx_update` from the server. A real count replaces the reading; an estimate may raise it, never lower it.
export function applyUpdate(reading, event) {
  const current = reading || { used: 0, working: 0, window: 0, measured: false, ratio: DEFAULT_RATIO };
  if (event?.used == null) return current;
  const used = event.measured || current.compacted ? number(event.used) : Math.max(current.used, number(event.used));
  return { ...without(current, 'peak', ...(isSplit(event.parts) ? [] : ['parts'])), used, working: number(event.working), window: number(event.window) || current.window, measured: !!event.measured, compacted: event.measured ? false : current.compacted, fixed: number(event.fixed) || current.fixed || 0, ...(isSplit(event.parts) ? { parts: event.parts } : {}) };
}

// `done.context`: the reply is finished and what it used while working is gone.
export function settle(reading, context) {
  // Without a final reading (a stopped reply) the split still describes the moment it stopped: drop it.
  if (!context || !number(context.used)) return reading ? { ...without(reading, 'parts', 'peak'), working: 0 } : reading;
  return { used: number(context.used), working: 0, window: number(context.window) || reading?.window || 0, measured: !!context.measured, ratio: Number(context.ratio) || reading?.ratio || DEFAULT_RATIO, fixed: number(context.fixed) || reading?.fixed || 0, ...split(context), ...takenWith(reading?.model, context.asked || reading?.asked), ...cut(context) };
}

// The window a chat's next reply will get: the per-model setting, else the default, and never more than
// the model's own limit (0 = not known; backend/model_limits.py). One rule for the pill and the request:
// they used to read the per-model setting differently, so an unreadable value gave the pill no window.
export function chatWindow(perModel, fallback, limit = 0) {
  const own = perModel === undefined || perModel === null || perModel === '' || perModel === 'default' ? NaN : parseFloat(perModel);
  const asked = number(Number.isFinite(own) && own > 0 ? own : fallback);
  return { asked, window: number(limit) && asked ? Math.min(asked, number(limit)) : asked };
}

// A reading describes the reply it came from. Once the chat is switched to another model, or its context
// size is changed, the next reply runs in a different window (the pill kept showing the old one, also
// after a switch to a cloud model whose window is not known at all).
export function staleWindow(reading, expect) {
  if (!reading || !expect) return false;
  if (reading.model && expect.model && expect.model !== 'auto' && reading.model !== expect.model) return true;
  return !!(reading.asked && expect.asked && reading.asked !== expect.asked);
}

// What the pill and the breakdown draw: the reading, without a window that no longer applies.
export const forDisplay = (reading, expect) => (staleWindow(reading, expect) ? { ...reading, window: 0 } : reading);

// A reply is about to run with this model and context size: the reading is now about that.
export function forRequest(reading, expect) {
  if (!reading) return reading;
  return { ...forDisplay(reading, expect), ...takenWith(expect?.model || reading.model, expect?.asked || reading.asked) };
}

// What to keep on the browser's own copy of a reply that ended without a final reading (stopped, or the
// connection was lost): enough for the next send to start from it instead of from a guess that leaves
// out the instructions and tools (the pill dipped by about 3k tokens until the first count arrived).
export function keptReading(reading) {
  if (!reading || !number(reading.used)) return null;
  return { used: number(reading.used), window: number(reading.window), measured: false, ratio: Number(reading.ratio) || DEFAULT_RATIO, ...(number(reading.fixed) ? { fixed: number(reading.fixed) } : {}), ...(number(reading.asked) ? { asked: number(reading.asked) } : {}) };
}

// Tokens per second while a reply streams, from the stream's own running count of generated tokens.
// `previous` is the sample returned last time; a count that went down is a new model call starting over.
export function liveSpeed(previous, tokens, now) {
  const count = number(tokens);
  if (!previous || count < previous.tokens) return { at: now, tokens: count, speed: 0 };
  const seconds = (now - previous.at) / 1000;
  if (seconds < 1) return previous;
  const speed = (count - previous.tokens) / seconds;
  return { at: now, tokens: count, speed: speed >= 10 ? Math.round(speed) : Math.round(speed * 10) / 10 };
}

// `context_compacted`: earlier messages were summarized, so the conversation really is smaller now.
// Only `freed_history` came out of the conversation. Room made by shortening this reply's own tool results
// (`summarized: 0`) was working context and never part of `used`.
export function compacted(reading, event) {
  if (!reading) return reading;
  const freed = event?.freed_history != null ? number(event.freed_history) : event?.summarized === 0 ? 0 : number(event?.freed);
  return freed ? { ...without(reading, 'parts'), used: Math.max(0, reading.used - freed), compacted: true } : reading;
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
  const warning = number(reading?.overflow)
    ? `The last request needed about ${number(reading.overflow).toLocaleString()} tokens and did not fit: the model read only part of it.`
    : tight ? `Only about ${room.toLocaleString()} tokens are left for the conversation and replies. Replies will be cut off: raise the context size in Settings.` : '';
  if (warning) parts.unshift(warning);
  const { limit } = compactionLimit(window, compactPercent);
  if (limit) parts.push(`Auto-compaction at ${limit.toLocaleString()} tokens`);
  if (reading && !reading.measured) parts.push('Estimate until the model reports its count');
  if (!reading) parts.push('No reply yet');
  return { used, working, total: used + working, window, percent: window > 0 ? Math.round(fill * 100) : null, usedShare: share(used), workingShare: Math.max(0, fill - share(used)), tone: warning ? 'full' : tone, warning, tooltip: parts.join('\n') };
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
