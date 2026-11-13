import assert from 'node:assert/strict';
import test from 'node:test';
import { capSavedEvents, isCard, mergeContinued, messageParts, resetOffsets, splitPoint, stampCard } from './messageParts.js';

const output = (stdout, at, success = true) => ({ type: 'code_output', data: { language: 'python', stdout, success }, ...(at == null ? {} : { at }) });
const file = (filename, at) => ({ type: 'file_ready', data: { filename, url: `/dl/${filename}` }, ...(at == null ? {} : { at }) });
const texts = result => result.parts.map(part => part.text.trim());
const shown = result => result.parts.map(part => part.cards.map(card => card.data.stdout || card.data.filename));

const ROUND_1 = 'Now let me resize, convert to WebP, and build the zip:';
const ROUND_2 = 'Interesting — some images grow. Let me verify:';
const ROUND_3 = 'Every value verified. Here is the table.';

test('only output and file events are cards, and only cards are stamped', () => {
  assert.equal(isCard(output('x')), true);
  assert.equal(isCard({ type: 'tool_end', data: {} }), false);
  assert.equal(stampCard({ type: 'tool_start', data: {} }, 10).at, undefined);
  assert.equal(stampCard(output('x'), 10).at, 10);
});

test('each card sits under the text of the round that ran it', () => {
  // Live 2026-11-13: seven python outputs and a zip were all stacked under the final table.
  const content = [ROUND_1, ROUND_2, ROUND_3].join('\n\n');
  const first = ROUND_1.length, second = first + 2 + ROUND_2.length;
  const result = messageParts(content, [
    output('ModuleNotFoundError', first, false), output('Pillow installed', first),
    { type: 'tool_end', data: { tool: 'execute_code' } },
    output('all 6 match', second), file('images.zip', second),
  ]);
  assert.deepEqual(texts(result), [ROUND_1, ROUND_2, ROUND_3]);
  assert.deepEqual(shown(result), [['ModuleNotFoundError', 'Pillow installed'], ['all 6 match', 'images.zip'], []]);
  assert.deepEqual(result.trailing, []);
});

test('while the tool is running its card is the last thing in the reply', () => {
  const result = messageParts(ROUND_1, [output('running', ROUND_1.length)]);
  assert.deepEqual(texts(result), [ROUND_1]);
  assert.deepEqual(shown(result), [['running']]);
});

test('a stamp that lands early moves to the end of its paragraph', () => {
  const content = `${ROUND_1}\n\n${ROUND_2}`;
  assert.equal(splitPoint(content, ROUND_1.length - 12), ROUND_1.length);
  assert.equal(splitPoint(content, ROUND_1.length + 2), ROUND_1.length);   // just past the break
  assert.equal(splitPoint(content, 9999), content.length);
  assert.equal(splitPoint(content, 0), 0);
});

test('a card never lands inside a code block', () => {
  const code = '```python\nprint(1)\n\nprint(2)\n```';
  const content = `Here is the script:\n\n${code}\n\nDone.`;
  const point = splitPoint(content, content.indexOf('print(1)'));
  assert.equal(content.slice(0, point).endsWith('```'), true);
  assert.deepEqual(texts(messageParts(content, [output('1', content.indexOf('print(1)'))])), [`Here is the script:\n\n${code}`, 'Done.']);
});

test('messages saved before stamps existed keep the old layout', () => {
  const content = `${ROUND_1}\n\n${ROUND_2}`;
  const result = messageParts(content, [output('a'), file('x.zip')]);
  assert.deepEqual(result.parts, [{ text: content, cards: [] }]);
  assert.deepEqual(result.trailing.map(card => card.type), ['code_output', 'file_ready']);
});

test('a reply with footnotes is not split', () => {
  const content = `Claim[^1].\n\nMore text.\n\n[^1]: Source`;
  const result = messageParts(content, [output('a', 10)]);
  assert.equal(result.parts.length, 1);
  assert.equal(result.trailing.length, 1);
});

test('a file delivered twice shows once, at its latest place, with a version count', () => {
  const content = `${ROUND_1}\n\n${ROUND_2}`;
  const result = messageParts(content, [file('out.zip', ROUND_1.length), file('out.zip', content.length)]);
  assert.deepEqual(shown(result), [['out.zip']]);
  assert.equal(result.parts[0].text, content);
  assert.equal(result.parts[0].cards[0].versions, 2);
});

test('a wiped reply moves earlier cards above the new text; a replaced one drops their places', () => {
  const cleared = [output('a', 40), output('b')];
  resetOffsets(cleared, 'clear');
  assert.deepEqual(cleared.map(event => event.at), [0, undefined]);
  const result = messageParts('Fresh text after the tool round.', [...cleared.slice(0, 1), output('c', 0)]);
  assert.deepEqual(texts(result), ['', 'Fresh text after the tool round.']);
  assert.deepEqual(shown(result)[0], ['a', 'c']);

  const replaced = [output('a', 40)];
  resetOffsets(replaced, 'replace');
  assert.equal('at' in replaced[0], false);
});

test('the saved-event cap drops progress events before cards', () => {
  // execute_code emits a progress event every 3 s; a long turn used to push its first outputs out of the cap.
  const noise = index => ({ type: 'tool_start', data: { tool: 'execute_code', status: `${index}s elapsed` } });
  const events = [output('first', 5), ...Array.from({ length: 200 }, (_, index) => noise(index)), output('last', 90)];
  const capped = capSavedEvents(events, 150);
  assert.equal(capped.length, 150);
  assert.deepEqual(capped.filter(isCard).map(event => event.data.stdout), ['first', 'last']);
  assert.equal(capped[1].data.status, '52s elapsed');      // the oldest progress events went first
  assert.equal(capSavedEvents(events.slice(0, 20), 150).length, 20);
});

test('continuing a reply keeps the cards from its first half', () => {
  const merged = mergeContinued([output('before', 10), { type: 'tool_end', data: {} }], [output('after', 80)]);
  assert.deepEqual(merged.map(event => event.data.stdout), ['before', 'after']);
  assert.deepEqual(mergeContinued([], [output('only', 3)]).length, 1);
});
