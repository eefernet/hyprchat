import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {NAV_REGISTRY, NAV_ITEM_IDS, NAV_LAYOUT_VERSION, DEFAULT_NAV_LAYOUT, resolveNavLayout, defaultZoneOf} from './navLayout.js';

// icons.jsx is JSX, which node:test cannot import; read the exported names as text.
const iconSource = fs.readFileSync(new URL('./components/icons.jsx', import.meta.url), 'utf8');
const hasIcon = name => new RegExp(`^  ${name}:`, 'm').test(iconSource);

const zonesOf = layout => ['bar', 'more', 'hidden'].flatMap(z => layout[z].map(id => [id, z]));
const allIds = layout => zonesOf(layout).map(([id]) => id);

const v1More = {bar: ['chat', 'research'], more: ['workspaces', 'library'], hidden: []};
const v1Hidden = {bar: ['chat', 'research'], more: ['library'], hidden: ['workspaces']};
const v1OnBar = {bar: ['chat', 'research', 'council', 'workspaces', 'images'], more: ['library'], hidden: []};
const v2More = {v: 2, bar: ['chat'], more: ['workspaces'], hidden: []};
const v1EmptyBar = {v: 1, bar: [], more: [...NAV_ITEM_IDS], hidden: []};
const fixtures = {null: null, v1More, v1Hidden, v1OnBar, v2More, v1EmptyBar, fresh: DEFAULT_NAV_LAYOUT};

test('registry ids are unique and every icon name exists in the icon set', () => {
  assert.equal(new Set(NAV_ITEM_IDS).size, NAV_REGISTRY.length);
  for (const it of NAV_REGISTRY) {
    assert.equal(typeof it.icon, 'string', it.id);
    assert.ok(hasIcon(it.icon), `${it.id} → ${it.icon}`);
  }
  assert.equal(NAV_LAYOUT_VERSION, 2);
  assert.equal(defaultZoneOf('workspaces'), 'bar');
  assert.equal(defaultZoneOf('library'), 'more');
});

test('a fresh layout is v2 with Workspaces right after Chat and every id in exactly one zone', () => {
  const l = resolveNavLayout(null);
  assert.equal(l.v, 2);
  assert.equal(l.bar[0], 'chat');
  assert.equal(l.bar[1], 'workspaces');
  assert.deepEqual([...allIds(l)].sort(), [...NAV_ITEM_IDS].sort());
  assert.equal(allIds(l).length, NAV_ITEM_IDS.length);
  assert.deepEqual(Object.keys(l), ['v', 'bar', 'more', 'hidden']);
});

test('a v1 layout with Workspaces behind More promotes it onto the bar after Chat', () => {
  const l = resolveNavLayout(v1More);
  assert.equal(l.v, 2);
  assert.equal(l.bar[1], 'workspaces');
  assert.ok(!l.more.includes('workspaces'));
  assert.equal(allIds(l).filter(id => id === 'workspaces').length, 1);
});

test('a v1 layout that hid Workspaces keeps it hidden', () => {
  const l = resolveNavLayout(v1Hidden);
  assert.equal(l.v, 2);
  assert.ok(l.hidden.includes('workspaces'));
  assert.ok(!l.bar.includes('workspaces'));
});

test('a v1 layout with Workspaces already on the bar leaves its position alone', () => {
  const l = resolveNavLayout(v1OnBar);
  assert.equal(l.bar.indexOf('workspaces'), 3);
});

test('a v2 layout with Workspaces behind More is not promoted again', () => {
  const l = resolveNavLayout(v2More);
  assert.ok(l.more.includes('workspaces'));
  assert.ok(!l.bar.includes('workspaces'));
});

test('resolution is idempotent for every fixture', () => {
  for (const [name, fx] of Object.entries(fixtures)) {
    const once = resolveNavLayout(fx);
    assert.deepEqual(resolveNavLayout(once), once, name);
  }
});

test('an empty bar still ends up with at least one item', () => {
  const l = resolveNavLayout(v1EmptyBar);
  assert.ok(l.bar.length >= 1);
  assert.deepEqual([...allIds(l)].sort(), [...NAV_ITEM_IDS].sort());
});

test('unknown ids are dropped, duplicates collapse and missing ids are appended to their default zone', () => {
  const l = resolveNavLayout({v: 2, bar: ['chat', 'bogus', 'chat', 42], more: ['chat', 'nope'], hidden: ['research']});
  assert.ok(!allIds(l).includes('bogus'));
  assert.ok(!allIds(l).includes('nope'));
  assert.equal(allIds(l).filter(id => id === 'chat').length, 1);
  assert.deepEqual(l.hidden, ['research']);
  assert.deepEqual([...allIds(l)].sort(), [...NAV_ITEM_IDS].sort());
  for (const id of NAV_ITEM_IDS) if (!['chat', 'research'].includes(id)) assert.ok(l[defaultZoneOf(id)].includes(id), id);
});

test('invalid shapes fall back to the fresh default', () => {
  for (const bad of [undefined, 'x', [], {bar: 'chat'}, {bar: [], more: []}]) assert.deepEqual(resolveNavLayout(bad), resolveNavLayout(null));
});
