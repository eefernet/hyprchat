import React, {useRef} from 'react';
import {IC} from '../icons.jsx';
import ModelDetail from './ModelDetail.jsx';
import {searchRows, displayName, rowSummary, overrideCount, hasOverrides, settingsContextLabel, STYLE_SLIDERS, GLOSSARY} from '../../modelsPage.js';

// Installed: every local model once, a place to add one by name, and on the right either the selected
// model or the defaults every model starts from.
export default function InstalledTab({m, rows}) {
  const {t, inputS, btnS} = m;
  const shown = searchRows(rows, m.search);
  const selected = rows.find(row => row.name === m.selected || row.aliases.includes(m.selected)) || null;
  const main = useRef(null);
  // On a narrow screen the list and the detail are one column: bring the detail (or the list) into view.
  const stacked = () => main.current && getComputedStyle(main.current.parentElement).flexDirection === 'column';
  const select = name => {
    const next = m.selected === name ? null : name;
    m.setSelected(next);
    if (next) m.loadInfo(next);
    if (next && stacked()) requestAnimationFrame(() => main.current?.scrollIntoView({block: 'start'}));
  };
  const back = () => { m.setSelected(null); main.current?.parentElement?.scrollTo?.({top: 0}); };
  return <div className="mp-body">
    <div className="mp-side">
      <div className="mp-side-head">
        <input value={m.search} onChange={e => m.setSearch(e.target.value)} placeholder="Search installed models" aria-label="Search installed models" style={{...inputS, width: '100%'}}/>
      </div>
      <div className="mp-side-list">
        {!rows.length && <div className="mp-empty">No models are installed yet.<br/>Add one by name below, or pick one under “Recommended for this server”.</div>}
        {rows.length > 0 && !shown.length && <div className="mp-empty">No installed model matches “{m.search}”.</div>}
        {shown.map(row => <button key={row.name} type="button" className="mp-row" aria-current={selected?.name === row.name ? 'true' : undefined} onClick={() => select(row.name)}>
          <div className="mp-row-name">
            <span title={row.name}>{displayName(row.name)}</span>
            {row.huggingFace && <span className="mp-chip is-warn is-small">Hugging Face</span>}
            {hasOverrides(m.modelParams[row.name]) && <span className="mp-chip is-accent is-small" title="This model has its own settings">custom</span>}
          </div>
          <div className="mp-row-sub">{rowSummary(row.name, m.modelDetails[row.name], m.modelFacts[row.name]) || row.name}</div>
        </button>)}
      </div>
      <div className="mp-side-foot">
        <div className="mp-h2" id="mp-add-label">Add a model by name</div>
        <div className="mp-inline">
          <input ref={m.pullInputRef} aria-labelledby="mp-add-label" value={m.pullName} onChange={e => m.setPullName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent?.isComposing) m.pullModel(); }} placeholder="llama3.1:8b" style={inputS}/>
          <button type="button" onClick={() => m.pullModel()} disabled={!m.pullName.trim()} style={{...btnS(t.ok), opacity: m.pullName.trim() ? 1 : .55, flexShrink: 0}}><IC.Download/> Install</button>
        </div>
        <div className="mp-muted" style={{marginTop: 6}}>A name from <a href="https://ollama.com/library" target="_blank" rel="noreferrer" style={{color: t.acc}}>ollama.com/library</a>. Progress shows under Activity at the top.</div>
      </div>
    </div>
    <div className="mp-main" ref={main}>
      {selected && <button type="button" className="mp-link mp-back" onClick={back}>‹ All installed models</button>}
      {selected ? <ModelDetail key={selected.name} m={m} row={selected}/> : <Overview m={m} rows={rows}/>}
    </div>
  </div>;
}

function Overview({m, rows}) {
  const custom = overrideCount(m.modelParams, rows.map(row => row.name));
  return <>
    <div className="mp-card">
      <div className="mp-h">Defaults for every model</div>
      <p className="mp-p" style={{margin: '5px 0 14px'}}>A model uses these unless you give it its own settings. Pick a model on the left to change it alone{custom ? `; ${custom} model${custom === 1 ? ' has' : 's have'} their own now` : ''}.</p>
      {STYLE_SLIDERS.filter(s => s.key !== 'repeat_penalty').map(slider => {
        const value = slider.key === 'temperature' ? m.defaultTemp : m.defaultTopP, set = slider.key === 'temperature' ? m.setDefaultTemp : m.setDefaultTopP;
        return <label key={slider.key} className="mp-slider" style={{display: 'block'}}>
          <div className="mp-slider-top"><span className="mp-value" style={{fontWeight: 600}}>{slider.label}</span><span className="mp-value">{Number(value).toFixed(2)}</span></div>
          <div className="mp-muted" style={{margin: '2px 0 6px'}}>{slider.help}</div>
          <input type="range" min={slider.min} max={slider.max} step={slider.step} value={value} onChange={e => set(parseFloat(e.target.value))}/>
          <div className="mp-slider-ends"><span>{slider.ends[0]}</span><span>{slider.ends[1]}</span><span>{slider.ends[2]}</span></div>
        </label>;
      })}
      <div className="mp-h2">Context window</div>
      <div className="mp-value">{settingsContextLabel(m.numCtx)}</div>
      <div className="mp-muted">Set in Settings → Model &amp; Generation. A model can have its own size here.</div>
    </div>
    <div className="mp-card">
      <div className="mp-h" style={{marginBottom: 10}}>Words used on this page</div>
      <dl className="mp-glossary">{GLOSSARY.map(([term, meaning]) => <React.Fragment key={term}><dt>{term}</dt><dd>{meaning}</dd></React.Fragment>)}</dl>
    </div>
  </>;
}
