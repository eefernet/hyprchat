import { useEffect, useRef } from 'react';
import { API } from './session.js';

import { runIsLive } from './runState.js';

// After a reload during such a run the composer is put into "replying" so Stop is offered, but there is
// no local stream whose end would release it: it stayed locked until Stop or another reload (found
// 2026-11-13). This watches the restored run and calls `onEnded` when the server says it is over.
// `conversationId` is the chat with the restored run, or null.
export function useRestoredRun(conversationId, onEnded, intervalMs = 4000) {
  const handler = useRef(onEnded);
  handler.current = onEnded;
  useEffect(() => {
    if (!conversationId) return undefined;
    let stopped = false, misses = 0;
    const check = async () => {
      try {
        const response = await fetch(`${API}/api/runs?conversation_id=${encodeURIComponent(conversationId)}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const runs = await response.json();
        if (stopped) return;
        misses = 0;
        if (!(runs || []).some(runIsLive)) { stopped = true; handler.current(conversationId); }
      } catch {
        // The server may be restarting. Give up after a minute rather than hold the composer forever.
        misses += 1;
        if (!stopped && misses >= 15) { stopped = true; handler.current(conversationId); }
      }
    };
    const timer = setInterval(check, intervalMs);
    return () => { stopped = true; clearInterval(timer); };
  }, [conversationId, intervalMs]);
}
