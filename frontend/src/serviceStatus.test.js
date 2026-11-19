import test from 'node:test';
import assert from 'node:assert/strict';
import {serviceStatus} from './serviceStatus.js';

const ok = {status: 'ok'}, bad = {status: 'error'}, slow = {status: 'degraded'};
const base = {ollama: ok, codebox: ok, searxng: ok, n8n: ok};

test('nothing wrong is one quiet line', () => {
  assert.deepEqual(serviceStatus(base), {tone: 'ok', text: 'All services up', down: [], slow: []});
  assert.equal(serviceStatus({...base, comfyui: ok, stt: ok, tts: ok}).text, 'All services up');
});

test('what is down is named by what it is for', () => {
  assert.equal(serviceStatus({...base, searxng: bad}).text, 'Search is down');
  assert.equal(serviceStatus({...base, searxng: bad, codebox: bad}).text, 'Sandbox and Search are down');
  assert.equal(serviceStatus({...base, ollama: bad, codebox: bad, n8n: bad}).text, '3 services are down');
  assert.deepEqual(serviceStatus({...base, ollama: bad, codebox: bad, n8n: bad}).down, ['Models', 'Sandbox', 'Automation']);
  assert.equal(serviceStatus({...base, searxng: bad}).tone, 'down');
});

test('slow is said only when nothing is down', () => {
  assert.deepEqual([serviceStatus({...base, searxng: slow}).tone, serviceStatus({...base, searxng: slow}).text], ['slow', 'Search is slow']);
  assert.equal(serviceStatus({...base, searxng: slow, ollama: bad}).text, 'Models is down');
});

test('a core service that did not answer at all counts as down; an optional one that is not installed does not', () => {
  assert.equal(serviceStatus({ollama: ok, codebox: ok, searxng: ok}).text, 'Automation is down');
  assert.equal(serviceStatus({...base}).down.length, 0);            // no Images / voice keys: not installed
  assert.equal(serviceStatus({...base, comfyui: bad}).text, 'Images is down');
});

test('before the first answer it says it is checking', () => {
  assert.equal(serviceStatus({}).tone, 'unknown');
  assert.equal(serviceStatus(null).text, 'Checking services…');
});
