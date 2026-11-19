import React from 'react';
import {IC} from '../icons.jsx';
import {fitView, ESTIMATE_FIELDS, KV_CHOICES} from '../../modelsPage.js';

const FALLBACK_CATEGORIES = [{id: 'for_you', label: 'For You'}];
const HARDWARE_FIELDS = [['gpu_count', 'Graphics cards', 0, 16], ['total_vram_gb', 'Graphics memory (GB)', 0, 512], ['system_ram_gb', 'System memory (GB)', 1, 2048]];

// Recommended for this server: models ranked by how well they fit the hardware on the left. Each card
// says in one sentence what the model needs, and installs with one click.
export default function RecommendedTab({m}) {
  const {t, btnS, inputS, fit} = m;
  const data = fit.data, profile = fit.profile;
  const categories = data?.categories?.length ? data.categories : FALLBACK_CATEGORIES;
  const active = categories.some(c => c.id === fit.category) ? fit.category : 'for_you';
  const grouped = data?.grouped_recommendations || {};
  const recs = grouped[active] || (active === 'for_you' ? data?.recommendations || [] : []);
  const set = (key, value) => fit.setProfile(p => ({...p, [key]: value}));
  const status = fit.rescanStatus?.detection_mode ? fit.rescanCopy(fit.rescanStatus) : null;
  const installing = name => (m.downloads || []).some(d => d.name === name && !['done', 'error'].includes(d.status));

  return <div className="mp-body">
    <div className="mp-side">
      <div className="mp-side-list">
        <div className="mp-h">This server</div>
        <p className="mp-muted" style={{margin: '4px 0 12px'}}>Recommendations are worked out from these numbers. Rescan reads them from the machine Ollama runs on; you can also correct them by hand.</p>
        {!profile ? <div className="mp-empty">{fit.loading ? 'Reading the hardware…' : 'The hardware could not be read. Try Rescan.'}</div> : <>
          <div className="mp-form">
            <label className="mp-field is-wide"><span>Graphics card</span>
              <input value={profile.gpu_name || ''} onChange={e => set('gpu_name', e.target.value)} placeholder="e.g. RTX 3090" style={inputS}/></label>
            {HARDWARE_FIELDS.map(([key, label, min, max]) => <label key={key} className="mp-field"><span>{label}</span>
              <input type="number" min={min} max={max} step="1" value={profile[key] ?? ''} onChange={e => set(key, Number(e.target.value))} style={inputS}/></label>)}
          </div>
          <details className="mp-fold" style={{marginTop: 12}}>
            <summary><span>Advanced</span><span className="mp-muted">Estimate fine-tuning</span></summary>
            <div className="mp-fold-body" style={{paddingLeft: 0}}>
              <p className="mp-muted" style={{margin: '0 0 10px'}}>These only change the estimates on this page. They do not change how Ollama or HyprChat runs.</p>
              {ESTIMATE_FIELDS.map(field => <label key={field.key} className="mp-field" style={{marginBottom: 10}}><span>{field.label}</span>
                <input type="number" min={field.min} max={field.max} step="1" value={profile[field.key] ?? ''} onChange={e => set(field.key, Number(e.target.value))} style={{...inputS, maxWidth: 120}}/>
                <small>{field.help}</small></label>)}
              <label className="mp-field" style={{marginBottom: 10}}><span>Memory format for the context (KV cache)</span>
                <select value={profile.kv_cache_type || 'q8_0'} onChange={e => set('kv_cache_type', e.target.value)} style={inputS}>{KV_CHOICES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
                <small>Matches Ollama’s OLLAMA_KV_CACHE_TYPE. Smaller formats leave more room for the model.</small></label>
              <label className="mp-inline mp-muted" style={{cursor: 'pointer'}}><input type="checkbox" checked={!!profile.sched_spread} onChange={e => set('sched_spread', e.target.checked)} style={{accentColor: t.acc, flex: 'none'}}/> One model is spread across all graphics cards</label>
              <label className="mp-field" style={{marginTop: 10}}><span>Name of this profile</span>
                <input value={profile.name || ''} onChange={e => set('name', e.target.value)} style={inputS}/></label>
            </div>
          </details>
          <div className="mp-actions" style={{marginTop: 12}}>
            <button type="button" onClick={fit.save} disabled={fit.saving} style={{...btnS(t.ok), opacity: fit.saving ? .65 : 1}}>{fit.saving ? 'Saving…' : 'Save'}</button>
            <button type="button" onClick={fit.rescan} disabled={fit.rescanning} style={{...btnS(t.warm), opacity: fit.rescanning ? .65 : 1}}><IC.Activity/> {fit.rescanning ? 'Scanning…' : 'Rescan hardware'}</button>
          </div>
          {status && <div className={`mp-note ${status.type === 'success' ? 'is-ok' : status.type === 'error' ? 'is-bad' : 'is-warn'}`} role="status">{status.text}{fit.rescanStatus?.message ? `: ${fit.rescanStatus.message}` : ''}</div>}
        </>}
        {data?.assumptions?.length > 0 && <details className="mp-fold" style={{marginTop: 14}}>
          <summary><span>How the estimates are made</span></summary>
          <div className="mp-fold-body" style={{paddingLeft: 0}}>{data.assumptions.map((line, i) => <p key={i} className="mp-muted" style={{margin: '0 0 6px'}}>{line}</p>)}</div>
        </details>}
      </div>
    </div>
    <div className="mp-main is-wide">
      <div className="mp-chips" role="group" aria-label="Kind of model" style={{marginBottom: 10}}>
        {categories.map(c => <button key={c.id} type="button" className="mp-chipbtn" aria-pressed={c.id === active} onClick={() => fit.setCategory(c.id)}>{c.label}{(c.count ?? (grouped[c.id] || []).length) ? ` · ${c.count ?? (grouped[c.id] || []).length}` : ''}</button>)}
      </div>
      <div className="mp-inline" style={{marginBottom: 14, flexWrap: 'wrap'}}>
        <button type="button" role="switch" className="mp-chipbtn" aria-checked={fit.live} disabled={fit.loading} onClick={() => { const next = !fit.live; fit.setLive(next); fit.load(false, next); }}>Also show what is trending on Hugging Face: {fit.live ? 'On' : 'Off'}</button>
        <span className="mp-muted">{fit.live ? 'Trending models are not checked by hand.' : 'Showing the hand-picked list.'}</span>
      </div>
      {data?.ollama_error && <div className="mp-note is-warn" style={{marginBottom: 10}}>Ollama could not be reached, so “installed” marks may be missing: {data.ollama_error}</div>}
      {data?.hf_error && <div className="mp-note" style={{marginBottom: 10}}>Hugging Face could not be reached; showing the hand-picked list only. ({data.hf_error})</div>}
      {fit.loading && !data ? <div className="mp-empty">Working out what fits…</div>
        : !recs.length ? <div className="mp-empty">Nothing to recommend here for this hardware.</div>
        : <div className="mp-grid">{recs.map(rec => {
          const view = fitView(rec, data?.profile || profile);
          const busy = installing(rec.pull_name);
          return <article key={`${active}-${rec.id}`} className="mp-card mp-rec">
            <div className="mp-headrow" style={{flexWrap: 'nowrap'}}>
              <div style={{minWidth: 0}}>
                <div className="mp-h" style={{overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'}} title={rec.name}>{rec.name}</div>
                <div className="mp-muted" style={{overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'}} title={rec.pull_name}>{rec.purpose}</div>
              </div>
              <span className={`mp-chip is-${view.tone === 'muted' ? 'small' : view.tone}`} style={{flexShrink: 0}}>{view.label}</span>
            </div>
            <div className="mp-p" style={{flex: 1}}>{rec.summary}</div>
            {view.percent > 0 && <div className="mp-bar-track" role="img" aria-label={`Uses ${view.percent}% of this server's memory`}><div className={`mp-bar-fill is-${view.tone}`} style={{width: `${view.percent}%`}}/></div>}
            <div className="mp-muted">{view.sentence}</div>
            {(view.tone === 'warn' || view.tone === 'bad') && rec.fit_note && <div className="mp-muted">{rec.fit_note}</div>}
            <div className="mp-actions">
              {rec.installed ? <>
                <span className="mp-chip is-ok">Installed</span>
                <button type="button" onClick={() => m.useModel(rec.installed_name || rec.pull_name)} style={btnS(t.acc)}>Use in chat</button>
              </> : <button type="button" disabled={busy || !rec.pull_name} onClick={() => m.pullModel(rec.pull_name)} style={{...btnS(view.tone === 'bad' ? t.mut : t.ok), opacity: busy ? .6 : 1}}><IC.Download/> {busy ? 'Installing…' : 'Install'}</button>}
              {/hugging/i.test(rec.source || '') && <span className="mp-chip is-small">from Hugging Face</span>}
            </div>
          </article>;
        })}</div>}
    </div>
  </div>;
}
