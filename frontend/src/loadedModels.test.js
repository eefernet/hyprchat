import test from 'node:test';
import assert from 'node:assert/strict';
import { LIMIT_CHOICES, limitChoice, limitLabel, limitLine, loadedLine, modelRow } from './loadedModels.js';

test('the stored limit becomes one of the choices', () => {
  assert.deepEqual([0, 1, 3, 5, 7, 8, 40, -2, 'x', null, '2'].map(limitChoice), [0, 1, 3, 4, 6, 8, 8, 0, 0, 0, 2]);
  assert.ok(LIMIT_CHOICES.every(choice => limitChoice(choice) === choice));
});

test('off is worded as Ollama deciding', () => {
  assert.equal(limitLabel(0), 'Ollama decides');
  assert.equal(limitLabel(3), '3');
  assert.match(limitLine(0), /Ollama keeps models loaded/);
  assert.match(limitLine(1), /unloads the other one/);
  assert.match(limitLine(3), /When 3 models are loaded and a chat needs another/);
});

test('a loaded model shows its size, context and when it unloads', () => {
  const now = Date.parse('2026-11-17T06:00:00Z');
  const row = modelRow({name: 'qwen3.8:27b', size_bytes: 20 * 1024 ** 3, gpu_bytes: 20 * 1024 ** 3, context: 65536,
    expires_at: '2026-11-16T23:08:24.494123456-07:00', helper: false}, now);
  assert.deepEqual(row, {name: 'qwen3.8:27b', helper: false, detail: '20.0 GB · 65,536 context · unloads in 8 min if unused', spill: ''});
});

test('a model that is partly off the GPU says so', () => {
  const row = modelRow({name: 'big:30b', size_bytes: 20e9, gpu_bytes: 18.4e9, context: 0, expires_at: ''});
  assert.equal(row.spill, '8% not on the GPU');
  assert.equal(modelRow({name: 'cpu:1b', size_bytes: 1e9, gpu_bytes: 0}).spill, '100% not on the GPU');
  assert.equal(modelRow({name: 'odd', size_bytes: 0, gpu_bytes: 0}).spill, '');
});

test('a model kept forever shows no unload time', () => {
  const now = Date.parse('2026-11-17T06:00:00Z');
  assert.equal(modelRow({name: 'pinned', size_bytes: 1024 ** 3, gpu_bytes: 1024 ** 3, expires_at: '2318-01-01T00:00:00Z'}, now).detail, '1.0 GB');
});

test('the line above the list counts helper models apart', () => {
  assert.equal(loadedLine(null), 'Checking…');
  assert.equal(loadedLine({reachable: false, models: []}), 'Ollama could not be reached.');
  assert.equal(loadedLine({reachable: true, models: []}), 'Nothing is loaded right now.');
  assert.equal(loadedLine({reachable: true, models: [{name: 'a'}]}), '1 model loaded');
  assert.equal(loadedLine({reachable: true, models: [{name: 'a'}, {name: 'b'}, {name: 'e', helper: true}]}), '2 models loaded · 1 helper');
});
