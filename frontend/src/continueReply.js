// Continuing a reply that did not finish, and the note a failed reply carries.
//
// A reply can stop short for three reasons: the model hit its output limit, the user pressed Stop (or the
// connection dropped), or the turn failed. Only the first could be continued, its button sat in the hover
// toolbar where nobody saw it, and it came back after a successful Continue because the "cut off" flag
// lived in the browser's copy of the metadata.

const INTERRUPTED = /\n*_\[interrupted: [^\n]*\]_\s*$/;
const WARNING_BLOCK = /\n*```\n⚠ [^`]*\n```\s*$/;      // the block older versions appended in the browser
const NO_RESPONSE = /^\s*_\[(?:No response|The model produced no text response)/;

const metadataOf = message => {
  const value = message?.metadata;
  if (!value) return {};
  if (typeof value === 'string') { try { return JSON.parse(value) || {}; } catch { return {}; } }
  return value;
};

// The reply's own text, without the failure note under it: what the model is asked to carry on from.
export function continueText(content) {
  return String(content || '').replace(INTERRUPTED, '').replace(WARNING_BLOCK, '');
}

// '' (nothing to continue), 'length' (hit the output limit), 'interrupted' (stopped or failed part-way)
// or 'rounds' (used every tool round it had; the answer is whole, the work may not be).
// `unsaved`: the chat is a Ghost or offline one. Carrying on mid-reply needs the reply's saved row, which
// such a chat never has, so Continue always ended in "Message id not found". Continuing a tool run is a
// new message and still works.
export function continueKind(message, isLast, unsaved = false) {
  if (!isLast || !message || message.role !== 'assistant' || message.isS) return '';
  const body = continueText(message.content);
  if (!body.trim() || NO_RESPONSE.test(body)) return '';
  const meta = metadataOf(message);
  const status = message._activityStatus || meta.delivery_status || '';
  if (['stopped', 'interrupted', 'failed'].includes(status)) return unsaved ? '' : 'interrupted';
  if (meta.truncated) return unsaved ? '' : 'length';
  return roundLimit(meta) ? 'rounds' : '';
}

// A reply that failed or was stopped with nothing to carry on from: only writing it again helps. It used
// to show nothing at all; Regenerate was in the toolbar that appears on hover.
export function needsRetry(message, isLast) {
  if (!isLast || !message || message.role !== 'assistant' || message.isS || continueKind(message, isLast)) return false;
  const status = message._activityStatus || metadataOf(message).delivery_status || '';
  return ['stopped', 'interrupted', 'failed'].includes(status);
}

// How many tool rounds the reply used when it ran out, or 0. Replies saved before 2026-11-15 only say
// that they ran out (`max_rounds_reached`) and which round they were on.
export function roundLimit(meta) {
  const limit = Number(meta?.round_limit) || 0;
  return limit || (meta?.max_rounds_reached ? Number(meta.round) || 12 : 0);
}

// What the Continue button sends for a reply that stopped at the round limit. A new turn, not the
// continue mode used for a half-written reply: that asks the model to carry on mid-sentence.
export const CONTINUE_WORK = 'Continue from where you stopped.';

export function cutOffLine(kind, meta) {
  if (kind === 'rounds') {
    const limit = roundLimit(meta);
    return limit ? `This reply stopped at the limit of ${limit} tool rounds.` : 'This reply stopped at the tool round limit.';
  }
  if (kind === 'retry') return 'This reply ended before anything was written.';
  return kind === 'length' ? 'This reply was cut off at the output limit.'
    : kind === 'interrupted' ? 'This reply stopped before it was finished.' : '';
}

// What the reply shows after an error event. The server keeps the reason on the saved reply
// (`saved_note`): an empty reply already arrived as a one-line note, a partial one gets the reason under
// it, in the same words the server saved. Without `saved_note` (an older server) the old block is used.
// The browser used to add its own ⚠ block in every case, so an empty reply showed the error twice.
export function withFailureNote(full, error, savedNote) {
  const reason = String(error || '').split(/\s+/).filter(Boolean).join(' ').slice(0, 160);
  const text = String(full || '');
  if (!savedNote) return `${text}${text ? '\n\n' : ''}\`\`\`\n⚠ ${error}\n\`\`\``;
  if (!text.trim() || NO_RESPONSE.test(text) || !reason) return text;
  return `${text.replace(/\s+$/, '')}\n\n_[interrupted: ${reason}]_`;
}

// What to show when the server refuses a chat request outright. The reply used to carry the raw response
// body, usually a line of JSON.
export function requestError(status, body) {
  let reason = String(body || '').trim();
  try {
    const detail = JSON.parse(reason)?.detail;
    if (typeof detail === 'string') reason = detail;
    else if (Array.isArray(detail)) reason = detail.map(item => item?.msg || '').filter(Boolean).join('; ') || reason;
  } catch { /* not JSON: show the text itself */ }
  reason = reason.replace(/\s+/g, ' ').slice(0, 300);
  return reason ? `${reason} (HTTP ${status})` : `The server refused the request (HTTP ${status})`;
}
