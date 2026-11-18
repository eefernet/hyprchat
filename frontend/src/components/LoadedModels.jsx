import React, {useCallback, useEffect, useState} from 'react';
import {LIMIT_CHOICES, limitChoice, limitLabel, limitLine, loadedLine, modelRow} from '../loadedModels.js';
import './loadedModels.css';

const REFRESH_MS = 10000;

// Settings → Model & Generation: how many models HyprChat keeps loaded in Ollama at once, and what is
// loaded now (GET /api/models/loaded). Wording and numbers come from loadedModels.js. `active` is false
// while the page is not on screen, so nothing is fetched then.
export default function LoadedModels({limit, onLimit, active = true}) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const chosen = limitChoice(limit);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/models/loaded');
      setStatus(response.ok ? await response.json() : {reachable: false, models: []});
    } catch {
      setStatus({reachable: false, models: []});
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active) return undefined;
    refresh();
    const timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const rows = (status?.models || []).map(row => modelRow(row));
  return <div className="lm-root">
    <div className="lm-label" id="lm-limit-label">Models loaded at once</div>
    <div className="lm-options" role="group" aria-labelledby="lm-limit-label">
      {LIMIT_CHOICES.map(choice => <button key={choice} type="button" className="lm-option" aria-pressed={chosen === choice}
        onClick={() => onLimit(choice)}>{limitLabel(choice)}</button>)}
    </div>
    <p className="lm-note">{limitLine(chosen)}{chosen ? ' Embedding and helper models are not counted, and a model that is busy finishes its reply first.' : ''}</p>
    <div className="lm-loaded">
      <div className="lm-loaded-head">
        <span aria-live="polite">{loadedLine(status)}</span>
        <button type="button" className="lm-refresh" onClick={refresh} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>
      </div>
      {rows.length > 0 && <ul className="lm-list" aria-label="Models loaded now">
        {rows.map(row => <li key={row.name} className="lm-row">
          <span className="lm-name">{row.name}{row.helper && <i className="lm-tag">helper</i>}</span>
          <span className="lm-detail">{row.detail}{row.spill && <b className="lm-spill">{row.detail ? ' · ' : ''}{row.spill}</b>}</span>
        </li>)}
      </ul>}
    </div>
    <p className="lm-note">This can lower how many models stay loaded, not raise it: Ollama's own limit and the memory of its machine still apply.</p>
  </div>;
}
