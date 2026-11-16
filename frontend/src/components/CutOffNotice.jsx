import React from 'react';
import { cutOffLine } from '../continueReply.js';

// The line under a reply that did not finish, with Continue beside it (Retry when there is nothing to continue). It is always visible: the old
// Continue button sat in the toolbar that only shows on hover.
export default function CutOffNotice({ kind, meta, onContinue, disabled, t, font }) {
  const line = cutOffLine(kind, meta);
  if (!line) return null;
  return <div role="note" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', margin: '8px 0 2px', padding: '7px 11px', borderRadius: 'var(--hc-radius)', border: `1px solid ${t.warm}40`, background: `${t.warm}10`, color: t.dim, fontFamily: font, fontSize: 'var(--hc-text-secondary)' }}>
    <span>{line}</span>
    <button type="button" onClick={onContinue} disabled={disabled} style={{ background: `${t.warm}22`, border: `1px solid ${t.warm}55`, color: t.warm, cursor: disabled ? 'default' : 'pointer', padding: '3px 12px', borderRadius: 'var(--hc-radius-sm)', fontFamily: font, fontSize: 'var(--hc-text-control)', fontWeight: 700 }}>{kind === 'retry' ? 'Retry' : 'Continue'}</button>
  </div>;
}
