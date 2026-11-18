// Rules for the conversation list that need no React. Pure, so node:test can import them.

export const isLocalChat = c => !!c?.ephemeral || /^(ghost|l)-/.test(String(c?.id || ''));

// Deleting a chat: the list without it and the chat to show next (only when the deleted one was open).
// The caller has to LOAD that next chat: list rows carry no messages, and showing one unloaded made it
// look empty, so the next message went out with no history and replaced the chat's title.
export function afterDelete(convs, id, openId) {
  const list = (convs || []).filter(c => c.id !== id);
  if (openId !== id) return {list, nextId: openId ?? null, changed: false};
  const next = list.find(c => !isLocalChat(c)) || list[0] || null;
  return {list, nextId: next ? next.id : null, changed: true};
}

// A fresh list from the server joins the list on screen: chats already loaded keep their messages and
// local fields, chats only this tab has (ghost, offline) stay. Replacing the list emptied every open chat.
export function mergeListed(prev, listed, normalize = row => row) {
  const known = new Map((prev || []).map(c => [c.id, c]));
  const seen = new Set();
  const merged = (listed || []).map(row => {
    seen.add(row.id);
    const old = known.get(row.id);
    const fresh = normalize(row);
    return old ? {...old, ...fresh, messages: old.messages || []} : {...fresh, messages: []};
  });
  const localOnly = (prev || []).filter(c => !seen.has(c.id) && isLocalChat(c));
  return [...localOnly, ...merged];
}

// A title check after a reply must look at the chat as it is NOW. The copy from when Send was pressed
// still had the placeholder, so a rename made while the reply streamed was replaced by a generated title.
export const currentChat = (convs, id, fallback = null) => (convs || []).find(c => c.id === id) || fallback;
