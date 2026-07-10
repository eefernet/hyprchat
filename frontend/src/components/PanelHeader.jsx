import React from 'react';

// Shared header shell for the Jarvis panels: icon chip + uppercase title +
// subtitle, optional nav elements (tabs/filters) before the spacer, and
// right-aligned action buttons as children.
export default function PanelHeader({ t, color, icon, title, subtitle, nav, children }) {
  const c = color || t.acc;
  return <div style={{ padding: '14px 20px', borderBottom: `1px solid ${t.brd}28`, display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, flexWrap: 'wrap' }}>
    <span style={{ display: 'flex', color: c }}>{icon}</span>
    <div style={{ marginRight: 12 }}>
      <div style={{ fontSize: 14, fontWeight: 800, letterSpacing: 1, textTransform: 'uppercase', color: c }}>{title}</div>
      {subtitle && <div style={{ fontSize: 10, color: t.mut, marginTop: 2 }}>{subtitle}</div>}
    </div>
    {nav}
    <div style={{ flex: 1 }} />
    {children}
  </div>;
}
