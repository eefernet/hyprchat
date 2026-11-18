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

// Where in the saved list the row of the message at `index` can be: after the saved row of the nearest
// earlier message on screen whose id is known, and before that of the nearest later one. Saved rows keep
// the order of the chat, so a row outside this range is some other message, whatever its text.
export function savedRange(saved, onScreen, index) {
  const rows = saved || [];
  const position = new Map(rows.map((row, at) => [String(row?.id), at]));
  const at = message => (message?.id != null && position.has(String(message.id)) ? position.get(String(message.id)) : null);
  let after = -1;
  let before = rows.length;
  const list = onScreen || [];
  for (let i = Math.min(index, list.length) - 1; i >= 0; i -= 1) {
    const found = at(list[i]);
    if (found != null) { after = found; break; }
  }
  for (let i = index + 1; i < list.length; i += 1) {
    const found = at(list[i]);
    if (found != null) { before = found; break; }
  }
  return { after, before };
}

// The id of the saved row for `local` (the message at `index` on screen), or null when it cannot be told.
// Same role and same text wins, nearest position first; position alone counts only if both lists have
// the same length, so a shifted list never points at a neighbour.
//
// `onScreen` is the whole list on screen. With it, only rows between the neighbours' own saved rows count
// (savedRange). Without that limit a reply stopped before it had any text matched an EARLIER empty reply
// (same role, same empty text), and Regenerate / Retry then deleted that row and every turn after it on
// the server (found 2026-11-16: `DELETE …/messages?from_id=8` for the sixteenth message of a chat).
export function findSavedMessage(saved, local, index, localCount = null, onScreen = null) {
  if (local?.id != null) return local.id;
  const rows = saved || [];
  if (!local) return null;
  const { after, before } = onScreen ? savedRange(rows, onScreen, index) : { after: -1, before: rows.length };
  const inRange = at => at > after && at < before;
  const taken = new Set((onScreen || []).filter((message, i) => i !== index && message?.id != null).map(message => String(message.id)));
  const free = row => row?.id != null && !taken.has(String(row.id));
  const same = rows
    .map((row, at) => ({ row, at }))
    .filter(({ row, at }) => inRange(at) && free(row) && row.role === local.role && text(row) === text(local))
    .sort((a, b) => Math.abs(a.at - index) - Math.abs(b.at - index));
  if (same.length) return same[0].row.id;
  const candidate = rows[index];
  const aligned = localCount == null || localCount === rows.length;
  if (aligned && inRange(index) && free(candidate) && candidate.role === local.role) return candidate.id;
  if (!onScreen) return null;
  // Text differs (a reply the server saved with its own note, say) and the lists do not line up: still
  // certain when the range holds exactly one free row of this role and the screen exactly one such message.
  const rowsHere = rows.filter((row, at) => inRange(at) && free(row) && row.role === local.role);
  let first = index;
  while (first > 0 && onScreen[first - 1]?.id == null) first -= 1;
  let last = index;
  while (last < onScreen.length - 1 && onScreen[last + 1]?.id == null) last += 1;
  const messagesHere = onScreen.slice(first, last + 1).filter(message => message?.role === local.role);
  return rowsHere.length === 1 && messagesHere.length === 1 ? rowsHere[0].id : null;
}
