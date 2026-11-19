import test from 'node:test';
import assert from 'node:assert/strict';
import {isInstalledModel, installedRows, displayName, searchRows, abilities, guessAbilities, rowSummary, parameterWords, quantMeaning,
  contextLabel, settingsContextLabel, contextNote, hasOverrides, overrideCount, withoutModel, sliderValue, toolFix, fitView, speedWords,
  searchOutcome, repoFiles, repoError} from './modelsPage.js';

test('the routing entry and cloud models are not installed models', () => {
  assert.equal(isInstalledModel('auto'), false);
  assert.equal(isInstalledModel('openai:gpt-5'), false);
  assert.equal(isInstalledModel('custom:thing'), false);
  assert.equal(isInstalledModel('qwen3.8:27b'), true);
});

test('one downloaded file under two names is one row, led by the name the user chose', () => {
  const details = {'hf.co/unsloth/Qwen3.6-GGUF:Q4_K_M': {digest: 'abc'}, 'qwen36-q4': {digest: 'abc'}, 'llama3.1:8b': {digest: 'def'}, 'no-digest': {}};
  const rows = installedRows(['hf.co/unsloth/Qwen3.6-GGUF:Q4_K_M', 'auto', 'qwen36-q4', 'llama3.1:8b', 'no-digest', 'anthropic:claude'], details);
  assert.deepEqual(rows, [
    {name: 'qwen36-q4', aliases: ['hf.co/unsloth/Qwen3.6-GGUF:Q4_K_M'], huggingFace: true},
    {name: 'llama3.1:8b', aliases: [], huggingFace: false},
    {name: 'no-digest', aliases: [], huggingFace: false},
  ]);
  assert.equal(searchRows(rows, 'unsloth').length, 1);        // an alias is searchable too
  assert.equal(displayName('hf.co/unsloth/Qwen3.6-GGUF:Q4_K_M'), 'Qwen3.6-GGUF (Q4_K_M)');
});

test('a re-created alias of a Hugging Face download is the same row, but an equal-sized stranger is not', () => {
  const details = {
    'mirothinker-v1.5-30b-gguf:latest': {digest: 'a', size: 22312825568},
    'hf.co/Mungert/MiroThinker-v1.5-30B-GGUF:MiroThinker-v1.5-30B-q5_k_m': {digest: 'b', size: 22312826006},
    'qwen3.6:27b': {digest: 'c', size: 17420432739},
    'huihui_ai/qwen3.5-abliterated:27b': {digest: 'd', size: 17420432772},     // 33 bytes apart, a different model
    'hf.co/MaziyarPanahi/phi-4-GGUF:Q2_K': {digest: 'e', size: 5547348939},     // no alias: stays its own row
  };
  const rows = installedRows(Object.keys(details), details);
  assert.deepEqual(rows.map(r => [r.name, r.aliases.length, r.huggingFace]), [
    ['mirothinker-v1.5-30b-gguf:latest', 1, true], ['qwen3.6:27b', 0, false], ['huihui_ai/qwen3.5-abliterated:27b', 0, false], ['hf.co/MaziyarPanahi/phi-4-GGUF:Q2_K', 0, true],
  ]);
});

test('abilities come from what Ollama reports, with the gaps named', () => {
  const view = abilities('gemma3:27b-it', {capabilities: ['completion', 'vision']});
  assert.equal(view.guessed, false);
  assert.deepEqual(view.list.map(a => a.id), ['vision']);
  assert.deepEqual(view.missing.map(a => a.label), ['No tools', 'No visible thinking']);
  assert.deepEqual(abilities('nomic-embed-text', {capabilities: ['embedding']}).missing, []);
});

test('without a report the name gives a marked guess, and never claims a gap', () => {
  const view = abilities('qwen3.8:27b', null);
  assert.equal(view.guessed, true);
  assert.deepEqual(view.list.map(a => a.id), ['tools', 'thinking']);
  assert.deepEqual(view.missing, []);
});

test('the name guess no longer reads "it" or a stray "vl" into every name', () => {
  assert.deepEqual(guessAbilities('summit:7b'), []);                   // ended in "it"
  assert.deepEqual(guessAbilities('qwen2.5vl-7b'), ['tools']);         // "vl" inside a word is not vision
  assert.deepEqual(guessAbilities('qwen2.5-vl:7b'), ['tools', 'vision']);
  assert.deepEqual(guessAbilities('nomic-embed-text'), ['embedding']);
  assert.deepEqual(guessAbilities('gpt-oss:20b'), ['tools', 'thinking']);
});

test('the list line says size and abilities in words', () => {
  assert.equal(rowSummary('qwen3.8:27b', {size: 17.7e9}, {capabilities: ['completion', 'tools', 'thinking']}), '17.7 GB · tools, thinking');
  assert.equal(rowSummary('x', {}, {capabilities: ['completion']}), '');
});

test('size and compression are explained', () => {
  assert.equal(parameterWords('27.3B'), '27.3 billion parameters');
  assert.equal(parameterWords('568M'), '568 million parameters');
  assert.match(quantMeaning('Q4_K_M'), /4-bit/);
  assert.match(quantMeaning('q8_0'), /8-bit/);
  assert.match(quantMeaning('IQ3_XS'), /2 to 3-bit/);
  assert.equal(quantMeaning(''), '');
});

test('a context size reads as a size, and "0" as words', () => {
  assert.equal(contextLabel(65536), '64K');
  assert.equal(contextLabel(40000), '40,000');
  assert.equal(settingsContextLabel(0), "Ollama's default");
  assert.equal(settingsContextLabel(16384), '16K');
  assert.equal(contextNote(262144, 131072).tone, 'warn');
  assert.match(contextNote(32768, 131072).text, /up to 131,072/);
  assert.equal(contextNote(32768, 0).tone, 'info');
});

test('only real settings count as overrides, and a deleted model takes its settings along', () => {
  const params = {'a:1b': {temperature: '0.4'}, 'b:1b': {temperature: '', num_ctx: 'default'}, 'gone:1b': {top_k: '40'}};
  assert.equal(hasOverrides(params['b:1b']), false);
  assert.equal(overrideCount(params, ['a:1b', 'b:1b']), 1);            // the deleted model is not counted
  assert.deepEqual(Object.keys(withoutModel(params, 'gone:1b')), ['a:1b', 'b:1b']);
  assert.equal(withoutModel(params, 'never'), params);
});

test('a slider shows the default until the model has its own value', () => {
  assert.deepEqual(sliderValue('temperature', {}, {temperature: 0.6}), {own: false, value: 0.6});
  assert.deepEqual(sliderValue('temperature', {temperature: '1.2'}, {temperature: 0.6}), {own: true, value: 1.2});
  assert.deepEqual(sliderValue('repeat_penalty', {repeat_penalty: 'default'}, {}), {own: false, value: 1.1});
});

test('the tool-calling fix is offered only where it can help', () => {
  assert.equal(toolFix('qwen3.8:27b', {capabilities: ['completion', 'tools']}), null);
  assert.equal(toolFix('nomic-embed-text', {capabilities: ['embedding']}), null);
  assert.deepEqual(toolFix('hf.co/x/Mistral-GGUF:Q4', {capabilities: ['completion']}), {family: 'mistral', known: true});
  assert.deepEqual(toolFix('mystery:7b', null), {family: 'chatml', known: false});
});

test('a recommendation reads as one sentence with a fit bar', () => {
  const view = fitView({fit: 'great', required_gb: 6.61, estimated_speed: '~120.7 tok/s', context_tokens: 32768}, {total_vram_gb: 24});
  assert.deepEqual(view, {label: 'Fits easily', tone: 'ok', percent: 28, over: false, sentence: 'Needs 6.6 GB of your 24 GB · about 121 tokens a second · 32K context'});
  const big = fitView({fit: 'too_large', required_gb: 60}, {total_vram_gb: 24});
  assert.deepEqual([big.label, big.tone, big.percent, big.over], ['Too large for this server', 'bad', 100, true]);
  assert.equal(fitView({fit: 'fits', required_gb: 20}, {unified_memory: true, system_ram_gb: 64, total_vram_gb: 0}).sentence, 'Needs 20 GB of your 64 GB');
  assert.equal(fitView({}, {}).label, 'Fit unknown');
});

test('speed estimates read as words', () => {
  assert.equal(speedWords('~43 tok/s, RAM-spill risk'), 'about 43 tokens a second, may spill into slower system memory');
  assert.equal(speedWords('not recommended'), 'Not recommended');
  assert.equal(speedWords(''), '');
});

test('a failed Hugging Face answer is an error, not an empty list or a crash', () => {
  assert.deepEqual(searchOutcome([{id: 'a/b'}]), {results: [{id: 'a/b'}], error: ''});
  assert.deepEqual(searchOutcome({detail: 'HF search failed: timeout'}, false), {results: [], error: 'HF search failed: timeout'});
  assert.equal(searchOutcome(null, false).error, 'Hugging Face could not be reached.');
  assert.equal(repoFiles({detail: 'boom'}), null);
  assert.equal(repoError({detail: 'boom'}), 'boom');
  assert.deepEqual(repoFiles({gguf_files: []}), []);
  assert.equal(repoError({gguf_files: []}), '');
  assert.equal(repoError(null), '');
});
