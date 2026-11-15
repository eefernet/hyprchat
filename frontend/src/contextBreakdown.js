// What fills the context window, as rows for the popover the context pill opens.
//
// The server splits each request into categories (backend/context_breakdown.py) and sends the split with
// the reading: while a reply runs it describes the window right now, afterwards what the conversation
// keeps. Totals are the model's own counts; the split between rows is an estimate scaled to that total.

import { compactionLimit, roomLeft } from './contextMeter.js';

// In the order they are listed. `kept: false` rows are in the window for this turn or this reply only.
export const CATEGORIES = [
  { key: 'instructions', label: 'Instructions', color: 'var(--hc-f4)', kept: true },
  { key: 'tools', label: 'Tool definitions', color: 'var(--hc-warm)', kept: true },
  { key: 'memory', label: 'Memory', color: 'var(--hc-pink)', kept: true },
  { key: 'skills', label: 'Skills', color: 'var(--hc-acc2)', kept: true },
  { key: 'summary', label: 'Summary of earlier messages', color: 'color-mix(in srgb, var(--hc-f4) 45%, var(--hc-pink))', kept: true },
  { key: 'messages', label: 'Messages', color: 'var(--hc-ok)', kept: true },
  // What earlier replies' tools returned, sent again so the model does not redo the work (chat_tool_memory.py).
  { key: 'kept_tools', label: 'Kept tool results', color: 'color-mix(in srgb, var(--hc-ok) 45%, var(--hc-accent))', kept: true },
  { key: 'excerpts', label: 'Search and knowledge excerpts', note: 'this turn', color: 'var(--hc-f1)', kept: false },
  { key: 'tool_calls', label: 'Tool calls and results', note: 'this reply', color: 'var(--hc-accent)', kept: false },
  { key: 'reply', label: 'Reply being written', color: 'var(--hc-secondary)', kept: false },
];
const LEGACY = [
  { key: 'fixed', label: 'Instructions and tools', color: 'var(--hc-f4)', kept: true },
  { key: 'conversation', label: 'Conversation', color: 'var(--hc-ok)', kept: true },
  { key: 'working', label: "This reply's working context", note: 'not kept', color: 'var(--hc-accent)', kept: false },
];
const FREE_COLOR = 'var(--hc-line-strong)';
const PEAK_MIN = 200, PEAK_SHARE = 0.05, PEAK_ROW_MIN = 100;

const number = value => (Number.isFinite(Number(value)) && Number(value) > 0 ? Math.round(Number(value)) : 0);

// The split, made to add up to `total`. It can be a little off: a message typed since the reading was
// saved, or text that arrived between two readings. Messages take a small difference; a large one means
// the split is stale and is scaled.
function fitted(parts, total) {
  const out = {};
  CATEGORIES.forEach(({ key }) => { out[key] = number(parts?.[key]); });
  const sum = Object.values(out).reduce((a, b) => a + b, 0);
  if (!sum || !total) return null;
  const diff = total - sum;
  if (diff >= 0 || out.messages + diff >= 0) { out.messages += diff; return out; }
  let left = total;
  CATEGORIES.forEach(({ key }, index) => {
    const share = index === CATEGORIES.length - 1 ? left : Math.min(left, Math.round((out[key] * total) / sum));
    out[key] = share; left -= share;
  });
  return out;
}

const rowsFrom = (list, values, window, total) => list
  .map(item => ({ ...item, tokens: number(values[item.key]) }))
  .filter(row => row.tokens > 0)
  .map(row => ({ ...row, share: (window || total) > 0 ? row.tokens / (window || total) : 0 }));

// Everything the popover draws. `live`: a reply is running in this chat.
export function breakdownView(reading, { fallbackWindow = 0, compactPercent = 0, live = false } = {}) {
  const used = number(reading?.used), working = live ? number(reading?.working) : 0;
  const total = used + working;
  const window = number(reading?.window) || number(fallbackWindow);
  const parts = fitted(reading?.parts, total);
  const fixed = Math.min(number(reading?.fixed), used);
  const rows = parts
    ? rowsFrom(CATEGORIES, parts, window, total)
    : rowsFrom(LEGACY, { fixed, conversation: used - fixed, working }, window, total);
  const free = window > 0 ? Math.max(0, window - total) : 0;
  const { limit, capped } = compactionLimit(window, compactPercent);
  const standing = parts ? parts.instructions + parts.tools + parts.memory + parts.skills : fixed;
  const { room, tight } = standing ? roomLeft(window, standing) : { room: 0, tight: false };
  // What the last reply held at its fullest and no longer does. Worth a line only when it was a real
  // share of the chat; the reply's own draft and thinking are not listed (the answer itself is kept).
  const peakTotal = number(reading?.peak?.total);
  const dropped = peakTotal - used;
  const peak = !live && dropped >= Math.max(PEAK_MIN, used * PEAK_SHARE) ? {
    total: peakTotal, dropped,
    rows: rowsFrom(CATEGORIES.filter(item => !item.kept && item.key !== 'reply'), reading.peak.parts || {}, window, peakTotal)
      .filter(row => row.tokens >= PEAK_ROW_MIN),
  } : null;
  return {
    total, used, working, window, live,
    percent: window > 0 ? Math.min(100, (total / window) * 100) : null,
    rows, free, freeShare: window > 0 ? free / window : 0, freeColor: FREE_COLOR,
    legacy: !parts, estimate: !!reading && !reading.measured, empty: !reading,
    compactAt: limit, compactPercent: limit ? compactPercent : 0, capped,
    warning: tight ? `Only about ${room.toLocaleString()} tokens are left for the conversation and replies. Raise the context size in Settings.` : '',
    peak,
  };
}

export const percentText = share => {
  const value = share * 100;
  return value > 0 && value < 0.1 ? '<0.1%' : `${value.toFixed(value < 10 ? 1 : 0)}%`;
};
