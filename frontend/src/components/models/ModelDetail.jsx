import React, {useState} from 'react';
import {IC} from '../icons.jsx';
import {abilities, displayName, formatSize, parameterWords, quantMeaning, CONTEXT_PRESETS, contextLabel, settingsContextLabel, contextNote,
  hasOverrides, STYLE_SLIDERS, sliderValue, toolFix, TEMPLATE_FAMILIES, fromHuggingFace} from '../../modelsPage.js';

// One installed model in plain words: what it can do, how big it is, how much it remembers, then two
// closed sections for the settings most people never touch.
export default function ModelDetail({m, row}) {
  const {t, btnS, inputS} = m;
  const name = row.name;
  const details = m.modelDetails[name] || {}, det = details.details || {};
  const facts = m.modelFacts[name], able = abilities(name, facts);
  const params = m.modelParams[name] || {};
  const setParam = (key, value) => m.setModelParams(p => ({...p, [name]: {...(p[name] || {}), [key]: value}}));
  const quant = String(det.quantization_level || '').toUpperCase();
  const added = details.modified_at ? (() => { try { return new Date(details.modified_at).toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'}); } catch { return ''; } })() : '';

  const ownCtx = params.num_ctx && params.num_ctx !== 'default' ? parseInt(params.num_ctx, 10) || 0 : 0;
  const effectiveCtx = ownCtx || Number(m.numCtx) || 0;
  const note = contextNote(effectiveCtx, facts?.context_limit);
  const styleOwn = STYLE_SLIDERS.filter(s => sliderValue(s.key, params, {}).own).length + (params.top_k && params.top_k !== 'default' ? 1 : 0);

  const fix = toolFix(name, facts);
  const [family, setFamily] = useState(fix?.family || 'chatml');
  const [fixing, setFixing] = useState(false);
  const [fixResult, setFixResult] = useState(null);
  const applyFix = async () => {
    if (fixing) return;
    const ok = await m.confirmAction({title: 'Rewrite this model for tool calling?', body: `"${name}" is re-created under the same name with a chat template that lets it call tools. The downloaded file is kept; only its instructions change. If its replies get worse afterwards, delete the model and install it again.`, confirmLabel: 'Apply the fix'});
    if (!ok) return;
    setFixing(true); setFixResult(null);
    try {
      const hf = fromHuggingFace(name);
      const r = await fetch(`${m.API}/api/models/${encodeURIComponent(name)}/${hf ? 'create-tool-model' : 'fix-template'}`, hf ? {method: 'POST'} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({family})});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setFixResult({ok: true, text: 'Done. Start a new chat with this model and tools switched on to try it.'});
      m.notify({type: 'success', text: 'Tool calling fix applied', detail: name});
      m.forgetInfo(name); m.loadInfo(name, true); m.refreshModels();
    } catch (error) {
      setFixResult({ok: false, text: `The fix failed: ${error.message}`});
      m.notify({type: 'error', text: 'Tool calling fix failed', detail: error.message});
    }
    setFixing(false);
  };
  const info = m.modelInfoCache[name];

  return <div className="hc-reveal">
    <div className="mp-card">
      <div className="mp-headrow">
        <div style={{minWidth: 0, flex: '1 1 240px'}}>
          <div className="mp-h" style={{fontSize: 'var(--hc-text-display)', wordBreak: 'break-word'}}>{displayName(name)}</div>
          <div className="mp-muted">{[added && `Installed ${added}`, fromHuggingFace(name) && 'from Hugging Face'].filter(Boolean).join(' · ') || name}</div>
          {row.aliases.length > 0 && <div className="mp-muted" title="The same downloaded file under another name. Deleting one name keeps the other.">Also listed as: {row.aliases.join(', ')}</div>}
        </div>
        <div className="mp-actions">
          <button type="button" onClick={() => m.useModel(name)} style={btnS(t.acc)}>Use in chat</button>
          <button type="button" className="hc-button is-compact" aria-label={`Delete ${name}`} title="Delete this model" onClick={() => m.deleteModel(name)} style={{color: t.err}}><IC.Trash/></button>
        </div>
      </div>

      <div className="mp-h2">What it can do</div>
      <div className="mp-chips">
        {able.list.map(a => <span key={a.id} className={`mp-chip ${a.id === 'embedding' ? 'is-warn' : 'is-ok'}`} title={a.detail}>{a.label}</span>)}
        {able.missing.map(a => <span key={a.id} className="mp-chip">{a.label}</span>)}
        {!able.list.length && !able.missing.length && <span className="mp-chip">Chat only</span>}
      </div>
      {able.guessed && <div className="mp-muted" style={{marginTop: 4}}>Guessed from the name: Ollama did not report this model’s abilities.</div>}

      <div className="mp-h2">Size</div>
      <div className="mp-value">{[parameterWords(det.parameter_size), details.size && `${formatSize(details.size)} on disk`].filter(Boolean).join(' · ') || 'Unknown'}</div>
      {quant && <div className="mp-muted">Compression {quant}{quantMeaning(quant) ? `: ${quantMeaning(quant)}` : ''}</div>}

      <div className="mp-h2" id="mp-ctx-label">How much it remembers (context window)</div>
      <div className="mp-chips" role="group" aria-labelledby="mp-ctx-label">
        <button type="button" className="mp-chipbtn" aria-pressed={!ownCtx} onClick={() => setParam('num_ctx', '')}>Same as Settings ({settingsContextLabel(m.numCtx)})</button>
        {CONTEXT_PRESETS.map(v => <button key={v} type="button" className="mp-chipbtn" aria-pressed={ownCtx === v} onClick={() => setParam('num_ctx', String(v))}>{contextLabel(v)}</button>)}
        <input aria-label="Another size, in tokens" type="number" min="1024" step="1024" value={ownCtx && !CONTEXT_PRESETS.includes(ownCtx) ? ownCtx : ''} placeholder="Other" onChange={e => setParam('num_ctx', e.target.value)} style={{...inputS, width: 110, flex: 'none'}}/>
      </div>
      <div className={`mp-note${note.tone === 'warn' ? ' is-warn' : ''}`} role="note">{note.text}</div>

      <details className="mp-fold" style={{marginTop: 16}}>
        <summary><span>Response style</span><span className="mp-muted">{styleOwn ? `${styleOwn} of its own` : 'Using your defaults'}</span></summary>
        <div className="mp-fold-body">
          {STYLE_SLIDERS.map(slider => {
            const {own, value} = sliderValue(slider.key, params, {temperature: m.defaultTemp, top_p: m.defaultTopP});
            return <div key={slider.key} className={`mp-slider${own ? '' : ' is-default'}`}>
              <div className="mp-slider-top">
                <label htmlFor={`mp-${slider.key}`} className="mp-value" style={{fontWeight: 600}}>{slider.label}</label>
                <span className="mp-inline">{own && <button type="button" className="mp-link" onClick={() => setParam(slider.key, '')}>Use the default</button>}<span className="mp-value">{own ? value.toFixed(2) : `default (${value.toFixed(2)})`}</span></span>
              </div>
              <div className="mp-muted" style={{margin: '2px 0 6px'}}>{slider.help}</div>
              <input id={`mp-${slider.key}`} type="range" min={slider.min} max={slider.max} step={slider.step} value={value} onChange={e => setParam(slider.key, e.target.value)}/>
              <div className="mp-slider-ends"><span>{slider.ends[0]}</span><span>{slider.ends[1]}</span><span>{slider.ends[2]}</span></div>
            </div>;
          })}
          <label className="mp-field" style={{maxWidth: 260}}>
            <span>Candidate words per step (top-k)</span>
            <input type="number" min="1" max="200" step="1" value={params.top_k && params.top_k !== 'default' ? params.top_k : ''} placeholder="Default" onChange={e => setParam('top_k', e.target.value)} style={inputS}/>
            <small>How many of the most likely next words the model may choose between. Leave empty for the model’s own value.</small>
          </label>
        </div>
      </details>

      <details className="mp-fold" onToggle={e => { if (e.currentTarget.open) m.loadInfo(name); }}>
        <summary><span>Advanced</span><span className="mp-muted">Model file{fix ? ', tool-calling fix' : ''}</span></summary>
        <div className="mp-fold-body">
          {fix && <>
            <div className="mp-value" style={{fontWeight: 600}}>Tool-calling fix</div>
            <p className="mp-muted" style={{margin: '3px 0 8px'}}>{fix.known ? 'Ollama says this model cannot use tools.' : 'If this model ignores tools,'} {fix.known ? 'Some' : 'some'} custom downloads only lack the right chat template; this rewrites the model with one. It does not help a model that was never trained for tools.</p>
            <div className="mp-inline" style={{flexWrap: 'wrap'}}>
              {!fromHuggingFace(name) && <select aria-label="Chat template" value={family} onChange={e => setFamily(e.target.value)} style={{...inputS, flex: '1 1 180px'}}>{TEMPLATE_FAMILIES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>}
              <button type="button" onClick={applyFix} disabled={fixing} style={{...btnS(t.warm), opacity: fixing ? .6 : 1}}>{fixing ? 'Applying…' : 'Apply the fix'}</button>
            </div>
            {fixResult && <div className={`mp-note ${fixResult.ok ? 'is-ok' : 'is-bad'}`} role="status">{fixResult.text}</div>}
          </>}
          <div className="mp-value" style={{fontWeight: 600, marginTop: fix ? 14 : 0}}>Model file</div>
          <p className="mp-muted" style={{margin: '3px 0 0'}}>The instructions Ollama runs this model with (its template and built-in settings).</p>
          <pre className="mp-pre">{info === undefined ? 'Loading…' : info?.modelfile || 'Ollama returned no model file for this model.'}</pre>
        </div>
      </details>

      {hasOverrides(params) && <div style={{borderTop: '1px solid var(--hc-line-subtle)', paddingTop: 12}}>
        <button type="button" className="mp-link" onClick={() => m.setModelParams(p => { const next = {...p}; delete next[name]; return next; })}>Remove this model’s own settings</button>
      </div>}
    </div>
  </div>;
}
