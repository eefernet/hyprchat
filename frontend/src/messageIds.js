// Which saved row a message on screen is.
//
// The browser used to learn the id of an assistant reply only when its stream finished, and never the id
// of the message the user had just sent. Actions on such rows (delete, edit, regenerate, rate, fork)
// either did nothing on the server, so a deleted message came back after a reload, or looked the row up
// by its POSITION in the list, which is wrong whenever the two lists differ (a greeting that was never
// saved, a row removed elsewhere). The stream's first event now carries both ids; position is only the
// last resort, and only when the lists line up.

const text = message => String(message?.content ?? '');

// The user row a reply answers: the last user message before the reply that is being written.
export function withUserId(messages, userMessageId) {
  const list = messages || [];
  if (userMessageId == null || !userMessageId) return list;
  for (let index = list.length - 1; index >= 0; index -= 1) {
    if (list[index]?.role !== 'user') continue;
    if (list[index].id === userMessageId) return list;
    const next = [...list];
    next[index] = { ...list[index], id: userMessageId };
    return next;
  }
  return list;
}

// The id of the saved row for `local` (the message at `index` on screen), or null when it cannot be told.
// Same role and same text wins, nearest position first; position alone counts only if both lists have
// the same length, so a shifted list never points at a neighbour.
export function findSavedMessage(saved, local, index, localCount = null) {
  if (local?.id != null) return local.id;
  const rows = saved || [];
  if (!local) return null;
  const same = rows
    .map((row, at) => ({ row, at }))
    .filter(({ row }) => row?.role === local.role && text(row) === text(local) && row.id != null)
    .sort((a, b) => Math.abs(a.at - index) - Math.abs(b.at - index));
  if (same.length) return same[0].row.id;
  const candidate = rows[index];
  const aligned = localCount == null || localCount === rows.length;
  return aligned && candidate?.role === local.role && candidate.id != null ? candidate.id : null;
}
