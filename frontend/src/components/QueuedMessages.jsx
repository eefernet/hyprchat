import React from 'react';
import { holdLine, queuedLabel } from '../messageQueue.js';
import './queuedMessages.css';

// The messages waiting for the reply to finish, just above the message box. Click one to put it back in
// the box; × removes it. A queue that is waiting says why and offers "Send now".
export default function QueuedMessages({ queue, idle, onEdit, onRemove, onSendNow, maxWidth }) {
  if (!queue || !queue.items.length) return null;
  const waiting = idle && !queue.ready && !queue.sending;
  return <div className="hc-queue" role="group" aria-label="Queued messages" style={{ maxWidth }}>
    <div className="hc-queue-head">
      <span>{waiting ? holdLine(queue.hold) : queue.sending ? 'Sending the next queued message…' : `Queued: sent in order when this reply finishes`}</span>
      {waiting && <button type="button" className="hc-queue-now" onClick={onSendNow}>Send now</button>}
    </div>
    <ol className="hc-queue-list">
      {queue.items.map(item => {
        const going = queue.sending === item.id;
        const files = (item.attachments || []).length;
        return <li key={item.id} className="hc-queue-item" data-sending={going ? 'true' : 'false'}>
          <button type="button" className="hc-queue-text" disabled={going} onClick={() => onEdit(item.id)} title="Put this message back in the box to edit it">
            {queuedLabel(item)}{files > 0 && <span className="hc-queue-files"> · {files} file{files === 1 ? '' : 's'}</span>}
          </button>
          <button type="button" className="hc-queue-remove" disabled={going} onClick={() => onRemove(item.id)} aria-label="Remove this queued message" title="Remove">×</button>
        </li>;
      })}
    </ol>
  </div>;
}
