import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {PHASE_ICONS, TOOL_ICONS, activityVisual, activityColor, activityIcon, summaryChips} from './chatActivityVisual.js';

// icons.jsx is JSX, which node:test cannot import; read the exported names as text.
const iconSource = fs.readFileSync(new URL('./components/icons.jsx', import.meta.url), 'utf8');
const hasIcon = name => new RegExp(`^  ${name}:`, 'm').test(iconSource);

const t = {err: '#e1', f4: '#f4', warm: '#wa', mut: '#mu', acc: '#ac', dim: '#di'};
const state = (phase, extra = {}) => ({phase, active: false, entries: [], tools: 0, ...extra});

test('every phase and tool icon name exists in the icon set', () => {
  for (const name of [...Object.values(PHASE_ICONS), ...Object.values(TOOL_ICONS), 'Activity', 'ChevDown']) assert.ok(hasIcon(name), name);
});

test('colour follows the phase rule used by the pill today', () => {
  assert.equal(activityColor(state('failed', {active: true}), t), t.err);
  assert.equal(activityColor(state('thinking', {active: true}), t), t.f4);
  assert.equal(activityColor(state('reviewing', {active: true}), t), t.warm);
  assert.equal(activityColor(state('stopped'), t), t.mut);
  assert.equal(activityColor(state('writing', {active: true}), t), t.acc);
  assert.equal(activityColor(state('complete'), t), t.dim);
});

test('tool icons override the generic tool icon only while a tool runs', () => {
  assert.equal(activityIcon(state('tool'), 'execute_code'), 'Terminal');
  assert.equal(activityIcon(state('tool'), 'unknown_tool'), 'Tool');
  assert.equal(activityIcon(state('writing'), 'execute_code'), 'Pencil');
  assert.equal(activityIcon(state('nonsense')), 'Activity');
});

test('visual vars derive from one colour and keep the surface on the elevation token', () => {
  const v = activityVisual(state('thinking', {active: true}), t, null);
  assert.equal(v.icon, 'Brain');
  assert.equal(v.vars['--activity-color'], t.f4);
  assert.equal(v.vars['--activity-ring'], `${t.f4}40`);
  assert.equal(v.vars['--activity-surface'], 'var(--hc-elev-1)');
});

test('summary chips match the former joined summary text', () => {
  const s = state('complete', {tools: 2, entries: [{kind: 'reasoning'}, {kind: 'tool'}, {kind: 'tool'}, {kind: 'review'}]});
  assert.deepEqual(summaryChips(s), ['Reasoning', '2 tools', 'Review']);
  assert.deepEqual(summaryChips(state('complete', {tools: 1, entries: [{kind: 'tool'}]})), ['1 tool']);
  assert.deepEqual(summaryChips(state('complete')), []);
});
