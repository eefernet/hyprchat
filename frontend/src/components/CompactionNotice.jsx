import React, { useState } from 'react';
import { apiJson } from '../api.js';

// The line in a chat where compaction cut it: everything above is sent to the model as one summary
// (backend/chat_context.py). The summary is fetched only when the reader asks for it.
export default function CompactionNotice({ conversationId, t, font }) {
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState(null);
  const [failed, setFailed] = useState(false);
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || summary != null) return;
    try {
      const conversation = await apiJson(`/api/conversations/${conversationId}`);
      setSummary(String(conversation?.summary || '').trim());
    } catch { setFailed(true); }
  };
  return <div className="hc-compaction-notice" style={{ margin: '6px 0 18px', fontFamily: font }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <div style={{ flex: 1, height: 1, background: 'var(--hc-line)' }}/>
      <button type="button" onClick={toggle} aria-expanded={open} style={{ background: 'transparent', border: '1px solid var(--hc-line)', borderRadius: 999, padding: '4px 12px', color: t.mut, cursor: 'pointer', fontFamily: 'inherit', fontSize: 'var(--hc-text-secondary)', fontWeight: 600 }}>
        Earlier messages are summarized for the model · {open ? 'hide' : 'show'} summary
      </button>
      <div style={{ flex: 1, height: 1, background: 'var(--hc-line)' }}/>
    </div>
    {open && <div style={{ margin: '10px auto 0', maxWidth: 720, padding: '10px 14px', borderRadius: 'var(--hc-radius-card)', border: '1px solid var(--hc-line-subtle)', background: t.bgDeep, color: t.dim, fontSize: 'var(--hc-text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
      {failed ? 'The summary could not be loaded.' : summary == null ? 'Loading…' : summary || 'No summary is stored for this chat.'}
    </div>}
  </div>;
}
