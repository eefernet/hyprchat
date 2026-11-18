import assert from 'node:assert/strict';
import test from 'node:test';
import { failureDetail, withFile, withoutFile } from './kbFiles.js';

test('a second upload under the same name replaces its row', () => {
  const files = [{ id: 1, filename: 'a.md', file_size: 10 }, { id: 2, filename: 'b.md', file_size: 5 }];
  assert.deepEqual(withFile(files, { id: 1, filename: 'a.md', file_size: 99 }),
    [{ id: 1, filename: 'a.md', file_size: 99 }, { id: 2, filename: 'b.md', file_size: 5 }]);
  assert.equal(withFile(files, { id: 3, filename: 'c.md' }).length, 3);
  assert.equal(withFile(files, { id: 7, filename: 'b.md' }).length, 2);      // same name, new id from the server
  assert.deepEqual(withFile(null, { id: 1, filename: 'a.md' }), [{ id: 1, filename: 'a.md' }]);
});

test('a row is removed by id, never by its position', () => {
  const files = [{ id: 1, filename: 'a.md' }, { id: 2, filename: 'a.md' }, { filename: 'draft.md' }];
  assert.deepEqual(withoutFile(files, { id: 2, filename: 'a.md' }), [{ id: 1, filename: 'a.md' }, { filename: 'draft.md' }]);
  assert.deepEqual(withoutFile(files, { filename: 'draft.md' }).length, 2);
});

test('a refused request names the server reason', async () => {
  assert.equal(await failureDetail({ status: 404, json: async () => ({ detail: 'KB not found' }) }), 'KB not found');
  assert.equal(await failureDetail({ status: 500, json: async () => { throw new Error('no body'); } }), 'HTTP 500');
});
