import {useRef} from 'react';

// Which rows of a list just appeared. Rows are identified by POSITION (an
// index at or past the previous row count) and by not having been listed
// before, because chat row keys change when the server assigns an id. A
// change of resetKey (another conversation) marks everything as seen so a
// load never staggers; the container fades instead. Entries stay flagged
// for `ttl` so per-token re-renders keep the class until the animation ends.
export function nextEntering(prev, keys, resetKey, now, ttl = 400) {
  if (prev.resetKey !== resetKey) return {resetKey, keys: keys.slice(), entered: new Map()};
  const previous = new Set(prev.keys);
  const entered = new Map();
  for (const [key, at] of prev.entered) if (now - at < ttl && keys.includes(key)) entered.set(key, at);
  // Only an unseen suffix past the old length counts: appended rows qualify,
  // prepended history and re-keyed existing rows do not.
  for (let i = keys.length - 1; i >= prev.keys.length && !previous.has(keys[i]); i--) {
    if (!entered.has(keys[i])) entered.set(keys[i], now);
  }
  return {resetKey, keys: keys.slice(), entered};
}

export default function useNewKeys(keys, resetKey, ttl = 400, now = Date.now) {
  const ref = useRef({resetKey: undefined, keys: [], entered: new Map()});
  ref.current = nextEntering(ref.current, keys, resetKey, now(), ttl);
  return new Set(ref.current.entered.keys());
}
