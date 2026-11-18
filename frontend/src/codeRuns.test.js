import assert from 'node:assert/strict';
import test from 'node:test';
import { errorLine, firstMeaningfulLine, foldRuns, groupCards, runKey, runLabel, runSummary, slimSaved, summaryText, withRunCode } from './codeRuns.js';
import { withLegacyIds } from './toolCalls.js';
import { chatActivity } from './chatActivity.js';

const run = (data = {}, seq = 0) => ({ type: 'code_output', seq, data: { language: 'python', success: true, exec_time: 1, ...data } });
const file = name => ({ type: 'file_ready', data: { filename: name } });

test('a run is called what the model said it does', () => {
  // Live 2026-11-14: twelve rows that each said only "python output".
  assert.equal(runLabel(run({ description: '  Clone the\n repository ', code: 'import os' })), 'Clone the repository');
  assert.equal(runLabel(run({ description: 'x'.repeat(200) })).length, 90);
});

test('without a description the first line that says something is used', () => {
  assert.equal(runLabel(run({ code: '#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n\n# Count the Python files\nprint(1)' })), 'Count the Python files');
  assert.equal(runLabel(run({ code: 'import subprocess\nresult = subprocess.run(["git", "clone", "https://example.org/repo"])' })).startsWith('result = subprocess.run'), true);
  assert.equal(firstMeaningfulLine('set -e\ncd /root && ls'), 'cd /root && ls');
  assert.equal(firstMeaningfulLine("'use strict'\nconst fs = require('fs')\n// List the folder\nconsole.log(1)"), 'List the folder');
  assert.equal(runLabel(run({ code: 'import os\n\n' })), 'python run');
  assert.equal(runLabel(run({ language: '' })), 'code run');
});

test('a failed run shows the line that names the error', () => {
  const failed = run({ success: false, stderr: 'Traceback (most recent call last):\n  File "x.py", line 3\nFileNotFoundError: no such file\n' });
  assert.equal(errorLine(failed), 'FileNotFoundError: no such file');
  assert.equal(errorLine(run({ success: false, stdout: 'assert failed\n' })), 'assert failed');
  assert.equal(errorLine(run({ success: false })), 'The run failed.');
  assert.equal(errorLine(run()), '');
});

test('runs with nothing written between them form one list, and a file stays where it was', () => {
  const items = groupCards([run({}, 0), run({}, 1), file('a.zip'), run({}, 2)]);
  assert.deepEqual(items.map(item => item.type === 'runs' ? item.runs.length : item.card.data.filename), [2, 'a.zip', 1]);
  assert.deepEqual(groupCards([]), []);
});

test('the header adds up the runs', () => {
  const summary = runSummary([run({ exec_time: 4 }), run({ exec_time: '1.0' }), run({ success: false, exec_time: 0.5 })]);
  assert.deepEqual(summary, { count: 3, failed: 1, passed: 2, seconds: 5.5 });
  assert.equal(summaryText(summary), '2 passed, 1 failed · 5.5s');
  assert.equal(summaryText(runSummary([run(), run()])), 'all passed · 2.0s');
  assert.equal(summaryText(runSummary([run({ success: false })])), 'all failed · 1.0s');
});

test('a long list shows the newest five and every failed run', () => {
  const runs = Array.from({ length: 12 }, (_, index) => run({ success: index !== 2 }, index));
  const folded = foldRuns(runs);
  assert.deepEqual(folded.rows.map(row => row.index), [2, 7, 8, 9, 10, 11]);
  assert.equal(folded.hidden, 6);
  assert.equal(foldRuns(runs, { showAll: true }).rows.length, 12);
  assert.equal(foldRuns(runs.slice(0, 6)).hidden, 0);        // hiding a single row is not worth a click
});

test('each run keeps its own open state key', () => {
  assert.equal(runKey(run({ tool_call_id: 'c3-ab' }, 4)), 'run:c3-ab');
  assert.equal(runKey(run({}, 4)), 'run:n4');
});

const end = (code, extra = {}) => ({ type: 'tool_end', data: { tool: 'execute_code', status: 'OK (1.0s)', detail: JSON.stringify({ code, language: 'python', stdout: 'out', stderr: '', success: true }), ...extra } });
const start = (status = 'Running python code...', extra = {}) => ({ type: 'tool_start', data: { tool: 'execute_code', status, ...extra } });
const card = (extra = {}) => ({ type: 'code_output', data: { language: 'python', stdout: 'out', success: true, ...extra } });

test('a reply saved before cards carried code takes it from the event before the card', () => {
  const events = [start(), end('print(1)'), card(), start(), end('# Second\nprint(2)'), card()];
  const filled = withRunCode(events);
  assert.deepEqual(filled.filter(event => event.type === 'code_output').map(event => event.data.code), ['print(1)', '# Second\nprint(2)']);
  assert.equal(runLabel(filled[5]), 'Second');
  const current = [end('print(1)'), card({ code: 'print(1)' })];
  assert.equal(withRunCode(current), current);                // nothing to add: the same array
  assert.equal(withRunCode([card()])[0].data.code, undefined);
});

test('the saved events do not carry a run\'s output twice', () => {
  const events = [end('print(1)', { tool_call_id: 'a' }), card({ tool_call_id: 'a', code: 'print(1)' }), end('print(2)'), card()];
  const slim = slimSaved(events);
  assert.deepEqual(JSON.parse(slim[0].data.detail), { code: 'print(1)', language: 'python', success: true });
  assert.equal(slim[2], events[2]);                            // no card with the code: left whole
  assert.equal(slimSaved([card()]).length, 1);
});

test('twelve runs and one search count thirteen, not twenty-six', () => {
  // Live 2026-11-14: every run sent the chat loop's start, the sandbox's start and progress starts.
  const events = [{ type: 'tool_start', data: { tool: 'quick_search', status: 'Searching' } }, { type: 'tool_done', data: { tool: 'quick_search' } }];
  for (let index = 0; index < 12; index += 1) {
    events.push(start('⚡ Executing code (python)'), start(), start('Running python... 3s elapsed'), end(`print(${index})`), card());
  }
  assert.equal(chatActivity({ savedEvents: events }).tools, 13);
  const ids = withLegacyIds(events).filter(event => event.type === 'code_output').map(event => event.data.tool_call_id);
  assert.equal(new Set(ids).size, 12);
});

test('one run that reported progress counts once when the server names the call', () => {
  const named = { tool_call_id: 'c0-1' };
  const events = [start('⚡ Executing code: Count lines', named), start('Running python code...', named),
    { type: 'tool_progress', data: { tool: 'execute_code', ...named } }, start('Running python... 3s elapsed', named), end('print(1)', named)];
  const state = chatActivity({ savedEvents: events });
  assert.equal(state.tools, 1);
  assert.equal(state.entries.find(row => row.kind === 'tool').state, 'complete');
  assert.equal(withLegacyIds(events), events);                 // already named: untouched
});

test('tools that can run side by side are not guessed at', () => {
  const read = type => ({ type, data: { tool: 'read_file' } });
  const events = [read('tool_start'), read('tool_start'), read('tool_end'), read('tool_end')];
  assert.equal(withLegacyIds(events), events);
});

test('progress that arrives after its call ended is not another call', () => {
  // Found 2026-11-15 in the browser: "8 tools" for four runs right after the reply, "4 tools" after a reload.
  const named = { tool_call_id: 'c0-1' };
  const saved = [start('⚡ Executing code: One', named), start('Running python code...', named), end('print(1)', named)];
  const live = [...saved, { type: 'tool_progress', data: { tool: 'execute_code', status: 'Executing code (2s)', ...named } }];
  const state = chatActivity({ savedEvents: saved.map(event => ({ ...event, timestamp: 1 })), events: live.map((event, index) => ({ ...event, timestamp: index < 3 ? 1 : 2 })) });
  assert.equal(state.tools, 1);
  assert.equal(state.entries.find(row => row.kind === 'tool').state, 'complete');
  const unnamed = [start(), end('print(1)'), { type: 'tool_progress', data: { tool: 'execute_code', status: 'late' } }];
  assert.equal(chatActivity({ savedEvents: unnamed }).tools, 1);
});
