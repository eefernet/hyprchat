import React from 'react';
import { formatTokens, meterView } from '../contextMeter.js';

// The context pill in the chat header. The number is what the conversation holds; the lighter end of the
// bar is what the current reply is using on top of that while it runs (see contextMeter.js).
export default function ContextMeter({ reading, fallbackWindow, compactPercent, live, speed, t, font }) {
  const view = meterView(reading, fallbackWindow, compactPercent);
  const color = view.tone === 'full' ? t.err : view.tone === 'high' ? t.warm : view.tone === 'ok' ? t.ok : live ? t.acc : t.warm;
  return <div title={view.tooltip} aria-label={view.tooltip} role="status" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', borderRadius: 'var(--hc-radius-card)', border: `1px solid ${color}45`, background: `${color}12`, minWidth: 90, cursor: 'default' }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 5, padding: '5px 11px' }}>
      <span style={{ fontSize: 'var(--hc-text-control)', display: 'inline-block', animation: live ? 'bop 1.5s ease-in-out infinite' : 'none' }}>🧠</span>
      {view.warning && <span aria-hidden="true" style={{ fontSize: 'var(--hc-text-control)', color }}>⚠</span>}
      <span style={{ fontSize: 'var(--hc-text-control)', color, fontWeight: 700, fontFamily: font, letterSpacing: .2 }}>{reading && !reading.measured ? '~' : ''}{formatTokens(view.used)}</span>
      {view.window > 0 && <span style={{ fontSize: 'var(--hc-text-micro)', color: t.mut, fontFamily: font }}>/{formatTokens(view.window)}</span>}
      {live && view.working >= 100 && <span style={{ fontSize: 'var(--hc-text-micro)', color: `${color}BB`, fontFamily: font }}>+{formatTokens(view.working)}</span>}
      {live && speed > 0 && <span style={{ fontSize: 'var(--hc-text-micro)', color: `${color}99`, marginLeft: 2 }}>{speed}t/s</span>}
    </div>
    {view.window > 0 && <div style={{ display: 'flex', height: 3, background: `${t.surface}88` }}>
      <div style={{ height: '100%', background: color, width: `${view.usedShare * 100}%`, transition: 'width .3s ease' }}/>
      <div style={{ height: '100%', background: `${color}66`, width: `${view.workingShare * 100}%`, transition: 'width .3s ease' }}/>
    </div>}
  </div>;
}
