import test from 'node:test';
import assert from 'node:assert/strict';
import {BACKGROUND_GROUPS, CSS_EFFECTS, CANVAS_EFFECTS, REAL_COLOR_EFFECTS, backgroundGroups, clampStrength, strengthLabel, scaledOpacity, effectOpacity, auroraColors, festiveColors} from './backgrounds.js';
import {BACKGROUND_EFFECTS} from './theme.js';

test('every effect is in exactly one group, and Off is in none', () => {
  const groups = backgroundGroups(BACKGROUND_EFFECTS);
  const listed = groups.flatMap(group => group.items.map(item => item.id));
  assert.equal(new Set(listed).size, listed.length);
  assert.deepEqual([...listed].sort(), BACKGROUND_EFFECTS.map(e => e.id).filter(id => id !== 'off').sort());
  assert.ok(!groups.some(group => group.id === 'more'), 'a new effect was not given a group');
});

test('the Christmas backgrounds have their own group, named Christmas', () => {
  const group = backgroundGroups(BACKGROUND_EFFECTS).find(g => g.id === 'christmas');
  assert.equal(group.name, 'Christmas');
  assert.deepEqual(group.items.map(item => item.id), ['snowfall', 'xmasLights', 'silentNight', 'holidayGlow']);
});

test('groups only name effects that exist', () => {
  const known = new Set(BACKGROUND_EFFECTS.map(e => e.id));
  for (const group of BACKGROUND_GROUPS) for (const id of group.ids) assert.ok(known.has(id), id);
  for (const id of CSS_EFFECTS) assert.ok(known.has(id), id);
});

test('an effect no group names still shows, under More', () => {
  const groups = backgroundGroups([...BACKGROUND_EFFECTS, {id: 'brandNew', name: 'Brand New'}]);
  assert.deepEqual(groups.at(-1), {id: 'more', name: 'More', items: [{id: 'brandNew', name: 'Brand New'}]});
});

test('strength is clamped, rounded and defaults to normal', () => {
  assert.equal(clampStrength(undefined), 1);
  assert.equal(clampStrength('abc'), 1);
  assert.equal(clampStrength('0.7'), 0.7);
  assert.equal(clampStrength(0), 0.4);
  assert.equal(clampStrength(9), 1.6);
  assert.equal(clampStrength(1.234), 1.2);
});

test('strength names', () => {
  assert.equal(strengthLabel(0.4), 'Subtle');
  assert.equal(strengthLabel(0.8), 'Soft');
  assert.equal(strengthLabel(1), 'Normal');
  assert.equal(strengthLabel(1.2), 'Bold');
  assert.equal(strengthLabel(1.6), 'Strong');
});

test('opacity scales with strength and never passes 1', () => {
  assert.equal(scaledOpacity(0.7, 1), 0.7);
  assert.ok(Math.abs(scaledOpacity(0.7, 0.5) - 0.35) < 1e-9);
  assert.equal(scaledOpacity(0.8, 1.6), 1);
});

test('every effect is drawn either by the canvas or by a plain element, never both', () => {
  const all = BACKGROUND_EFFECTS.map(e => e.id).filter(id => id !== 'off').sort();
  assert.deepEqual([...CANVAS_EFFECTS, ...CSS_EFFECTS].sort(), all);
  assert.equal(new Set([...CANVAS_EFFECTS, ...CSS_EFFECTS]).size, all.length);
  for (const id of REAL_COLOR_EFFECTS) assert.ok(CANVAS_EFFECTS.includes(id), id);
});

test('canvas opacity: dimmed on light themes, less so for the real-colour effects, scaled by strength', () => {
  assert.equal(effectOpacity('flow'), 0.8);
  assert.ok(Math.abs(effectOpacity('flow', true) - 0.44) < 1e-9);
  assert.ok(Math.abs(effectOpacity('northern', true) - 0.81) < 1e-9);
  assert.equal(effectOpacity('neural'), 0.7);
  assert.equal(effectOpacity('xmasLights', false, 1.6), 1);
  assert.ok(Math.abs(effectOpacity('flow', false, 0.5) - 0.4) < 1e-9);
});

test('real colours have a darker set for light themes', () => {
  assert.notEqual(auroraColors(true).green, auroraColors(false).green);
  assert.notEqual(festiveColors(true).snow, festiveColors(false).snow);
  for (const set of [auroraColors(true), auroraColors(false), festiveColors(true), festiveColors(false)])
    for (const colour of Object.values(set)) assert.match(colour, /^#[0-9a-f]{6}$/);
});
