import React, { useState } from 'react';
import { API } from '../session.js';

// The cards a tool leaves in a reply: code output and delivered files. messageParts.js decides where
// each one goes; this file only draws them. Used in place inside a reply (MessageBody) and by ToolStatus
// for the timelines that still list their cards together.

const FILE_ICONS = {
  py: '🐍', python: '🐍', js: '📜', mjs: '📜', jsx: '⚛️', ts: '🔷', tsx: '🔷', java: '☕', jar: '☕', rs: '🦀', go: '🐹',
  c: '⚙️', cpp: '⚙️', h: '⚙️', cs: '🟣', rb: '💎', php: '🐘', swift: '🐦', kt: '🟠', lua: '🌙', r: '📊', sh: '🖥️',
  bash: '🖥️', zsh: '🖥️', html: '🌐', css: '🎨', scss: '🎨', json: '📋', yaml: '📋', yml: '📋', xml: '📋', toml: '📋',
  md: '📝', txt: '📄', csv: '📊', sql: '🗃️', dockerfile: '🐳', png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', svg: '🖼️',
  pdf: '📕', zip: '📦', tar: '📦', gz: '📦',
};
const fileIcon = name => FILE_ICONS[String(name || '').split('.').pop().toLowerCase()] || '📁';

// One line until opened. A failed run starts open, so the error is not hidden behind a click.
export function CodeOutputPill({ data, t, defaultOpen }) {
  const d = data || {};
  const [open, setOpen] = useState(defaultOpen ?? !d.success);
  const tone = d.success ? t.ok : t.err;
  const seconds = parseFloat(d.exec_time);
  const hasOutput = !!(d.stdout || d.stderr);
  return <div className="hc-tool-card" style={{ margin: '8px 0', borderRadius: 'var(--hc-radius-card)', overflow: 'hidden', border: `1px solid ${tone}33`, background: t.bgDeep }}>
    <button type="button" onClick={() => setOpen(value => !value)} aria-expanded={open} title={open ? 'Hide output' : 'Show output'}
      style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 12px', background: `${tone}10`, border: 'none', borderBottom: open && hasOutput ? `1px solid ${tone}22` : 'none', borderRadius: 0, cursor: 'pointer', userSelect: 'none', fontFamily: 'inherit', textAlign: 'left' }}>
      <span style={{ display: 'inline-block', transition: 'transform .15s', transform: open ? 'rotate(0)' : 'rotate(-90deg)', color: tone, fontSize: 'var(--hc-text-secondary)' }}>{'▾'}</span>
      <span>{d.success ? '✅' : '❌'}</span>
      <span style={{ fontWeight: 600, color: tone, fontSize: 'var(--hc-text-secondary)' }}>{d.language || 'code'} output</span>
      {Number.isFinite(seconds) && <span style={{ color: t.mut, marginLeft: 'auto', fontSize: 'var(--hc-text-secondary)' }}>{seconds.toFixed(1)}s</span>}
    </button>
    {open && d.stdout && <pre style={{ margin: 0, padding: '8px 12px', whiteSpace: 'pre-wrap', color: t.dim, fontSize: 'var(--hc-text-secondary)', lineHeight: 1.5, maxHeight: 320, overflowY: 'auto', borderBottom: d.stderr ? '1px solid var(--hc-line-subtle)' : 'none', fontFamily: 'var(--hc-font-code)' }}>{d.stdout}</pre>}
    {open && d.stderr && <pre style={{ margin: 0, padding: '8px 12px', whiteSpace: 'pre-wrap', color: t.err, fontSize: 'var(--hc-text-secondary)', lineHeight: 1.5, maxHeight: 320, overflowY: 'auto', background: `${t.err}08`, fontFamily: 'var(--hc-font-code)' }}>{d.stderr}</pre>}
  </div>;
}

export function FileReadyCard({ data, versions = 1, t, onPreview, onOpenArtifact, messageText }) {
  const d = data || {};
  const isImage = d.is_image || /\.(png|jpe?g|gif|svg|webp)$/i.test(d.filename || '');
  // Generated images are streamed into the reply as markdown, so the picture is already there:
  // keep the buttons, skip a second copy. Older messages have no inline markdown and show it here.
  const inlineAlready = isImage && d.url && typeof messageText === 'string' && messageText.includes(d.url);
  return <div className="hc-tool-card" style={{ marginTop: 5 }}>
    {isImage && !inlineAlready && <img src={`${API}${d.url}`} alt={d.filename} style={{ maxWidth: '100%', maxHeight: 380, borderRadius: 'var(--hc-radius)', display: 'block', margin: '6px 0', border: '1px solid var(--hc-line-subtle)', cursor: 'pointer' }} onClick={() => onPreview && onPreview(d.filename, d.url)} onError={event => { event.target.style.display = 'none'; }}/>}
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginTop: isImage ? 2 : 0 }}>
      <a href={d.artifact_id ? `${API}/api/artifacts/${d.artifact_id}/download` : `${API}${d.url}`} download={d.filename}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '7px 14px', background: `${t.ok}15`, border: `1px solid ${t.ok}40`, borderRadius: '10px 0 0 10px', color: t.ok, textDecoration: 'none', fontSize: 'var(--hc-text-secondary)', fontWeight: 700, cursor: 'pointer' }}>
        <span style={{ fontSize: 'var(--hc-text-reading)' }}>{fileIcon(d.filename)}</span><span>{d.filename}</span>
        {versions > 1 && <span style={{ fontSize: 'var(--hc-text-secondary)', background: t.acc, color: t.bg, borderRadius: 'var(--hc-radius)', padding: '1px 5px', fontWeight: 700, marginLeft: 2 }}>v{versions}</span>}
        <span style={{ fontSize: 'var(--hc-text-secondary)', opacity: .6 }}>⬇</span>
      </a>
      {onPreview && <button onClick={() => onPreview(d.filename, d.url)} title="Preview" style={{ padding: '7px 10px', background: `${t.acc}15`, border: `1px solid ${t.acc}40`, borderRadius: '0 10px 10px 0', borderLeft: 'none', color: t.acc, cursor: 'pointer', fontSize: 'var(--hc-text-control)', display: 'flex', alignItems: 'center' }}>👁</button>}
      {d.artifact_id && onOpenArtifact && <button onClick={() => onOpenArtifact(d.artifact_id)} title="Open in Artifacts" style={{ padding: '7px 10px', background: `${t.pink}12`, border: `1px solid ${t.pink}35`, borderRadius: 'var(--hc-radius)', color: t.pink, cursor: 'pointer', fontSize: 'var(--hc-text-control)', fontFamily: 'inherit', fontWeight: 600, display: 'flex', alignItems: 'center' }}>Artifact</button>}
    </div>
  </div>;
}

// `cards` come from messageParts(): { type, data, versions }. `slot` keeps each pill's open state apart
// from the pills of other parts of the same reply.
export function MessageCards({ cards, slot = '', t, onPreview, onOpenArtifact, messageText, defaultOpen }) {
  if (!cards?.length) return null;
  return <div className="hc-tool-cards" style={{ margin: '6px 0 10px' }}>
    {cards.map((card, index) => card.type === 'file_ready'
      ? <FileReadyCard key={`${slot}f${card.data.filename}`} data={card.data} versions={card.versions} t={t} onPreview={onPreview} onOpenArtifact={onOpenArtifact} messageText={messageText}/>
      : <CodeOutputPill key={`${slot}o${index}`} data={card.data} t={t} defaultOpen={defaultOpen}/>)}
  </div>;
}
