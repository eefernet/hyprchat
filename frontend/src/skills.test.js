import test from 'node:test';
import assert from 'node:assert/strict';
import { skillsForTab, reviewCount, needsReview, usageLabel, heldLabel, lineDiff, diffStats, slugify } from './skills.js';

const skill = (name, extra = {}) => ({ name, description: `Use when ${name} is needed`, category: 'general', status: 'active',
  pinned: false, use_count: 0, scan_flags: [], pending: null, origin: 'reviewer', ...extra });

const all = [
  skill('resize-images'), skill('deploy-site', { pinned: true }), skill('old-trick', { status: 'stale' }),
  skill('draft-one', { status: 'draft' }), skill('parse-invoices', { pending: { body: 'new', held: 'this conversation read email content' } }),
  skill('retired', { status: 'archived' }),
];

test('tabs split the library, the review queue and the archive', () => {
  assert.deepEqual(skillsForTab(all, 'library').map(s => s.name), ['deploy-site', 'old-trick', 'parse-invoices', 'resize-images']);
  assert.deepEqual(skillsForTab(all, 'review').map(s => s.name), ['draft-one', 'parse-invoices']);
  assert.deepEqual(skillsForTab(all, 'archived').map(s => s.name), ['retired']);
});

test('a live skill with a parked change is both usable and waiting', () => {
  const parked = all[4];
  assert.equal(needsReview(parked), true);
  assert.equal(reviewCount(all), 2);
  assert.ok(skillsForTab(all, 'library').includes(parked));
});

test('search matches every word across name, description and category', () => {
  assert.deepEqual(skillsForTab(all, 'library', 'resize images').map(s => s.name), ['resize-images']);
  assert.deepEqual(skillsForTab(all, 'library', 'general site').map(s => s.name), ['deploy-site']);
  assert.deepEqual(skillsForTab(all, 'library', 'nothing-here'), []);
});

test('usage and hold reasons read as plain sentences', () => {
  assert.equal(usageLabel(skill('a')), 'Not used yet');
  assert.equal(usageLabel(skill('a', { use_count: 1 })), 'Used 1 time');
  assert.equal(usageLabel(skill('a', { use_count: 4 })), 'Used 4 times');
  assert.equal(heldLabel(all[4]), 'This conversation read email content');
  assert.equal(heldLabel(skill('a', { status: 'draft', scan_flags: ['exfiltration', 'email'] })), 'Safety check: sends data to a URL, sends email');
  assert.equal(heldLabel(skill('a', { status: 'draft' })), 'Waiting for your approval');
});

test('line diff marks what a proposed change adds and removes', () => {
  const rows = lineDiff('## Steps\n1. Run it.\n2. Check.', '## Steps\n1. Run it with --keep.\n2. Check.\n\n## Pitfalls\n- Mind the ratio.');
  assert.deepEqual(rows.map(r => r.type), ['same', 'del', 'add', 'same', 'add', 'add', 'add']);
  assert.deepEqual(diffStats(rows), { added: 4, removed: 1 });
  assert.deepEqual(lineDiff('same', 'same'), [{ type: 'same', text: 'same' }]);
  assert.deepEqual(diffStats(lineDiff('', 'new line')), { added: 1, removed: 1 });
});

test('names are turned into the slug the server will store', () => {
  assert.equal(slugify('Batch Resize_Images!'), 'batch-resize-images');
  assert.equal(slugify('  --x--  '), 'x');
  assert.equal(slugify('a'.repeat(80)).length, 48);
});
