// Ordering for requests that can overlap. Pure, so node:test can import them.

// Writes: a save that failed is retried later (after 1.5 s, or from a Retry button). If a newer save of
// the same thing went out in between, the retry would put the OLD value back. Each write takes a number
// per key; a retry goes out only while it still holds the newest number for every key it writes.
export function createSaveOrder() {
  const latest = new Map();
  return {
    begin(keys) {
      const list = (Array.isArray(keys) ? keys : [keys]).filter(Boolean);
      const turns = list.map(key => { const n = (latest.get(key) || 0) + 1; latest.set(key, n); return [key, n]; });
      return {isCurrent: () => turns.every(([key, n]) => latest.get(key) === n)};
    },
  };
}

// Reads: only the newest request's answer is used. `start()` aborts the one before it.
export function createLatest() {
  let n = 0, controller = null;
  return {
    start() {
      n += 1;
      try { controller?.abort(); } catch {}
      controller = typeof AbortController === 'undefined' ? null : new AbortController();
      const mine = n;
      return {signal: controller ? controller.signal : undefined, isCurrent: () => mine === n};
    },
    cancel() { n += 1; try { controller?.abort(); } catch {} controller = null; },
  };
}
