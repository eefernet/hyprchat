import React from 'react';
import { overflowLine } from '../contextFit.js';

// The line under a reply whose request was larger than the context window: the model answered from a
// part of it, and nothing else on the page says so.
export default function OverflowNotice({ overflow, t, font }) {
  const line = overflowLine(overflow);
  if (!line) return null;
  return <div role="note" style={{ margin: '8px 0 2px', padding: '7px 11px', borderRadius: 'var(--hc-radius)', border: `1px solid ${t.err}40`, background: `${t.err}10`, color: t.dim, fontFamily: font, fontSize: 'var(--hc-text-secondary)', lineHeight: 1.5 }}>
    {line}
  </div>;
}
