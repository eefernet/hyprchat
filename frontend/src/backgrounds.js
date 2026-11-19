// How the background picker groups the effects, and the rules of the strength setting.
// Pure, so node:test can import it.

export const BACKGROUND_GROUPS = [
  {id: 'calm', name: 'Calm', ids: ['dots', 'flow', 'aurora', 'candle', 'lattice', 'sacred']},
  {id: 'technical', name: 'Technical', ids: ['rain', 'circuit', 'neural', 'signal', 'veins', 'terminalGhost', 'synapse', 'blueprint', 'scanlines']},
  {id: 'space', name: 'Space', ids: ['northern', 'stars', 'quantum', 'solar', 'orbital', 'sonar', 'magnetic', 'microwave']},
  {id: 'christmas', name: 'Christmas', ids: ['snowfall', 'xmasLights', 'silentNight', 'holidayGlow']},
];

// Every effect BackgroundCanvas draws. Scanlines is the one plain element left in the app shell.
export const CANVAS_EFFECTS = ['dots', 'rain', 'flow', 'aurora', 'stars', 'circuit', 'neural', 'sacred', 'signal', 'quantum', 'veins', 'solar', 'terminalGhost', 'orbital', 'lattice', 'synapse', 'sonar', 'magnetic', 'candle', 'microwave', 'blueprint', 'northern', 'snowfall', 'xmasLights', 'silentNight', 'holidayGlow'];
export const CSS_EFFECTS = ['scanlines'];

// These keep their own colours on every theme (an aurora is green whatever the accent colour is).
export const REAL_COLOR_EFFECTS = ['northern', 'snowfall', 'xmasLights', 'silentNight', 'holidayGlow'];

const AURORA = {
  dark: {green: '#4dff9a', teal: '#2ee6c8', violet: '#8a6bff', pink: '#ff6ad5', star: '#dfe9ff'},
  light: {green: '#0fa85f', teal: '#0b998a', violet: '#6a48e0', pink: '#cf389f', star: '#7f95ad'},
};
const FESTIVE = {
  dark: {red: '#ff4d4d', green: '#35d07f', gold: '#ffc84a', blue: '#4da3ff', snow: '#ffffff', star: '#fff4c8', pine: '#8fc7b4', wire: '#ffffff'},
  light: {red: '#d92b2b', green: '#15955a', gold: '#d6960c', blue: '#2b78d9', snow: '#7f95ad', star: '#c98a06', pine: '#2c5a4c', wire: '#1c2530'},
};
// A light page needs darker, fuller colours: pale light on white cannot be seen.
export const auroraColors = light => (light ? AURORA.light : AURORA.dark);
export const festiveColors = light => (light ? FESTIVE.light : FESTIVE.dark);

// How opaque each effect's canvas is at Normal strength on a dark theme.
const BASE_OPACITY = {dots: .8, rain: .85, flow: .8, aurora: .62, stars: .82, circuit: .78, candle: .6, microwave: .62, northern: .9, snowfall: .9, xmasLights: 1, silentNight: .95, holidayGlow: .85};
export function effectOpacity(effect, light = false, strength = 1) {
  const base = BASE_OPACITY[effect] ?? .7;
  // Theme-coloured effects are dimmed on a light page; the real-colour ones already use darker colours.
  const onLight = light ? (REAL_COLOR_EFFECTS.includes(effect) ? .9 : .55) : 1;
  return Math.min(1, Math.max(0, base * onLight * clampStrength(strength)));
}

// `effects` is theme.js BACKGROUND_EFFECTS. An effect no group names is not lost: it goes to "More".
export function backgroundGroups(effects) {
  const byId = new Map((effects || []).map(effect => [effect.id, effect]));
  const used = new Set(['off']);
  const groups = BACKGROUND_GROUPS.map(group => {
    const items = group.ids.filter(id => byId.has(id)).map(id => byId.get(id));
    items.forEach(item => used.add(item.id));
    return {id: group.id, name: group.name, items};
  }).filter(group => group.items.length);
  const rest = (effects || []).filter(effect => !used.has(effect.id));
  if (rest.length) groups.push({id: 'more', name: 'More', items: rest});
  return groups;
}

export const STRENGTH_MIN = 0.4, STRENGTH_MAX = 1.6, STRENGTH_STEP = 0.1, STRENGTH_DEFAULT = 1;

export function clampStrength(value) {
  const n = typeof value === 'number' ? value : parseFloat(value);
  if (!Number.isFinite(n)) return STRENGTH_DEFAULT;
  return Math.round(Math.min(STRENGTH_MAX, Math.max(STRENGTH_MIN, n)) * 10) / 10;
}

export function strengthLabel(value) {
  const s = clampStrength(value);
  if (s <= 0.6) return 'Subtle';
  if (s < 1) return 'Soft';
  if (s === 1) return 'Normal';
  if (s <= 1.3) return 'Bold';
  return 'Strong';
}

// Opacity of an effect at a strength: the effect's own base times the strength, never above 1.
export const scaledOpacity = (base, strength) => Math.min(1, Math.max(0, base * clampStrength(strength)));
