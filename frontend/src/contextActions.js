// What the owner can do from the context breakdown: summarize the earlier messages now, read the summary
// the chat is using, and forget the tool results it keeps (backend/chat_compact_now.py).

const number = value => (Number.isFinite(Number(value)) && Number(value) > 0 ? Math.round(Number(value)) : 0);
const tokens = value => (number(value) >= 1000 ? `${(number(value) / 1000).toFixed(1)}k` : String(number(value)));
const many = (value, word) => `${number(value)} ${word}${number(value) === 1 ? '' : 's'}`;

// Which actions to offer. `saved`: the chat exists on the server (not a ghost or offline chat).
// `compactPercent`: the auto-compaction setting, 0 where compaction does not apply (cloud, coding chats, off).
export function availableActions({ reading, live, compactPercent, saved, summarized } = {}) {
  const idle = !!saved && !live;
  return {
    compact: idle && number(compactPercent) > 0 && number(reading?.used) > 0,
    forget: idle && number(reading?.parts?.kept_tools) > 0,
    summary: !!saved && !!summarized,
  };
}

export const anyAction = actions => !!actions && (actions.compact || actions.forget || actions.summary);

export function compactLine(result) {
  if (!result) return '';
  if (!result.compacted) return result.reason || 'Nothing was summarized.';
  return `Summarized ${many(result.summarized, 'earlier message')}${number(result.freed) ? `: about ${tokens(result.freed)} tokens freed` : ''}.`;
}

export function forgetLine(result) {
  if (!result) return '';
  if (!number(result.removed)) return 'This chat keeps no tool results.';
  return `Forgot ${many(result.removed, 'kept tool result')}${number(result.freed) ? `: about ${tokens(result.freed)} tokens freed` : ''}.`;
}
