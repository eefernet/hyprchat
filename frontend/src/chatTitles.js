// Chat titles. A new chat is named after the start of its first message until the title model writes a
// real one. That attempt used to be made exactly once, after the first reply: a first reply that was
// stopped, or a title call that timed out (three on the live server in a week), left the chat with the
// placeholder for good.

const TITLE_CHARS = 40;
const TRY_UNTIL_MESSAGES = 8;       // after a few exchanges a chat that still has no title keeps what it has

const firstRequest = conversation => (conversation?.messages || []).find(message => message?.role === 'user');

// The name a chat gets from its first message.
export function provisionalTitle(message) {
  const names = (message?.metadata?.pdfs || []).map(pdf => pdf?.name).filter(Boolean).join(', ');
  return String(message?.content || names || '').slice(0, TITLE_CHARS);
}

// True while the chat still wears a placeholder: no title, "New Chat", or the start of its first message.
export function needsTitle(conversation) {
  const title = String(conversation?.title || '').trim();
  if (!title || title === 'New Chat') return true;
  const first = firstRequest(conversation);
  if (!first || (conversation.messages || []).length > TRY_UNTIL_MESSAGES) return false;
  return title === provisionalTitle(first).trim();
}

// A title typed by the user: one line, no stray spaces, not longer than a title can sensibly be.
const TITLE_MAX = 80;
export const cleanTitle = text => String(text || '').replace(/\s+/g, ' ').trim().slice(0, TITLE_MAX);

// The change a rename makes, or null when there is nothing to save (empty, or the same title).
export function renameTo(conversation, text) {
  const title = cleanTitle(text);
  return !title || title === String(conversation?.title || '') ? null : { title };
}
