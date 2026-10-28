// Theme-derived surface tokens: elevation tints, hairlines, shadows and scrims.
// Pure module with no imports, so theme.js can import it without a cycle and
// node:test can exercise it without a DOM. Hex in, hex (or rgba()) out: the rest
// of the app appends two-digit alpha to theme colours and relies on that shape.
export const channels = hex => hex.slice(1).match(/../g).map(x => parseInt(x, 16));
export const luminance = hex => channels(hex)
  .map(v => {v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;})
  .reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
const hex2 = v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0');
// Linear blend of two hex colours; amount 0 keeps `a`, 1 gives `b`.
export const mix = (a, b, amount) => {
  const x = channels(a), y = channels(b);
  return '#' + x.map((v, i) => hex2(v + (y[i] - v) * amount)).join('');
};
export const rgba = (hex, alpha) => `rgba(${channels(hex).join(',')},${alpha})`;
export const isLightTheme = t => luminance(t.bg) > .4;

// Elevation keeps each theme's own palette slots: level 1 stays the theme's
// card surface, the sunken tone stays bgDeep, and only the floating (2) and
// modal (3) tones are derived, by lifting the lighter of bg/surface a little
// toward the text colour (dark themes) or toward white (light themes).
export function elevationTokens(t) {
  const light = isLightTheme(t);
  const base = light ? t.bg : (luminance(t.surface) > luminance(t.bg) ? t.surface : t.bg);
  const toward = light ? '#ffffff' : t.text;
  return {
    '--hc-bg': t.bg, '--hc-bg-deep': t.bgDeep, '--hc-surface-bright': t.sfBri,
    '--hc-ok': t.ok, '--hc-warm': t.warm, '--hc-pink': t.pink, '--hc-f1': t.f1, '--hc-f4': t.f4, '--hc-acc2': t.acc2,
    '--hc-elev-0': t.bg,
    '--hc-elev-1': light ? mix(t.bg, '#ffffff', .45) : t.surface,
    '--hc-elev-2': light ? mix(t.bg, '#ffffff', .8) : mix(base, toward, .05),
    '--hc-elev-3': light ? '#ffffff' : mix(base, toward, .08),
    '--hc-sunken': t.bgDeep,
    '--hc-line-subtle': `${t.brd}24`, '--hc-line': `${t.brd}40`, '--hc-line-strong': `${t.brd}66`,
    '--hc-shadow-1': light
      ? `0 1px 2px ${rgba(t.text, .06)}, 0 2px 8px -2px ${rgba(t.text, .08)}`
      : '0 1px 2px rgba(0,0,0,.28), 0 4px 14px -4px rgba(0,0,0,.35)',
    '--hc-shadow-2': light
      ? `0 1px 2px ${rgba(t.text, .06)}, 0 12px 32px -10px ${rgba(t.text, .18)}`
      : '0 12px 32px -8px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.04)',
    '--hc-shadow-3': light
      ? `0 24px 60px -16px ${rgba(t.text, .24)}`
      : '0 24px 64px -12px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.05)',
    '--hc-scrim': light ? rgba(t.text, .22) : 'rgba(0,0,0,.55)',
    '--hc-scrim-media': light ? rgba(t.text, .6) : 'rgba(0,0,0,.82)',
    '--hc-sheen': light ? 'rgba(0,0,0,.045)' : 'rgba(255,255,255,.07)',
    '--hc-accent-tint': `${t.acc}14`, '--hc-accent-tint-strong': `${t.acc}22`, '--hc-accent-ring': `${t.acc}40`,
    '--hc-is-light': light ? 1 : 0,
  };
}
