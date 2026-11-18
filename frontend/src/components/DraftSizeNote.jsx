import React from 'react';
import { draftFit, draftLine } from '../contextFit.js';

// Above the message box, before a draft that cannot fit the model's window is sent.
export default function DraftSizeNote({ text, attachments, window, fixed, ratio, maxWidth, t, font }) {
  const line = draftLine(draftFit({ text, attachments, window, fixed, ratio }));
  if (!line) return null;
  return <div role="status" style={{ maxWidth, margin: '0 auto 7px', padding: '6px 11px', borderRadius: 'var(--hc-radius)', border: `1px solid ${t.warm}40`, background: `${t.warm}10`, color: t.warm, fontFamily: font, fontSize: 'var(--hc-text-secondary)', lineHeight: 1.45 }}>
    {line}
  </div>;
}
