// One reply stream per tab. These rules say which chat owns it and what a chat load, a re-read or a
// chat that is going away may do while it runs. Pure, so node:test can import them.

// A run found on the server when a chat is opened (a research run, an old workflow) may take the
// composer only when no reply is streaming. It used to take it from a reply streaming in another chat:
// that chat's events were then saved on the wrong reply and Stop ended the wrong one.
export const canAdoptRun = streamingCid => !streamingCid;

// A chat load must not replace what this chat's own stream put on screen: not while it streams, and not
// when the stream ended after the load was requested (the answer is then older than the screen).
export function keepsLocalMessages({chatId, streamingCid, loadStartedAt, streamEndedAt}) {
  if (streamingCid != null && streamingCid === chatId) return true;
  return streamEndedAt != null && loadStartedAt != null && streamEndedAt >= loadStartedAt;
}

// The chat that is replying is about to leave the list (a ghost chat being left, a chat being deleted).
// Its reply has to be stopped first, or it runs on with no Stop button and blocks every other send.
export const strandsStream = (streamingCid, goneIds) =>
  streamingCid != null && (goneIds || []).some(id => id === streamingCid);

// After a request failed, the saved reply may replace the error bubble only when it is the row this
// stream announced. "The last saved assistant row" was the previous turn whenever the request was
// refused before anything was saved, and the bubble then became a copy of that reply.
export function failedReplyRow(savedMessages, announcedId) {
  if (announcedId == null) return null;
  const row = (savedMessages || []).find(m => m && m.id === announcedId && m.role === 'assistant');
  return row && String(row.content || '').trim() ? row : null;
}

// Each stream gets a number. Only the stream that still holds the newest number may release the
// composer when it ends, so a reply that ends late cannot release one that started after it.
export function createStreamTurns() {
  let latest = 0;
  return {
    begin() { latest += 1; return latest; },
    isLatest: turn => turn === latest,
  };
}
