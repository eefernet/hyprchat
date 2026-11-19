import React from 'react';
import {IC} from '../icons.jsx';
import {Spinner} from '../Spinner.jsx';
import {formatSize, quantMeaning, repoFiles, repoError} from '../../modelsPage.js';
import {fmtBytes} from '../../hfCompat.js';

const SUGGESTIONS = ['Llama 3', 'Qwen', 'Mistral', 'Gemma', 'Phi'];
const count = n => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n > 999 ? `${(n / 1000).toFixed(1)}k` : String(n ?? 0));
const quantOf = filename => { const found = String(filename || '').match(/[_\-.](I?Q[0-9]_[A-Z0-9_]+|[FQB][0-9]+(?:_[A-Z0-9]+)?|BF16|fp16|f32|f16)/i); return found ? found[1].toUpperCase() : ''; };
// Files split into numbered parts are one choice.
function groups(files) {
  const out = new Map();
  for (const file of files) {
    const part = file.name.match(/^(.+)-(\d{5})-of-(\d{5})\.gguf$/i);
    const key = part ? `${part[1]} (${Number(part[3])} parts)` : file.name;
    out.set(key, [...(out.get(key) || []), file]);
  }
  return [...out.entries()];
}

// Browse Hugging Face: search, pick a repository, tick the file to install. Only GGUF files can run in
// Ollama. A failed search or lookup says so (it used to look like "no results", or crash the page).
export default function HuggingFaceTab({m}) {
  const {t, btnS, inputS, hf} = m;
  const files = repoFiles(hf.info), filesError = repoError(hf.info);
  const report = hf.report;
  const tone = report.level === 'bad' ? 'bad' : report.level === 'warn' ? 'warn' : 'ok';
  const chosenBytes = (files || []).filter(f => hf.selectedFiles.includes(f.name)).reduce((sum, f) => sum + (f.size || 0), 0);
  const busyHere = hf.downloadRepo === hf.selected?.id, busyElsewhere = !!hf.downloadRepo && !busyHere;

  return <div className="mp-body">
    <div className="mp-side">
      <div className="mp-side-head">
        <div className="mp-inline">
          <input value={hf.search} onChange={e => hf.setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent?.isComposing) hf.doSearch(hf.search); }} placeholder="Search Hugging Face" aria-label="Search Hugging Face" style={inputS}/>
          <button type="button" aria-label="Search" onClick={() => hf.doSearch(hf.search)} disabled={hf.loading || !hf.search.trim()} style={{...btnS(t.acc), flexShrink: 0, opacity: !hf.loading && hf.search.trim() ? 1 : .55}}><IC.Search/></button>
        </div>
        <label className="mp-inline mp-muted" style={{marginTop: 8, cursor: 'pointer'}}>
          <input type="checkbox" checked={hf.ggufOnly} style={{accentColor: t.acc, flex: 'none'}} onChange={e => { hf.setGgufOnly(e.target.checked); if (hf.search.trim()) hf.doSearch(hf.search, e.target.checked); }}/>
          Only models Ollama can run (GGUF files)
        </label>
      </div>
      <div className="mp-side-list">
        {hf.loading && <div className="mp-empty"><Spinner size={12}/> Searching…</div>}
        {!hf.loading && hf.error && <div className="mp-note is-bad" role="alert">The search failed: {hf.error}</div>}
        {!hf.loading && !hf.error && !hf.results.length && <div className="mp-empty">
          {hf.searched ? <>Nothing found for “{hf.searched}”.<br/></> : null}
          Try: {SUGGESTIONS.map((s, i) => <React.Fragment key={s}><button type="button" className="mp-link" onClick={() => { hf.setSearch(s); hf.doSearch(s); }}>{s}</button>{i < SUGGESTIONS.length - 1 ? ' · ' : ''}</React.Fragment>)}
        </div>}
        {hf.results.map(result => {
          const [author, name] = result.id.split('/');
          const embedding = /feature-extraction|sentence-similarity/.test(result.pipeline_tag || '');
          return <button key={result.id} type="button" className="mp-row" aria-current={hf.selected?.id === result.id ? 'true' : undefined} onClick={() => hf.select(result)}>
            <div className="mp-row-sub" style={{margin: 0}}>{author}/</div>
            <div className="mp-row-name"><span title={result.id}>{name || result.id}</span></div>
            <div className="mp-row-sub">{count(result.downloads)} downloads · {count(result.likes)} likes{embedding ? ' · embeddings only' : ''}</div>
          </button>;
        })}
      </div>
    </div>
    {!hf.selected ? <div className="mp-main"><div className="mp-empty" style={{maxWidth: 420, margin: '40px auto'}}>Search for a model, then pick one to see its files.<br/>Models you install appear under <b>Installed</b>.</div></div>
    : <div className="mp-hf-detail">
      <div className="mp-hf-strip">
        <div className="mp-headrow">
          <div style={{minWidth: 0}}>
            <div className="mp-muted">{hf.selected.id.split('/')[0]}/</div>
            <div className="mp-h" style={{wordBreak: 'break-word'}}>{hf.selected.id.split('/')[1] || hf.selected.id}</div>
          </div>
          <div className="mp-muted">{count(hf.selected.downloads)} downloads · {count(hf.selected.likes)} likes</div>
        </div>
      </div>
      {files && <div className="mp-hf-strip">
        <div className={`mp-chip is-${tone}`}>{report.level === 'ok' ? (hf.selectedFiles.length ? 'Should run on this Ollama' : 'Tick a file to check it against this Ollama') : report.level === 'bad' ? 'Likely will not run' : 'May not run'}</div>
        {report.issues.length > 0 ? <ul className="mp-issues">{report.issues.map(issue => <li key={issue.label}><b>{issue.label}.</b> {issue.detail}</li>)}</ul>
          : <div className="mp-muted" style={{marginTop: 4}}>{hf.compatLoading ? 'Reading the file header and checking it against this Ollama…' : 'Nothing in the files or the description points to a problem.'}</div>}
      </div>}
      {hf.selectedFiles.length > 0 && <div className="mp-hf-strip">
        <div className="mp-inline" style={{flexWrap: 'wrap'}}>
          <label className="mp-field" style={{flex: '1 1 200px'}}><span>Name to install it as</span>
            <input value={hf.downloadName} onChange={e => hf.setDownloadName(e.target.value)} placeholder="e.g. llama3-q5" style={inputS}/></label>
          <button type="button" onClick={() => hf.download()} disabled={!!hf.downloadRepo || !hf.downloadName.trim()} style={{...btnS(hf.downloadRepo ? t.mut : report.flagged ? t.warm : t.ok), alignSelf: 'flex-end', opacity: hf.downloadRepo ? .7 : 1}}>
            {busyHere ? <Spinner size={12}/> : <IC.Download/>} {busyHere ? 'Installing…' : busyElsewhere ? 'Another install is running' : report.flagged ? 'Install anyway' : 'Install'}
          </button>
        </div>
        <div className="mp-muted" style={{marginTop: 6}}>
          {hf.selectedFiles.length} file{hf.selectedFiles.length === 1 ? '' : 's'}, {formatSize(chosenBytes) || 'size unknown'}.{' '}
          {hf.compatLoading ? 'Checking what will be downloaded…' : report.pull?.resolved ? `Downloads ${fmtBytes(report.pull.modelBytes)}${report.pull.projectorBytes ? ` plus a ${fmtBytes(report.pull.projectorBytes)} image add-on (projector) that the repository attaches` : ''}.` : ''}
        </div>
      </div>}
      <div className="mp-hf-cols">
        <div className="mp-hf-files">
          <div className="mp-h2">Files</div>
          {!hf.info ? <div className="mp-muted">Loading…</div>
            : filesError ? <div className="mp-note is-bad" role="alert">The file list could not be loaded: {filesError}</div>
            : !files.length ? <div className="mp-muted">This repository has no GGUF files, so Ollama cannot run it.</div>
            : groups(files).map(([key, parts]) => {
              const names = parts.map(f => f.name), all = names.every(n => hf.selectedFiles.includes(n));
              const toggle = () => hf.setSelectedFiles(prev => all ? prev.filter(n => !names.includes(n)) : [...new Set([...prev, ...names])]);
              const quant = quantOf(key);
              return <button key={key} type="button" className="mp-file" aria-pressed={all} onClick={toggle}>
                <input type="checkbox" checked={all} readOnly tabIndex={-1} style={{accentColor: t.acc, marginTop: 2, flex: 'none'}}/>
                <span style={{minWidth: 0}}>
                  <span className="mp-file-name" style={{display: 'block'}}>{key}</span>
                  <span className="mp-muted" style={{display: 'block'}}>{formatSize(parts.reduce((sum, f) => sum + (f.size || 0), 0))}{quant ? ` · ${quant}` : ''}{quant && quantMeaning(quant) ? ` (${quantMeaning(quant).split(':')[0]})` : ''}</span>
                </span>
              </button>;
            })}
        </div>
        <div className="mp-hf-readme">
          <div className="mp-h2">From the model’s page</div>
          {hf.readmeLoading ? <div className="mp-muted">Loading…</div> : hf.readme ? m.renderMarkdown(hf.readme) : <div className="mp-muted">The model’s page has no description.</div>}
        </div>
      </div>
    </div>}
  </div>;
}
