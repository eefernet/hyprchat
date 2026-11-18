import React, { useState } from 'react';
import { compactLine, forgetLine } from '../contextActions.js';

// The actions at the bottom of the context breakdown. Each one reports what it did in a line of its own;
// forgetting tool results asks first, because it cannot be undone.
export default function ContextActions({ available, onCompact, onForget, loadSummary }) {
  const [busy, setBusy] = useState('');
  const [line, setLine] = useState('');
  const [asking, setAsking] = useState(false);
  const [summary, setSummary] = useState(null);
  const [open, setOpen] = useState(false);
  const run = (name, action, describe) => async () => {
    setBusy(name); setLine(''); setAsking(false);
    try { setLine(describe(await action())); }
    catch (error) { setLine(`That did not work: ${error?.message || error}`); }
    finally { setBusy(''); }
  };
  const compact = run('compact', async () => { const result = await onCompact(); if (result?.compacted) setSummary(null); return result; }, compactLine);
  const forget = run('forget', onForget, forgetLine);
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || summary != null) return;
    try { setSummary(String(await loadSummary() || '').trim()); }
    catch { setSummary(''); setLine('The summary could not be loaded.'); }
  };
  return <div className="hc-ctx-actions">
    <div className="hc-ctx-action-row">
      {available.compact && <button type="button" className="hc-ctx-act" disabled={!!busy} onClick={compact} title="Summarize earlier messages now instead of waiting for the limit">{busy === 'compact' ? 'Summarizing…' : 'Compact now'}</button>}
      {available.summary && <button type="button" className="hc-ctx-act" aria-expanded={open} onClick={toggle}>{open ? 'Hide the summary' : 'Read the summary'}</button>}
      {available.forget && !asking && <button type="button" className="hc-ctx-act" disabled={!!busy} onClick={() => setAsking(true)}>{busy === 'forget' ? 'Forgetting…' : 'Forget kept tool results'}</button>}
    </div>
    {asking && <div className="hc-ctx-ask" role="group" aria-label="Forget kept tool results">
      <span>The model will no longer see what earlier replies' tools returned. This cannot be undone.</span>
      <button type="button" className="hc-ctx-act" data-danger="true" onClick={forget}>Forget them</button>
      <button type="button" className="hc-ctx-act" onClick={() => setAsking(false)}>Keep</button>
    </div>}
    {line && <div className="hc-ctx-result" role="status">{line}</div>}
    {open && <div className="hc-ctx-summary">{summary == null ? 'Loading…' : summary || 'No summary is stored for this chat.'}</div>}
  </div>;
}
