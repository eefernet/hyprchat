// Does a message fit the model's context window?
//
// Ollama does not refuse a request that is larger than the window: it cuts it and answers from what is
// left, with no error (backend/chat_overflow.py). Two things tell the user: a line under a reply whose
// request was cut, and a line above the message box before a draft that cannot fit is sent.

const number = value => (Number.isFinite(Number(value)) && Number(value) > 0 ? Math.round(Number(value)) : 0);
const count = value => number(value).toLocaleString();

// The fixed part of every request (instructions and tool definitions), when a reply has not reported it.
const TYPICAL_FIXED = 3500;
const REPLY_RESERVE = 4096, RESERVE_SHARE = 0.25;
const SMALL_DRAFT = 2000;       // characters: nothing this short is worth measuring
const SURE = 0.6;               // tokens per estimated unit used to decide: low, so a draft that fits is not flagged
const TYPICAL = 0.73;           // ...and used for the figure shown (measured for ordinary prose)

// Under a reply whose request did not fit. `overflow` is what the server saved: { needed, window }.
export function overflowLine(overflow) {
  const needed = number(overflow?.needed), window = number(overflow?.window);
  if (!needed || !window) return '';
  return `This request was larger than the context window (about ${count(needed)} of ${count(window)} tokens), so the model read only part of it. Use a larger context size for this model, or send less.`;
}

// In the pill and the breakdown, for a chat whose last request did not fit.
export const overflowWarning = needed => (number(needed)
  ? `The last request needed about ${count(needed)} tokens and did not fit: the model read only part of it.` : '');

// Characters of an attachment that are sent to the model as text with the message.
export function attachmentChars(attachment) {
  if (!attachment || attachment.error || ['image', 'office', 'data'].includes(attachment.type)) return 0;
  return String(attachment.content || '').length;
}

// The draft against the room one message can ever have: the window minus what every request carries and
// minus room for the reply. Null when it fits, when the window is not known (a cloud model), or when the
// draft is small. `ratio` is the chat's measured tokens per estimated unit, when a reply has reported one.
export function draftFit({ text, attachments, window, fixed, ratio } = {}) {
  const size = number(window);
  const chars = String(text || '').length + (attachments || []).reduce((sum, item) => sum + attachmentChars(item), 0);
  if (!size || chars < SMALL_DRAFT) return null;
  const units = chars / 3;
  const room = Math.max(0, size - (number(fixed) || TYPICAL_FIXED) - Math.min(REPLY_RESERVE, Math.floor(size * RESERVE_SHARE)));
  if (units * SURE <= room) return null;
  const rate = Number(ratio) >= 0.4 && Number(ratio) <= 1.2 ? Number(ratio) : TYPICAL;
  return { tokens: Math.round(units * rate), room, window: size };
}

export function draftLine(fit) {
  if (!fit) return '';
  // A very small window is used up by the instructions and tools alone: say that, not "room for about 0".
  const room = fit.room >= 200 ? `has room for about ${count(fit.room)}` : 'has almost no room left for a message';
  return `This message is about ${count(fit.tokens)} tokens. This model's context window (${count(fit.window)}) ${room}, so the model would read only part of it. Use a larger context size or send less.`;
}
