// Rules and wording for the Models page (panels/ModelsPanel.jsx). Pure, so node:test can import it.
//
// The page answers three questions in plain words: what is installed and what can it do, what would
// run well on this server, and what else can be downloaded. Everything a user reads is worked out here
// so the components only draw it.

const CLOUD = /^(openai|anthropic|custom):/;

// What a person can install, delete and tune: the local Ollama models. Cloud models are chosen in the
// chat's model picker, and "auto" is the routing entry, not a model.
export const isInstalledModel = name => !!name && name !== 'auto' && !CLOUD.test(String(name));
export const fromHuggingFace = name => String(name || '').startsWith('hf.co/');

// A Hugging Face download leaves the same file under two names: the hf.co/… pull and the name the user
// gave it. They are one row, led by the user's name, with the other as an alias. Two names are the same
// download when Ollama gives them one digest, or (the alias is re-created, so its digest and size differ
// by a few bytes) when the plain name is the repository's name and the sizes are within a megabyte.
// Size alone is not enough: two different fine-tunes of one base model can differ by 33 bytes.
const plain = text => String(text || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const repoBase = name => plain(String(name).slice('hf.co/'.length).split(':')[0].split('/').pop());
export function installedRows(models, modelDetails = {}) {
  const rows = [], byDigest = new Map();
  const merge = (row, name) => {
    row.huggingFace = row.huggingFace || fromHuggingFace(name);
    if (fromHuggingFace(row.name) && !fromHuggingFace(name)) { row.aliases.push(row.name); row.name = name; }
    else row.aliases.push(name);
  };
  for (const name of (models || []).filter(isInstalledModel)) {
    const digest = modelDetails?.[name]?.digest || '';
    const same = digest && byDigest.get(digest);
    if (same) { merge(same, name); continue; }
    const row = {name, aliases: [], huggingFace: fromHuggingFace(name)};
    if (digest) byDigest.set(digest, row);
    rows.push(row);
  }
  const size = name => Number(modelDetails?.[name]?.size) || 0;
  for (const hf of rows.filter(row => fromHuggingFace(row.name))) {
    const twin = rows.find(row => row !== hf && !row.gone && !fromHuggingFace(row.name) && plain(row.name.split(':')[0]) === repoBase(hf.name)
      && size(row.name) && Math.abs(size(row.name) - size(hf.name)) < 1e6);
    if (!twin) continue;
    twin.huggingFace = true;
    twin.aliases.push(hf.name, ...hf.aliases);
    hf.gone = true;
  }
  return rows.filter(row => !row.gone);
}

export function displayName(name) {
  const text = String(name || '');
  if (!fromHuggingFace(text)) return text;
  const [repo, tag] = text.slice('hf.co/'.length).split(':');
  const base = repo.split('/').pop() || repo;
  return tag && tag !== 'latest' ? `${base} (${tag})` : base;
}

export function searchRows(rows, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return rows;
  return rows.filter(row => [row.name, ...row.aliases].some(n => n.toLowerCase().includes(q)));
}

// ── What a model can do ──
// Ollama reports it (`facts.capabilities`, backend/model_facts.py). Until that answer is in, or for a
// model Ollama could not read, the name gives a guess, marked as one.
const ABILITIES = [
  ['tools', 'Uses tools', 'Can run code, search the web and call your connectors.'],
  ['thinking', 'Shows its thinking', 'Reasons step by step before it answers; you can read the reasoning.'],
  ['vision', 'Reads images', 'You can attach pictures and screenshots.'],
  ['embedding', 'Embeddings only', 'Turns text into numbers for search. It cannot chat.'],
];

export function guessAbilities(name) {
  const b = String(name || '').toLowerCase();
  if (/embed|bge-|nomic|minilm/.test(b)) return ['embedding'];
  const found = [];
  if (/qwen|llama3|llama-3|mistral|mixtral|command|hermes|deepseek|phi3|phi-3|phi4|wizardlm|gemma|gpt-oss|granite/.test(b)) found.push('tools');
  if (/qwen3|deepseek-r1|(^|[-:/_])r1([-:]|$)|qwq|thinking|reason|gpt-oss/.test(b)) found.push('thinking');
  if (/llava|vision|(^|[-:/_.])vl($|[-:_.])|pixtral|minicpm-v|moondream/.test(b)) found.push('vision');
  return found;
}

export function abilities(name, facts) {
  const reported = Array.isArray(facts?.capabilities) ? facts.capabilities : null;
  const has = new Set(reported || guessAbilities(name));
  return {
    guessed: !reported,
    list: ABILITIES.filter(([id]) => has.has(id)).map(([id, label, detail]) => ({id, label, detail})),
    // Worth saying only when Ollama said so: "no tools" from a name guess would be a second guess.
    missing: reported && !has.has('embedding') ? ABILITIES.filter(([id]) => id !== 'embedding' && !has.has(id)).map(([id, label]) => ({id, label: {tools: 'No tools', thinking: 'No visible thinking', vision: 'No images'}[id] || label})) : [],
  };
}

// The short line under a model's name in the list.
export function rowSummary(name, details, facts) {
  const parts = [];
  const size = Number(details?.size) || 0;
  if (size) parts.push(formatSize(size));
  const able = abilities(name, facts).list.map(a => ({tools: 'tools', thinking: 'thinking', vision: 'images', embedding: 'embeddings'}[a.id]));
  if (able.length) parts.push(able.join(', '));
  return parts.join(' · ');
}

export function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (!n) return '';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.round(n / 1e3)} KB`;
}

// ── Size in plain words ──
export function parameterWords(parameterSize) {
  const m = String(parameterSize || '').match(/^([\d.]+)\s*([BMK])$/i);
  if (!m) return String(parameterSize || '');
  return `${m[1]} ${{B: 'billion', M: 'million', K: 'thousand'}[m[2].toUpperCase()]} parameters`;
}

// "Quantization" is how far the model's numbers were rounded to make the file smaller.
export function quantMeaning(quant) {
  const q = String(quant || '').toUpperCase();
  if (!q) return '';
  if (/^(F32|FP32)$/.test(q)) return '32-bit, uncompressed: full quality, very large.';
  if (/^(F16|FP16|BF16)$/.test(q)) return '16-bit: full quality, about four times the size of 4-bit.';
  if (/^Q8/.test(q)) return '8-bit: almost no quality loss, about twice the size of 4-bit.';
  if (/^Q6/.test(q)) return '6-bit: slightly better than 4-bit, and larger.';
  if (/^Q5/.test(q)) return '5-bit: slightly better than 4-bit, and larger.';
  if (/^Q4/.test(q)) return '4-bit: the usual balance of quality and size.';
  if (/^(Q3|Q2|Q1|IQ)/.test(q)) return '2 to 3-bit: small and fast, with a visible loss of quality.';
  return '';
}

// ── Context window ──
export const CONTEXT_PRESETS = [8192, 16384, 32768, 65536, 131072, 262144];
export function contextLabel(tokens) {
  const n = Number(tokens) || 0;
  if (!n) return '';
  return n >= 1024 && n % 1024 === 0 ? `${n / 1024}K` : n.toLocaleString();
}
// The Settings value shown where a model inherits it. 0 means HyprChat sends no size and Ollama picks.
export const settingsContextLabel = numCtx => (Number(numCtx) > 0 ? contextLabel(numCtx) : "Ollama's default");

export function contextNote(effective, limit) {
  const asked = Number(effective) || 0, own = Number(limit) || 0;
  if (own && asked > own) return {tone: 'warn', text: `This model can hold ${own.toLocaleString()} tokens at most, so chats with it use ${own.toLocaleString()}, not ${asked.toLocaleString()}.`};
  if (own) return {tone: 'info', text: `This model can hold up to ${own.toLocaleString()} tokens. A larger window uses more GPU memory.`};
  return {tone: 'info', text: 'A larger window lets a chat run longer before it is summarised, and uses more GPU memory.'};
}

// ── Per-model settings ──
const isSet = v => v !== undefined && v !== null && v !== '' && v !== 'default';
export const hasOverrides = params => Object.values(params || {}).some(isSet);
export const overrideCount = (modelParams, installed) => (installed || []).filter(name => hasOverrides(modelParams?.[name])).length;
// A deleted model's settings would otherwise be counted forever and come back if the name is reused.
export function withoutModel(modelParams, name) {
  if (!modelParams || !(name in modelParams)) return modelParams;
  const next = {...modelParams};
  delete next[name];
  return next;
}

export const STYLE_SLIDERS = [
  {key: 'temperature', label: 'Creativity (temperature)', min: 0, max: 2, step: 0.05, ends: ['Precise', 'Balanced', 'Creative'], help: 'Low values give steady, repeatable answers. High values give more varied ones.'},
  {key: 'top_p', label: 'Word choice range (top-p)', min: 0.1, max: 1, step: 0.05, ends: ['Focused', 'Balanced', 'Diverse'], help: 'How much of the less likely wording the model may pick from.'},
  {key: 'repeat_penalty', label: 'Repetition penalty', min: 1, max: 2, step: 0.05, ends: ['None', 'Normal', 'Strong'], help: 'Pushes the model away from repeating itself. 1.1 is the usual value.'},
];
export function sliderValue(key, params, defaults) {
  const raw = params?.[key];
  if (isSet(raw) && Number.isFinite(parseFloat(raw))) return {own: true, value: parseFloat(raw)};
  return {own: false, value: Number(defaults?.[key] ?? {temperature: 0.7, top_p: 0.9, repeat_penalty: 1.1}[key])};
}

// ── Tool-calling fix ──
// Only a model Ollama says cannot use tools is offered the fix; one whose abilities are unknown is too.
export function toolFix(name, facts) {
  const reported = Array.isArray(facts?.capabilities) ? facts.capabilities : null;
  if (reported && (reported.includes('tools') || reported.includes('embedding'))) return null;
  const b = String(name || '').toLowerCase();
  const family = /qwen|chatml/.test(b) ? 'chatml' : /llama|hermes/.test(b) ? 'llama3' : /mistral|mixtral/.test(b) ? 'mistral' : /gemma/.test(b) ? 'gemma' : 'chatml';
  return {family, known: !!reported};
}
export const TEMPLATE_FAMILIES = [['chatml', 'ChatML (Qwen and most others)'], ['llama3', 'Llama 3'], ['mistral', 'Mistral / Mixtral'], ['gemma', 'Gemma']];

// ── Recommendations (HyprFit) ──
const FIT = {
  great: ['Fits easily', 'ok'], fits: ['Fits on its own', 'ok'], tight: ['Tight fit', 'warn'],
  too_large: ['Too large for this server', 'bad'], unknown: ['Fit unknown', 'muted'],
};
export function fitView(rec, profile) {
  const [label, tone] = FIT[rec?.fit] || FIT.unknown;
  const need = Number(rec?.required_gb ?? rec?.estimated_vram_gb) || 0;
  const unified = !!profile?.unified_memory;
  const have = Number(unified ? profile?.system_ram_gb : profile?.total_vram_gb) || 0;
  const parts = [];
  if (need && have) parts.push(`Needs ${trim(need)} GB of your ${trim(have)} GB`);
  else if (need) parts.push(`Needs ${trim(need)} GB`);
  const speed = speedWords(rec?.estimated_speed);
  if (speed) parts.push(speed);
  const context = contextLabel(rec?.context_tokens);
  if (context) parts.push(`${context} context`);
  return {label, tone, percent: need && have ? Math.max(4, Math.min(100, Math.round((need / have) * 100))) : 0, over: !!(need && have && need > have), sentence: parts.join(' · ')};
}
const trim = n => String(Math.round(n * 10) / 10);
export function speedWords(text) {
  const raw = String(text || '').trim();
  if (!raw) return '';
  const m = raw.match(/~?\s*([\d.]+)\s*tok\/s/i);
  if (!m) return raw.replace(/^./, c => c.toUpperCase());
  const rest = raw.replace(m[0], '').replace(/^[\s,]+|[\s,]+$/g, '');
  return `about ${Math.round(parseFloat(m[1]))} tokens a second${rest ? `, ${rest.replace('RAM-spill risk', 'may spill into slower system memory')}` : ''}`;
}

// The hardware fields that only change these estimates. They do not configure Ollama or HyprChat.
export const ESTIMATE_FIELDS = [
  {key: 'max_loaded_models', label: 'Models kept loaded', min: 1, max: 32, help: 'How many models you expect to keep in memory together. A higher number leaves each one less room in the estimate. To actually limit this, use Settings → Model & Generation → "Models loaded at once".'},
  {key: 'num_parallel', label: 'Replies at the same time', min: 1, max: 16, help: 'Matches Ollama\'s OLLAMA_NUM_PARALLEL. Each extra slot needs its own context memory.'},
];
export const KV_CHOICES = [['q8_0', '8-bit (q8_0), the usual choice'], ['f16', '16-bit (f16), about twice the memory'], ['q4_0', '4-bit (q4_0), smallest']];

// ── Hugging Face ──
// A failed search answers with an error object, not a list. It used to look like "no results".
export function searchOutcome(payload, ok = true) {
  if (Array.isArray(payload)) return {results: payload, error: ''};
  return {results: [], error: (payload && (payload.detail || payload.error)) || (ok ? 'Hugging Face sent an answer that could not be read.' : 'Hugging Face could not be reached.')};
}
// A failed repository lookup must not be drawn as a file list.
export const repoFiles = info => (Array.isArray(info?.gguf_files) ? info.gguf_files : null);
export const repoError = info => (info && !Array.isArray(info.gguf_files) ? String(info.detail || info.error || 'The file list could not be loaded.') : '');

export const GLOSSARY = [
  ['Context window', 'How much text a model can keep in view at once: the chat so far plus its reply. Measured in tokens.'],
  ['Token', 'A piece of a word. 1,000 tokens is about 750 English words.'],
  ['Parameters', 'The size of the model\'s "brain". More usually means smarter and slower, and needs more memory.'],
  ['Compression (quantization)', 'Model files are stored with rounded numbers to save space. Q4_K_M means 4-bit; lower numbers are smaller and less exact.'],
  ['GGUF', 'The file format Ollama runs. Only GGUF files from Hugging Face can be installed here.'],
];

// The newest report from /api/models/facts, for code that is not handed it as a prop (the model picker).
export const factsStore = {current: {}};
