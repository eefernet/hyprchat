import assert from 'node:assert/strict';
import test from 'node:test';
import { downloadLabel, plainDownloadName, withoutLinkedFiles } from './fileLinks.js';

const URL = '/api/downloads/f8725496-sourdough-weekend.docx';

test('a download address gives back the plain file name', () => {
  assert.equal(plainDownloadName(URL), 'sourdough-weekend.docx');
  assert.equal(plainDownloadName('/api/downloads/comfy_00012_.png'), 'comfy_00012_.png');
  assert.equal(plainDownloadName('/api/downloads/abcdef12-'), 'abcdef12-');
  assert.equal(plainDownloadName('/api/downloads/0a1b2c3d-my%20notes.md?user_id=u'), 'my notes.md');
});

test('a download link is named by its text, never by its address', () => {
  assert.equal(downloadLabel('Download sourdough-weekend.docx', URL), 'sourdough-weekend.docx');
  assert.equal(downloadLabel('f8725496-sourdough-weekend.docx', URL), 'sourdough-weekend.docx');
  assert.equal(downloadLabel('📎 f8725496-sourdough-weekend.docx', URL), 'sourdough-weekend.docx');
  assert.equal(downloadLabel('here', URL), 'sourdough-weekend.docx');
  assert.equal(downloadLabel('The weekend plan', URL), 'The weekend plan');
});

test('a link to a file that has its own card is taken out of the text', () => {
  const text = `Here is your plan.\n\n**[Download sourdough-weekend.docx](${URL})**\n\nEnjoy.`;
  assert.equal(withoutLinkedFiles(text, [URL]), 'Here is your plan.\n\nEnjoy.');
  assert.equal(withoutLinkedFiles(`Done.\n- [plan](${URL})`, [URL]), 'Done.\n');
  assert.equal(withoutLinkedFiles(`Saved to ${URL}`, [URL]), `Saved to ${URL}`);
  assert.equal(withoutLinkedFiles(`${URL}`, [URL]), '');
  assert.equal(withoutLinkedFiles(`See [the plan](${URL}) for times.`, [URL]), 'See the plan for times.');
});

test('other links and other files are left alone', () => {
  const other = '[Download b.txt](/api/downloads/11111111-b.txt)';
  assert.equal(withoutLinkedFiles(other, [URL]), other);
  assert.equal(withoutLinkedFiles(other, []), other);
  assert.equal(withoutLinkedFiles('[doc](/api/documents/files/abc)', ['/api/documents/files/abc']), '[doc](/api/documents/files/abc)');
});
