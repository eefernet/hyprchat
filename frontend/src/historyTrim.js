// Keeping a chat inside the window when auto-compaction is off: the browser drops the oldest messages
// before sending. (With compaction on the server summarizes instead, and cloud models are left alone:
// the local context setting says nothing about their window.)
//
// The size is the rough 4-characters-per-token figure this always used; it only decides how much to drop.

export const roughTokens = messages => (messages || []).reduce((sum, message) => sum + Math.ceil(String(message?.content || '').length / 4), 0);

// The newest messages that fit `limit`, never fewer than the last two. The kept part opens on a user
// message: dropping one message at a time could leave a reply whose question was gone.
export function trimHistory(messages, limit) {
  const list = messages || [];
  if (!(limit > 0) || list.length <= 2) return list;
  let start = 0, size = roughTokens(list);
  while (size > limit && list.length - start > 2) { size -= roughTokens([list[start]]); start += 1; }
  while (start > 0 && start < list.length - 1 && list[start]?.role !== 'user') start += 1;
  return start ? list.slice(start) : list;
}
