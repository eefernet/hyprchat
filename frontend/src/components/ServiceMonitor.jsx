import React, {useEffect, useRef} from 'react';
import {IC} from './icons.jsx';
import EmptyState from './EmptyState.jsx';
import {serviceLabel} from '../serviceLabels.js';
import {serviceStatus} from '../serviceStatus.js';
import {degradedColor} from '../theme.js';
import {paddedDays, barTone, barTitle, serviceState} from '../serviceMonitor.js';
import './serviceMonitor.css';

// The Service Monitor: ninety days of uptime per service. It fills the Settings dialog card in place
// of Settings (main.jsx decides when); Back returns to Settings, the X closes the dialog.
//
// `health` is the /api/health `services` object (state right now), `history` the answer of
// /api/health/history?days=90, or null while it has not arrived.
function ServiceCard({name, data, health, now}) {
  const rateLimited = name === 'searxng' && !!health?.searxng?.rate_limited;
  const state = serviceState(data.current_status, rateLimited);
  const days = paddedDays(data.days, now);
  return <article className="sm-card" aria-label={`${serviceLabel(name)}: ${state.label}`}>
    <div className="sm-card-head">
      <span className="sm-name">{serviceLabel(name)}</span>
      <span className={`sm-state is-${state.tone}`}>{state.label}</span>
    </div>
    {rateLimited && <div className="sm-note">Still working, with fewer search engines for now.</div>}
    <div className="sm-strip" role="img" aria-label={`Uptime over the last ${days.length} days: ${data.uptime_pct}%`}>
      {days.map(day => <i key={day.day} className={`sm-bar is-${barTone(day)}`} title={barTitle(day)}/>)}
    </div>
    <div className="sm-foot">
      <span>{days.length} days ago</span>
      <span className="sm-uptime">{data.uptime_pct}% uptime</span>
      <span>Today</span>
    </div>
    {data.avg_response_ms > 0 && <div className="sm-avg">Average response: {data.avg_response_ms}ms</div>}
  </article>;
}

export default function ServiceMonitor({t, health, history, loading = false, error = '', onBack, onClose, onRefresh}) {
  const summary = serviceStatus(health);
  const services = Object.entries(history?.services || {});
  const now = new Date();
  // The button that opened this view is hidden now, so focus moves to the way back.
  const back = useRef(null);
  useEffect(() => { back.current?.focus({preventScroll: true}); }, []);
  return <section className="sm-root" style={{'--sm-ok': t.ok, '--sm-degraded': degradedColor(t), '--sm-down': t.err}}>
    <header className="sm-head">
      <button ref={back} type="button" className="hc-icon-btn is-lg" aria-label="Back to Settings" title="Back to Settings" onClick={onBack}><IC.ArrowLeft/></button>
      <div className="sm-title">
        <h3>Service Monitor</h3>
        <div className={`sm-summary is-${summary.tone}`} role="status"><i aria-hidden="true"/>{summary.text}</div>
      </div>
      <button type="button" className="hc-button" aria-label="Refresh" onClick={onRefresh} disabled={loading}><IC.Refresh/><span className="sm-refresh-text">{loading ? 'Refreshing…' : 'Refresh'}</span></button>
      <button type="button" className="hc-icon-btn is-lg" aria-label="Close" title="Close" onClick={onClose}><IC.X/></button>
    </header>
    <div className="sm-body">
      {error && !services.length
        ? <EmptyState compact icon={<IC.Activity/>} title="The uptime history did not load" hint={error} action={<button type="button" className="hc-button is-primary" onClick={onRefresh}>Retry</button>}/>
        : !history
          ? <div className="sm-loading" role="status">Loading uptime data…</div>
          : !services.length
            ? <EmptyState compact icon={<IC.Activity/>} title="No health data yet" hint="Checks run every 5 minutes. Data will appear shortly."/>
            : <>
              {error && <div className="sm-error" role="alert">The newest data did not load ({error}). This is the last copy.</div>}
              <div className="sm-grid">
                {services.map(([name, data]) => <ServiceCard key={name} name={name} data={data} health={health} now={now}/>)}
              </div>
              <div className="sm-legend" aria-label="What the colours mean">
                {[['ok', 'Operational'], ['degraded', 'Degraded'], ['down', 'Down'], ['none', 'No data']].map(([tone, label]) =>
                  <span key={tone}><i className={`sm-bar is-${tone}`}/>{label}</span>)}
              </div>
            </>}
    </div>
  </section>;
}
