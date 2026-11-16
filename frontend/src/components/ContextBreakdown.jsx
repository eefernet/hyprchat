import React from 'react';
import { breakdownView, percentText } from '../contextBreakdown.js';
import { formatTokens } from '../contextMeter.js';
import { anyAction } from '../contextActions.js';
import ContextActions from './ContextActions.jsx';

// What the context pill opens: a segmented bar and one row per category, then where auto-compaction
// fires and what the last reply held at its fullest. Pure display; ContextMeter owns opening and placing.
export default function ContextBreakdown({ reading, fallbackWindow, compactPercent, live, onInspect, actions }) {
  const view = breakdownView(reading, { fallbackWindow, compactPercent, live });
  const count = value => Math.round(value).toLocaleString();
  const dropped = view.peak?.rows.map(row => `${formatTokens(row.tokens)} of ${row.label.toLowerCase()}`).join(' and ');
  return <>
    <div className="hc-ctx-head">
      <strong>{view.percent == null ? `${formatTokens(view.total)} tokens` : `${view.percent < 10 ? view.percent.toFixed(1) : Math.round(view.percent)}% used`}</strong>
      <span>{view.estimate ? '~' : ''}{formatTokens(view.total)}{view.window ? ` / ${formatTokens(view.window)}` : ''}</span>
    </div>
    {view.window > 0 && <div className="hc-ctx-stack" aria-hidden="true">
      <div className="hc-ctx-stack-track">{view.rows.map(row => <i key={row.key} style={{ width: `${row.share * 100}%`, background: row.color, opacity: row.kept ? 1 : .6 }}/>)}</div>
      {view.compactAt > 0 && <span className="hc-ctx-tick" style={{ left: `${(view.compactAt / view.window) * 100}%` }}/>}
    </div>}
    <dl className="hc-ctx-rows">
      {view.rows.map(row => <React.Fragment key={row.key}>
        <span className="hc-ctx-swatch" aria-hidden="true" style={{ background: row.color, opacity: row.kept ? 1 : .6 }}/>
        <dt className="hc-ctx-label" title={row.label}>{row.label}{row.note && <small>{row.note}</small>}</dt>
        <dd className="hc-ctx-tokens">{formatTokens(row.tokens)}</dd>
        <dd className="hc-ctx-share">{view.window > 0 ? percentText(row.share) : ''}</dd>
      </React.Fragment>)}
      {view.window > 0 && <>
        <span className="hc-ctx-swatch" aria-hidden="true" style={{ background: view.freeColor }}/>
        <dt className="hc-ctx-label" data-free="true">Free space</dt>
        <dd className="hc-ctx-tokens" data-free="true">{formatTokens(view.free)}</dd>
        <dd className="hc-ctx-share" data-free="true">{percentText(view.freeShare)}</dd>
      </>}
    </dl>
    <div className="hc-ctx-notes">
      {view.warning && <div role="alert">{view.warning}</div>}
      {view.empty && <div>No reading yet. The numbers appear with the next reply.</div>}
      {view.compactAt > 0
        ? <div>Auto-compaction at {count(view.compactAt)} tokens{view.capped ? ' (limited to leave room for a reply)' : ` (${view.compactPercent}%)`}.</div>
        : view.window > 0 ? <div>Auto-compaction is off for this chat.</div> : <div>This model's context limit is not known here, so no percentages are shown.</div>}
      {view.peak && <div>While it worked, the last reply held up to {count(view.peak.total)} tokens{dropped ? `, with ${dropped}` : ' (thinking and drafting included)'}. The extra {count(view.peak.dropped)} are not kept.</div>}
      {view.live && view.working > 0 && <div>Rows marked "this turn" or "this reply" are in the window now and are not sent again next turn.</div>}
      {view.legacy && !view.empty && <div>A full breakdown appears after the next reply.</div>}
      {view.estimate && !view.empty && <div>An estimate until the model reports its own count.</div>}
      {!view.legacy && <div>Totals are the model's own counts. The split between rows is an estimate.</div>}
    </div>
    {anyAction(actions?.available) && <ContextActions {...actions}/>}
    {onInspect && <button type="button" className="hc-ctx-link" onClick={onInspect}>See what was sent with the last reply</button>}
  </>;
}
