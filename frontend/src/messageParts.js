// Puts tool output where it happened in a reply.
//
// The reply text arrives on the chat stream and tool events on a second connection, and nothing in
// either says where in the text a tool ran. So when a card event (code output, a delivered file) arrives
// during a stream, it is stamped with the length of the reply at that moment (`at`). A tool runs after
// its round's text has finished and before the next round's text starts, so that length is the end of
// the text that triggered it. The stamp is saved with the event; a reload places the cards the same way.
//
// Events without a stamp (every message saved before this existed) keep the old layout: after the text.

const CARD_TYPES = new Set(['code_output', 'file_ready']);
const FENCE = /^ {0,3}(```|~~~)/gm;
const FOOTNOTE = /^\[\^[^\]\s]+\]:/m;   // footnotes are resolved across the whole reply, so it cannot be split

export const isCard = event => CARD_TYPES.has(event?.type);
const placed = event => typeof event?.at === 'number' && Number.isFinite(event.at);

export function stampCard(event, textLength) {
  if (isCard(event) && Number.isFinite(textLength)) event.at = Math.max(0, Math.floor(textLength));
  return event;
}

// The visible reply was wiped ("clear": a text-fallback tool round) or swapped for another ("replace":
// the review step). After a clear everything so far happened before the text that follows; after a
// replace the old positions mean nothing.
export function resetOffsets(events, mode) {
  for (const event of events || []) {
    if (!placed(event)) continue;
    if (mode === 'replace') delete event.at;
    else event.at = 0;
  }
}

function insideFence(text, end) {
  const matches = text.slice(0, end).match(FENCE);
  return !!matches && matches.length % 2 === 1;
}

// Where to cut for a card stamped at `at`: the end of the paragraph it landed in, never inside a code block.
// A stamp can land a few characters early (the last tokens of a round still in flight), so look forward.
export function splitPoint(text, at) {
  let point = Math.min(Math.max(0, at), text.length);
  if (point === 0) return 0;
  while (point > 0 && /\s/.test(text[point - 1])) point -= 1;   // step back over the break itself
  for (let next = text.indexOf('\n\n', point); next !== -1; next = text.indexOf('\n\n', next + 2)) {
    if (!insideFence(text, next)) return next;
  }
  return text.length;
}

function fileCards(events) {
  // One card per file name, at its latest position, counting how many times it was delivered.
  const latest = new Map();
  for (const event of events) {
    if (event.type !== 'file_ready') continue;
    const name = event.data?.filename;
    if (!name) continue;
    latest.set(name, { event, versions: (latest.get(name)?.versions || 0) + 1 });
  }
  return latest;
}

// { parts: [{ text, cards }], trailing }. A card is { type, data, versions, at }.
export function messageParts(content, events) {
  const text = String(content || '');
  const source = (events || []).filter(isCard);
  const files = fileCards(source);
  const cards = [];
  for (const event of source) {
    if (event.type === 'file_ready') {
      const kept = files.get(event.data?.filename);
      if (!kept || kept.event !== event) continue;
      cards.push({ type: event.type, data: event.data || {}, versions: kept.versions, at: event.at });
    } else {
      cards.push({ type: event.type, data: event.data || {}, versions: 1, at: event.at });
    }
  }
  if (!cards.some(placed) || FOOTNOTE.test(text)) return { parts: [{ text, cards: [] }], trailing: cards };

  const byPoint = new Map();
  for (const card of cards.filter(placed)) {
    const point = splitPoint(text, card.at);
    if (!byPoint.has(point)) byPoint.set(point, []);
    byPoint.get(point).push(card);
  }
  const parts = [];
  let start = 0;
  for (const point of [...byPoint.keys()].sort((a, b) => a - b)) {
    parts.push({ text: text.slice(start, point), cards: byPoint.get(point) });
    start = point;
  }
  if (text.slice(start).trim()) parts.push({ text: text.slice(start), cards: [] });
  return { parts, trailing: cards.filter(card => !placed(card)) };
}

// Saved events are capped, and a long run is mostly progress ticks. Cards are what the reader sees
// after a reload, so the cap drops other events first, oldest first.
export function capSavedEvents(events, limit = 150) {
  const list = events || [];
  if (list.length <= limit) return list;
  const room = Math.max(0, limit - list.filter(isCard).length);
  let others = list.length - list.filter(isCard).length;
  return list.filter(event => {
    if (isCard(event)) return true;
    others -= 1;
    return others < room;
  });
}

// Continue mode writes one reply in two streams. The second stream's events replace the first's in the
// saved metadata; keep the first stream's cards (their offsets still fit: the text only grew).
export function mergeContinued(prior, next) {
  const earlier = (prior || []).filter(isCard);
  return earlier.length ? [...earlier, ...(next || [])] : next || [];
}
