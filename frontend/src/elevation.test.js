import test from 'node:test';
import assert from 'node:assert/strict';
import {THEMES, UI_THEMES, contrastRatio, presentationTokens} from './theme.js';
import {elevationTokens, luminance, mix, rgba, isLightTheme} from './elevation.js';

const HEX = /^#[0-9a-f]{6}$/i;

test('elevation tints are hex, readable, and ordered from page to modal in every theme', () => {
  for (const [name, t] of Object.entries(UI_THEMES)) {
    const e = elevationTokens(t);
    for (const key of ['--hc-elev-0', '--hc-elev-1', '--hc-elev-2', '--hc-elev-3', '--hc-sunken']) assert.match(e[key], HEX, `${name} ${key}`);
    for (const key of ['--hc-elev-1', '--hc-elev-2', '--hc-elev-3']) assert.ok(contrastRatio(t.text, e[key]) >= 4.5, `${name}: text on ${key}`);
    const l0 = luminance(e['--hc-elev-0']), l2 = luminance(e['--hc-elev-2']), l3 = luminance(e['--hc-elev-3']);
    assert.ok(l3 >= l2 && l2 >= l0, `${name}: floating/modal tones lift above the page`);
    assert.equal(e['--hc-sunken'], t.bgDeep, `${name}: sunken stays the theme's deep tone`);
    assert.equal(e['--hc-is-light'], isLightTheme(t) ? 1 : 0);
  }
});

test('light themes get text-tinted shadows and scrims; dark themes keep black ones', () => {
  const light = elevationTokens(UI_THEMES.oneLight), dark = elevationTokens(UI_THEMES.hyprflat);
  assert.equal(isLightTheme(UI_THEMES.oneLight), true);
  assert.equal(isLightTheme(UI_THEMES.hyprflat), false);
  assert.ok(!light['--hc-scrim'].startsWith('rgba(0,0,0'));
  assert.ok(dark['--hc-scrim'].startsWith('rgba(0,0,0'));
  assert.ok(light['--hc-shadow-2'].includes(rgba(UI_THEMES.oneLight.text, .18)));
  assert.equal(light['--hc-elev-3'], '#ffffff');
});

test('mix and rgba keep the hex/rgba shapes the app relies on', () => {
  assert.equal(mix('#000000', '#ffffff', .5), '#808080');
  assert.equal(mix('#102030', '#102030', .3), '#102030');
  assert.equal(rgba('#ff0000', .5), 'rgba(255,0,0,0.5)');
});

test('presentationTokens keeps its original keys and adds the elevation set', () => {
  const t = UI_THEMES.nord, tokens = presentationTokens(t, 'f', 'c', 1);
  assert.equal(tokens['--hc-surface'], t.surface);
  assert.equal(tokens['--hc-surface-raised'], t.bgDeep);
  assert.equal(tokens['--hc-hover'], t.sfHov);
  assert.equal(tokens['--hc-border'], t.brd);
  assert.equal(tokens['--hc-text'], t.text);
  assert.equal(tokens['--hc-secondary'], t.dim);
  assert.equal(tokens['--hc-muted'], t.mut);
  assert.equal(tokens['--hc-accent'], t.acc);
  assert.equal(tokens['--hc-danger'], t.err);
  for (const key of Object.keys(elevationTokens(t))) assert.ok(key in tokens, key);
  for (const surface of ['bg', 'bgDeep', 'surface', 'sfHov', 'sfBri', 'brd']) assert.equal(t[surface], THEMES.nord[surface]);
});
