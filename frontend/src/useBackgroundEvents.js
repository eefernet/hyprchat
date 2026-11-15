import { useEffect, useRef } from 'react';
import { userScopedUrl } from './session.js';

// Keeps listening to one conversation's event stream while a different chat is on screen.
//
// The app's main event stream follows the chat being shown, and the server keeps no replay. A reply that
// was still running when the reader opened another chat therefore lost every tool event sent in the
// meantime: no code output, no download card, no sources, for good (found 2026-11-13). `conversationId`
// is the chat that is replying in the background, or null.
export function useBackgroundEvents(conversationId, onEvent) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  useEffect(() => {
    if (!conversationId) return undefined;
    const source = new EventSource(userScopedUrl(`/api/events/${conversationId}`));
    source.onmessage = message => {
      try {
        const event = JSON.parse(message.data);
        if (event.type !== 'heartbeat' && event.type !== 'keepalive') handler.current(event);
      } catch { /* a malformed frame is not worth losing the stream for */ }
    };
    return () => source.close();
  }, [conversationId]);
}
