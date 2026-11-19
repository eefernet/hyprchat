import React from 'react';
import {IC} from '../components/icons.jsx';
import InstalledTab from '../components/models/InstalledTab.jsx';
import RecommendedTab from '../components/models/RecommendedTab.jsx';
import HuggingFaceTab from '../components/models/HuggingFaceTab.jsx';
import {installedRows} from '../modelsPage.js';
import '../components/models/modelsPage.css';

// The Models page. Tabs are named for what you want to do: see what is installed, find something that
// runs well on this server, or browse Hugging Face. State and the handlers that talk to the server stay
// in the root component and arrive as `m`; wording and rules are in modelsPage.js.
//
// The tab ids are the old ones ("ollama" | "hyprfit" | "hf") because other code switches tabs by id.
const TABS = [['ollama', 'Installed'], ['hyprfit', 'Recommended for this server'], ['hf', 'Browse Hugging Face']];

export default function ModelsPanel({m}) {
  const {t, tab, setTab} = m;
  const rows = installedRows(m.models, m.modelDetails);
  const fromHf = rows.filter(row => row.huggingFace).length;
  const refresh = tab === 'hyprfit' ? () => m.fit.load(true) : tab === 'hf' && m.hf.search.trim() ? () => { m.refreshModels(); m.hf.doSearch(m.hf.search); } : m.refreshModels;
  return <div className="mp" style={{'--mp-ok': t.ok, '--mp-warn': t.warm, '--mp-bad': t.err}}>
    <header className="mp-bar">
      <div>
        <div className="mp-title">Models</div>
        <div className="mp-sub">{rows.length} installed{fromHf ? `, ${fromHf} from Hugging Face` : ''}</div>
      </div>
      <div className="mp-tabs" role="tablist" aria-label="Models">
        {TABS.map(([id, label]) => <button key={id} type="button" role="tab" className="mp-tab" aria-selected={tab === id}
          onClick={() => { setTab(id); if (id !== 'ollama') m.setSelected(null); if (id !== 'hf') m.hf.clear(); }}>
          {label}{id === 'ollama' ? ` · ${rows.length}` : ''}
        </button>)}
      </div>
      <div className="mp-spacer"/>
      <button type="button" className="hc-button is-compact" aria-label={tab === 'hyprfit' ? 'Refresh recommendations' : 'Refresh'} title={tab === 'hyprfit' ? 'Refresh recommendations' : 'Refresh'} onClick={refresh}><IC.Refresh/></button>
    </header>
    {tab === 'ollama' && <InstalledTab m={m} rows={rows}/>}
    {tab === 'hyprfit' && <RecommendedTab m={m}/>}
    {tab === 'hf' && <HuggingFaceTab m={m}/>}
  </div>;
}
