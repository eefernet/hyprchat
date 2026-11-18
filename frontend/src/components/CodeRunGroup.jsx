import React from 'react';
import { errorLine, foldRuns, runKey, runLabel, runSummary, summaryText } from '../codeRuns.js';
import { useCardMemory } from './CodeCard.jsx';
import './codeRuns.css';

// One run: a thin line saying what it did and how long it took. A failed run shows the line that names
// its error. Opening it shows the code and the output.
function RunRow({ run }) {
  const data = run.data || {};
  const [open, setOpen] = useCardMemory(runKey(run), false);
  const seconds = Number.parseFloat(data.exec_time);
  const error = errorLine(run);
  const label = runLabel(run);
  return <div className="hc-run" data-state={data.success ? 'ok' : 'failed'}>
    <button type="button" className="hc-run-row" aria-expanded={open} title={open ? 'Hide the code and output' : 'Show the code and output'} onClick={() => setOpen(value => !value)}>
      <span className="hc-run-mark" aria-hidden="true" style={{ color: data.success ? 'var(--hc-ok)' : 'var(--hc-danger)' }}>{data.success ? '✓' : '✕'}</span>
      <span className="hc-run-label">{label}<span className="hc-sr-only">{data.success ? ', passed' : ', failed'}</span></span>
      <span className="hc-run-meta"><span className="hc-run-lang">{data.language || 'code'}{Number.isFinite(seconds) ? ' · ' : ''}</span>{Number.isFinite(seconds) ? `${seconds.toFixed(1)}s` : ''}</span>
      <span className="hc-run-chevron" data-open={open} aria-hidden="true">▾</span>
    </button>
    {!open && error && <p className="hc-run-error" title={error}>{error}</p>}
    {open && <div className="hc-run-panel">
      {data.code && <><span className="hc-run-caption">Code{data.code_truncated ? ' (first 2,000 characters)' : ''}</span><pre>{data.code}</pre></>}
      {data.stdout && <><span className="hc-run-caption">Output</span><pre>{data.stdout}</pre></>}
      {data.stderr && <><span className="hc-run-caption">Error</span><pre data-kind="error">{data.stderr}</pre></>}
      {!data.stdout && !data.stderr && <span className="hc-run-caption">{data.success ? 'The run printed nothing.' : 'The run failed without printing anything.'}</span>}
    </div>}
  </div>;
}

// Code runs with no text between them, as one box (codeRuns.js `groupCards`). Three or more get a header
// that adds them up; a long list shows the newest and the failed ones first.
export default function CodeRunGroup({ runs }) {
  const first = runKey(runs[0]);
  const [collapsed, setCollapsed] = useCardMemory(`fold:${first}`, false);
  const [showAll, setShowAll] = useCardMemory(`all:${first}`, false);
  const summary = runSummary(runs);
  const headed = runs.length >= 3;
  const { rows, hidden } = foldRuns(runs, { showAll });
  return <div className="hc-runs" role="group" aria-label={`Code runs (${summary.count})`}>
    {headed && <button type="button" className="hc-runs-head" aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}>
      <span className="hc-run-chevron" data-open={!collapsed} aria-hidden="true">▾</span>
      <span className="hc-run-label">{summary.count} code runs</span>
      <span className="hc-run-meta" style={summary.failed ? { color: 'var(--hc-warm)' } : undefined}>{summaryText(summary)}</span>
    </button>}
    {!(headed && collapsed) && <div className="hc-runs-list">
      {hidden > 0 && <button type="button" className="hc-runs-more" onClick={() => setShowAll(true)}>
        <span className="hc-run-mark" aria-hidden="true">⋯</span><span>Show {hidden} more</span>
      </button>}
      {rows.map(({ run }) => <RunRow key={runKey(run)} run={run}/>)}
    </div>}
  </div>;
}
