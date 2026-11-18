// Presentation rules from the 2026-11-16 chat pass.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chatActivity, doneLabel} from './chatActivity.js';

test('a finished reply says what it did instead of the word "Activity"', () => {
  assert.equal(doneLabel([{kind: 'reasoning'}]), 'Thought it through');
  assert.equal(doneLabel([{kind: 'tool'}, {kind: 'tool'}]), 'Worked on it');
  assert.equal(doneLabel([{kind: 'reasoning'}, {kind: 'tool'}]), 'Thought and worked');
  assert.equal(doneLabel([{kind: 'review'}]), 'Worked on it');
  assert.equal(doneLabel([]), 'Activity');
  assert.equal(doneLabel(undefined), 'Activity');
});

test('the finished label comes from the reply\'s own events', () => {
  const events = [
    {type: 'thinking', data: {status: 'hm'}},
    {type: 'thought_done', data: {}},
    {type: 'tool_start', data: {tool: 'execute_code', tool_call_id: 'a'}},
    {type: 'tool_end', data: {tool: 'execute_code', tool_call_id: 'a'}},
  ];
  const done = chatActivity({events: [], savedEvents: events, live: false, content: 'answer', status: 'completed'});
  assert.equal(done.phase, 'complete');
  assert.equal(done.label, 'Thought and worked');
  // A stopped or failed reply keeps its own word.
  assert.equal(chatActivity({events: [], savedEvents: events, live: false, content: '', status: 'stopped'}).label, 'Stopped');
  assert.equal(chatActivity({events: [], savedEvents: events, live: false, content: '', status: 'failed'}).label, 'Failed');
});
