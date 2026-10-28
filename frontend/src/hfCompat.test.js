import test from 'node:test';
import assert from 'node:assert/strict';
import { hfCompatibilityReport, isDraft, isMmproj, splitPart } from './hfCompat.js';

const GB = 1e9;
const REPO = {
  id: 'JonathanColetti/Qwen3.8-27B-Uncensored-GGUF',
  pipeline_tag: 'text-generation',
  tags: ['gguf', 'qwen3.8', 'conversational'],
};
const INFO = { gguf_files: [
  { name: 'Qwen3.8-27B-Uncensored-Q4_K_M.gguf', size: 16.8 * GB },
  { name: 'Qwen3.8-27B-Uncensored-draft-Q8_0.gguf', size: 3.2 * GB },
  { name: 'Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf', size: 16.5 * GB },
  { name: 'Qwen3.8-27B-Uncensored-noMTP-Q8_0.gguf', size: 28.6 * GB },
  { name: 'mmproj-Qwen3.8-27B-Uncensored-F16.gguf', size: 0.93 * GB },
] };
const README = 'Vision | yes. Files with the mmproj prefix are the projector.';
const compatFor = (filename, { supports = true, mtp = 'stripped', projector = 0.93 * GB } = {}) => ({
  tag: filename.replace(/\.gguf$/, ''),
  pull_name: `hf.co/${REPO.id}:${filename.replace(/\.gguf$/, '')}`,
  classification: { kind: 'main', quant: 'Q4_K_M', mtp },
  header: { architecture: 'qwen35', type: 'model', mtp_layers: mtp === 'fused' ? 1 : 0 },
  manifest: { model_bytes: 16.5 * GB, projector_bytes: projector },
  ollama: { version: '0.35.0', architectures: ['qwen3', 'qwen35', 'gptoss'], supports_arch: supports },
});
const labels = (r) => r.issues.map((i) => i.label);
const levelOf = (r, label) => r.issues.find((i) => i.label === label)?.level;

test('a text-only quant on a supported architecture is not flagged, even though the repo ships an mmproj', () => {
  const file = 'Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf';
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: [file], compat: compatFor(file) });
  assert.equal(r.level, 'ok');
  assert.equal(r.flagged, false);
  assert.deepEqual(labels(r), ['Architecture qwen35', 'Projector bundled', 'MTP head stripped']);
  assert.equal(levelOf(r, 'Projector bundled'), 'info');
  assert.equal(r.pull.tag, 'Qwen3.8-27B-Uncensored-noMTP-Q4_K_M');
  assert.equal(r.pull.projectorBytes, 0.93 * GB);
});

test('the fused-MTP twin is explained, not flagged', () => {
  const file = 'Qwen3.8-27B-Uncensored-Q4_K_M.gguf';
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: [file], compat: compatFor(file, { mtp: 'fused' }) });
  assert.equal(r.level, 'ok');
  assert.ok(labels(r).includes('MTP head retained'));
});

test('an unknown architecture on this Ollama is a warning and makes the bundled projector a warning too', () => {
  const file = 'Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf';
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: [file], compat: compatFor(file, { supports: false }) });
  assert.equal(r.level, 'warn');
  assert.equal(levelOf(r, 'Architecture qwen35'), 'warn');
  assert.equal(levelOf(r, 'Projector bundled'), 'warn');
});

test('without compat data the README keyword rules still apply and the projector is informational', () => {
  const file = 'Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf';
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README + ' uses gated deltanet layers', selectedFiles: [file], compat: null });
  assert.equal(levelOf(r, 'Architecture risk'), 'warn');
  assert.equal(levelOf(r, 'Projector bundled'), 'info');
  assert.equal(r.pull, null);
});

test('draft heads are flagged as bad; projector-only selections too', () => {
  const draft = 'Qwen3.8-27B-Uncensored-draft-Q8_0.gguf';
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: [draft], compat: { ...compatFor(draft), classification: { kind: 'draft', quant: 'Q8_0', mtp: null } } });
  assert.equal(r.level, 'bad');
  assert.ok(labels(r).includes('Draft model'));
  assert.ok(!labels(r).some((l) => l.startsWith('MTP head')), 'a draft head is not described as an MTP variant');
  const p = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: ['mmproj-Qwen3.8-27B-Uncensored-F16.gguf'], compat: null });
  assert.equal(p.level, 'bad');
  assert.ok(labels(p).includes('Projector only'));
});

test('selecting the main file plus the projector is informational', () => {
  const r = hfCompatibilityReport({ model: REPO, info: INFO, readme: README, selectedFiles: ['Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf', 'mmproj-Qwen3.8-27B-Uncensored-F16.gguf'], compat: null });
  assert.ok(labels(r).includes('Includes projector'));
  assert.ok(!labels(r).includes('Projector bundled'));
});

test('split sets warn until every part is selected', () => {
  const info = { gguf_files: [
    { name: 'Big-Q8_0-00001-of-00002.gguf', size: 20 * GB },
    { name: 'Big-Q8_0-00002-of-00002.gguf', size: 20 * GB },
  ] };
  const half = hfCompatibilityReport({ model: { id: 'x/Big' }, info, readme: '', selectedFiles: ['Big-Q8_0-00001-of-00002.gguf'] });
  assert.equal(levelOf(half, 'Split GGUF'), 'warn');
  const all = hfCompatibilityReport({ model: { id: 'x/Big' }, info, readme: '', selectedFiles: info.gguf_files.map((f) => f.name) });
  assert.equal(levelOf(all, 'Split GGUF'), 'info');
  assert.equal(levelOf(all, 'Large pull'), 'info');
});

test('nothing selected and no files is bad; helpers classify names', () => {
  const r = hfCompatibilityReport({ model: { id: 'x/y' }, info: { gguf_files: [] }, readme: '' });
  assert.equal(r.level, 'bad');
  assert.ok(isDraft('Qwen3.8-27B-Uncensored-draft-Q8_0.gguf'));
  assert.ok(!isDraft('Qwen3.8-27B-Uncensored-noMTP-Q8_0.gguf'));
  assert.ok(isMmproj('mmproj-Qwen3.8-27B-Uncensored-F16.gguf'));
  assert.deepEqual(splitPart('Big-Q8_0-00002-of-00003.gguf'), { part: 2, of: 3 });
});
