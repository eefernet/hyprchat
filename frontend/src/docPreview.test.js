// Documents, drawings and tables in the side pane (2026-11-16).
import test from 'node:test';
import assert from 'node:assert/strict';
import {previewKindFor, looksLikeDocument, opensByItself, docTitle, fileNameFor, svgComplete, svgDataUrl, delimiterOf, parseDelimited, opensWhenDelivered, usesArtifactView} from './docPreview.js';
import {foldLongFences, unfoldFence, FENCE_MARK} from './longFences.js';
import {parseCodeFence} from './syntaxHighlight.js';

test('the pane knows which blocks it can show', () => {
  assert.equal(previewKindFor('html'), 'html');
  assert.equal(previewKindFor('markdown'), 'markdown');
  assert.equal(previewKindFor('md', 'notes.md'), 'markdown');
  assert.equal(previewKindFor('svg'), 'svg');
  assert.equal(previewKindFor('csv'), 'table');
  assert.equal(previewKindFor('tsv'), 'table');
  // A block labelled with a programming language is code, whatever its file is called.
  assert.equal(previewKindFor('python', 'report.md'), null);
  assert.equal(previewKindFor('json'), null);
  // A plain block that names a file goes by the file.
  assert.equal(previewKindFor('text', 'README.md'), 'markdown');
  assert.equal(previewKindFor('', 'data.csv'), 'table');
  assert.equal(previewKindFor('', ''), null);
});

test('a short Markdown snippet does not take the pane; a document does', () => {
  const snippet = '# Heading\n\nUse **bold** like this.';
  const report = ['# Quarterly report', '', ...Array.from({length: 30}, (_, i) => `Line ${i} of the report.`)].join('\n');
  assert.equal(looksLikeDocument(snippet), false);
  assert.equal(looksLikeDocument(report), true);
  assert.equal(looksLikeDocument(snippet, 'README.md'), true);       // the author named a file
  assert.equal(looksLikeDocument(report.replace('# Quarterly report', 'Just prose')), false);
  assert.equal(looksLikeDocument('   '), false);
  assert.equal(opensByItself('markdown', report, ''), true);
  assert.equal(opensByItself('svg', '<svg></svg>', 'logo.svg'), false);   // drawings and tables keep their button
  assert.equal(opensByItself('table', 'a,b\n1,2', 'data.csv'), false);
});

test('titles and file names come from the author first', () => {
  assert.equal(docTitle('markdown', 'docs/guide.md', '# Other'), 'guide.md');
  assert.equal(docTitle('markdown', '', 'intro\n\n## Setup guide ##\ntext'), 'Setup guide');
  assert.equal(docTitle('svg', '', '<svg><title>Logo mark</title></svg>'), 'Logo mark');
  assert.equal(docTitle('table', '', 'a,b'), 'Table');
  assert.equal(fileNameFor('markdown', 'Setup guide', ''), 'Setup-guide.md');
  assert.equal(fileNameFor('markdown', 'x', 'docs/guide.md'), 'guide.md');
  assert.equal(fileNameFor('svg', 'Drawing', ''), 'Drawing.svg');
  assert.equal(fileNameFor('table', 'Table', '', 'a\tb\n1\t2'), 'Table.tsv');
  assert.equal(fileNameFor('html', '', ''), 'preview.html');
});

test('a drawing is shown only once it is complete, as an image', () => {
  assert.equal(svgComplete('<svg viewBox="0 0 1 1"><rect/>'), false);
  assert.equal(svgComplete('<svg viewBox="0 0 1 1"><rect/></svg>\n'), true);
  assert.equal(svgComplete('</svg>'), false);
  assert.ok(svgDataUrl('<svg><script>alert(1)</script></svg>').startsWith('data:image/svg+xml;charset=utf-8,%3Csvg'));
});

test('tables read quoted fields, doubled quotes and line breaks inside quotes', () => {
  const table = parseDelimited('name,note\n"Smith, J","said ""hi"""\n"two\nlines",x\n\nlast,row,extra\n');
  assert.deepEqual(table.header, ['name', 'note', '']);
  assert.deepEqual(table.rows, [['Smith, J', 'said "hi"', ''], ['two\nlines', 'x', ''], ['last', 'row', 'extra']]);
  assert.equal(table.total, 3);
  assert.equal(table.truncated, false);
  assert.equal(delimiterOf('tsv'), '\t');
  assert.equal(delimiterOf('', 'data.tsv'), '\t');
  assert.equal(delimiterOf('csv', '', 'a\tb'), '\t');
  assert.equal(delimiterOf('csv', '', 'a,b'), ',');
  assert.deepEqual(parseDelimited('a\tb\n1\t2', {delimiter: '\t'}).rows, [['1', '2']]);
});

test('a large table draws its first rows and says how many there are', () => {
  const text = 'n\n' + Array.from({length: 1200}, (_, i) => i).join('\n');
  const table = parseDelimited(text, {maxRows: 500});
  assert.equal(table.rows.length, 500);
  assert.equal(table.total, 1200);
  assert.equal(table.truncated, true);
  assert.deepEqual(parseDelimited('').rows, []);
});

test('delivered files that open by themselves, and which use the document renderer', () => {
  for (const name of ['report.docx', 'deck.pptx', 'budget.xlsx', 'paper.pdf', 'chart.png', 'notes.md', 'page.html', 'data.csv']) assert.equal(opensWhenDelivered(name), true, name);
  for (const name of ['project.tar.gz', 'bundle.zip', 'app.py', 'binary', '']) assert.equal(opensWhenDelivered(name), false, name);
  assert.equal(usesArtifactView('report.docx'), true);
  assert.equal(usesArtifactView('data.csv'), true);
  assert.equal(usesArtifactView('notes.md'), false);
});

// ── documents that contain code ─────────────────────────────────────────────────────────────────────
const DOC = ['Here is the file:', '', '````markdown README.md', '# Tool', '', '```bash', 'npm install', '```', '', 'Done.', '````', '', 'Anything else?'].join('\n');

test('a Markdown document with code blocks inside it is not cut at the first inner fence', () => {
  const folded = foldLongFences(DOC);
  const fences = folded.split('\n').filter(line => /^```/.test(line));
  assert.deepEqual(fences, ['```markdown README.md', '```']);            // one block, opened and closed once
  const block = folded.slice(folded.indexOf('```markdown'), folded.lastIndexOf('```') + 3);
  const parsed = parseCodeFence(block);
  assert.equal(parsed.label, 'markdown');
  assert.equal(parsed.meta.filename, 'README.md');
  assert.equal(unfoldFence(parsed.code), '# Tool\n\n```bash\nnpm install\n```\n\nDone.');
  assert.ok(folded.endsWith('\nAnything else?'));
});

test('text without nesting is returned untouched, and four backticks inside a normal block are text', () => {
  const plain = 'a\n```js\nconst x = 1;\n```\nb';
  assert.equal(foldLongFences(plain), plain);
  const inside = '```text\nUse ```` to nest.\n````\nstill inside\n```\nafter';
  assert.equal(foldLongFences(inside), inside);
  assert.equal(unfoldFence('no marker'), 'no marker');
});

// What a model usually writes: three backticks for the document AND for the code inside it.
const USUAL = ['```markdown', '# tidepool', '', '## Install', '', '```bash', 'pip install tidepool', '```', '', '## Usage', '', '```python', 'import tidepool', '```', '', '## License', 'MIT', '```', '', 'Let me know.'].join('\n');

test('a ```markdown document with ``` code blocks inside it stays one block', () => {
  const folded = foldLongFences(USUAL);
  const fences = folded.split('\n').filter(line => /^```/.test(line));
  assert.deepEqual(fences, ['```markdown', '```']);
  const parsed = parseCodeFence(folded.slice(0, folded.lastIndexOf('```') + 3));
  assert.equal(unfoldFence(parsed.code), USUAL.split('\n').slice(1, 17).join('\n'));
  assert.ok(folded.endsWith('\nLet me know.'));
});

test('a short ```markdown snippet followed by other blocks is left exactly as it was', () => {
  const separate = '```markdown\n# hi\n```\n\nThen run:\n\n```bash\nls\n```\ndone';
  assert.equal(foldLongFences(separate), separate);
});

test('a document still being written keeps its unfinished inner block inside it', () => {
  const writing = '```md plan.md\n# Plan\n\n```py\nprint(1)';
  const folded = foldLongFences(writing);
  assert.equal((folded.match(/(?:^|\n)[ \t]{0,3}```/g) || []).length, 1);
  // The label has to be the whole word: ```mdx-something or ```markdownlint is not a document.
  const other = '```markdownlint\nrule\n```js\nx\n```';
  assert.equal(foldLongFences(other), other);
});

test('a long fence still being written keeps its inner fences out of the count', () => {
  const streaming = '````markdown plan.md\n# Plan\n\n```py\nprint(1)';
  const folded = foldLongFences(streaming);
  assert.equal((folded.match(/(?:^|\n)[ \t]{0,3}```/g) || []).length, 1);   // odd: the renderer closes it
  assert.ok(folded.includes(FENCE_MARK));
  // A longer closing fence closes a shorter opening one; a shorter one does not.
  assert.equal(foldLongFences('`````md\na\n````\nb\n`````\nc').split('\n').filter(l => l === '```').length, 1);
});
