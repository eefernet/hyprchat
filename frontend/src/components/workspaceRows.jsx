import React from 'react';
import './workspace.css';
import {IC} from './icons.jsx';
import {statusInfo} from '../workspaceLabels.js';
import {Skeleton} from './Skeleton.jsx';

// Shared list-row primitives for the workspace screens (home, detail tabs,
// overview cards). Layout lives in workspace.css; colours come from the theme
// object `t` so every chip keeps the active palette.

// Status word → tinted chip. `kind` is a workspaceLabels vocabulary
// ('source-kind' | 'readiness' | 'job-status' | 'job-kind' | 'report-status' | 'job-phase').
export function StatusChip({kind, value, t, icon, label}) {
  const info = statusInfo(kind, value);
  const tone = {ok: t.ok, warm: t.warm, err: t.err, mut: t.mut, acc: t.acc}[info.tone] || t.mut;
  return <span className="hc-chip is-status" style={{'--chip-c': tone}}>{icon}{label || info.label}</span>;
}

// One row: icon · title (button / link / text) · dot-separated meta (chips
// first, no dots between chips) · right-aligned actions · optional full-width
// footer. `index` staggers the reveal animation (capped in CSS).
export function WorkspaceListRow({icon, title, onOpen, href, meta = [], chips = null, actions = null, footer = null, index = 0, active = false}) {
  const metaItems = (meta || []).filter(Boolean);
  const titleNode = onOpen
    ? <button type="button" className="hc-ws-row-title" title={typeof title === 'string' ? title : undefined} onClick={onOpen}>{title}</button>
    : href
      ? <a className="hc-ws-row-title" title={typeof title === 'string' ? title : undefined} href={href}>{title}</a>
      : <span className="hc-ws-row-title" title={typeof title === 'string' ? title : undefined}>{title}</span>;
  return <div className={'hc-ws-row' + (active ? ' is-active' : '')} style={{'--i': index}}>
    <span className="hc-ws-row-icon" aria-hidden="true">{icon}</span>
    <div className="hc-ws-row-main">
      {titleNode}
      {(metaItems.length > 0 || !!chips) && <div className="hc-ws-meta">{chips}{metaItems.map((m, i) => <span key={i}>{m}</span>)}</div>}
    </div>
    {actions && <div className="hc-ws-row-actions">{actions}</div>}
    {footer && <div className="hc-ws-row-footer">{footer}</div>}
  </div>;
}

// "New chats" defaults as chips: model · persona · optional tools. Skeleton
// chips while `defaults` is still loading.
export function DefaultsChips({defaults, tools = [], t}) {
  if (!defaults) return <div className="hc-chip-row" aria-busy="true">{[0, 1, 2].map(i => <Skeleton key={i} t={t} w={96} h={26} r={999}/>)}</div>;
  const names = (defaults.tool_ids || []).map(id => tools.find(x => x.id === id)?.name || id);
  const allNames = names.join(', ');
  const text = names.length ? names.slice(0, 2).join(', ') + (names.length > 2 ? ` +${names.length - 2}` : '') : 'No optional tools';
  return <div className="hc-chip-row" aria-label="New chat defaults">
    <span className="hc-chip-row-label">New chats</span>
    <span className="hc-chip" style={{'--chip-c': t.acc}}><IC.HardDrive/>{defaults.model}</span>
    <span className="hc-chip" style={{'--chip-c': t.pink}}><IC.User/>{defaults.persona_name || 'No persona'}</span>
    <span className="hc-chip" style={{'--chip-c': t.mut}} title={allNames || undefined}><IC.Tool/>{text}</span>
  </div>;
}

export const KIND_ICON = {
  chat: <IC.Chat/>,
  note: <IC.Pencil/>,
  research: <IC.Research/>,
  artifact: <IC.File/>,
  knowledge: <IC.Database/>,
  document: <IC.File/>,
};

export const kindIcon = kind => KIND_ICON[kind] || <IC.File/>;
