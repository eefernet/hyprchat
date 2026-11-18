// Settings → Storage & Cleanup (2026-11-16).
import test from 'node:test';
import assert from 'node:assert/strict';
import {formatSize, monthLabel, localFilesView} from './storageSummary.js';

const OUTPUTS = {files: 130, bytes: 70104950, chat_folders: 47, loose: {files: 0, bytes: 0},
  kinds: {chats: {files: 61, bytes: 9000000}, image_studio: {files: 36, bytes: 60000000}, library: {files: 0, bytes: 0}, unsorted: {files: 33, bytes: 1104950}},
  months: [{month: '2026-11', files: 57, bytes: 62000000, chats: 9}, {month: '2026-09', files: 58, bytes: 6000000, chats: 28}]};

test('the page shows the files that are in chat folders, not the empty top level', () => {
  // What the server sent after the files were sorted: the cleanup's own counts are all zero.
  const view = localFilesView({preserved: 0, retained_bytes: 0, reclaimable_files: 0, reclaimable_bytes: 0, outputs: OUTPUTS}, {sandbox_file_count: 130});
  assert.equal(view.headline, '130 files · 66.9 MB');
  assert.equal(view.folders, 47);
  assert.deepEqual(view.where.map(row => `${row.label} ${row.files}`), ['Chats 61', 'Image Studio 36', 'Unsorted 33']);
  assert.deepEqual(view.months.map(row => `${row.label}: ${row.files}`), ['November 2026: 57', 'September 2026: 58']);
  assert.equal(view.cleanLine, 'Nothing to clean right now.');
  assert.equal(view.canClean, false);
});

test('temporary files that can be removed are said in one line', () => {
  const view = localFilesView({reclaimable_files: 1, reclaimable_bytes: 2048, outputs: OUTPUTS});
  assert.equal(view.cleanLine, '1 temporary file (2.0 KB) can be removed.');
  assert.equal(view.canClean, true);
});

test('an older server without the folder summary still shows its totals; nothing loaded shows that', () => {
  assert.equal(localFilesView({preserved: 3}, {sandbox_file_count: 3, sandbox_size_bytes: 1536}).headline, '3 files · 1.5 KB');
  assert.equal(localFilesView(null, null).headline, 'Not loaded');
  assert.equal(localFilesView(null, null).cleanLine, '');
  assert.deepEqual(localFilesView(null, {sandbox_file_count: 1, sandbox_size_bytes: 0}).months, []);
});

test('files still loose at the top are named as such', () => {
  const view = localFilesView({outputs: {...OUTPUTS, loose: {files: 2, bytes: 10}}});
  assert.equal(view.where.at(-1).label, 'Not in a folder');
});

test('sizes and months read plainly', () => {
  assert.equal(formatSize(0), '0 B');
  assert.equal(formatSize(999), '999 B');
  assert.equal(formatSize(1024 * 1024 * 250), '250 MB');
  assert.equal(formatSize(36.7 * 1024 ** 3), '36.7 GB');
  assert.equal(formatSize(null), '—');
  assert.equal(monthLabel('2026-11'), 'November 2026');
  assert.equal(monthLabel('unsorted'), 'unsorted');
});
