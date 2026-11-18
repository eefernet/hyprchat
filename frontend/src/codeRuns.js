// Code runs in a reply: what each one is called, and how a row of them is drawn as one list.
//
// Seen live 2026-11-14: a reply that ran twelve scripts without writing anything between them was a
// wall of twelve identical rows that each said only "python output". A card now carries the model's own
// few words for the run and the code (backend/tooling/codebox_tools.py); consecutive runs are drawn as
// one box with a line per run (components/CodeRunGroup.jsx).

const oneLine = value => String(value || '').split(/\s+/).filter(Boolean).join(' ');
const clip = (text, size) => (text.length > size ? `${text.slice(0, size - 1).trimEnd()}…` : text);

// Lines that say nothing about what a script does.
const BOILERPLATE = /^(#!|import\s|from\s+\S+\s+import\b|set\s+[-+]|["']use strict["']|(const|let|var)\s+[\w{},\s]+=\s*require\(|package\s|using\s|#include\b|<\?php)/;
const COMMENT = /^(#+|\/\/+|--+|\/\*+|\*+)\s*/;

export function firstMeaningfulLine(code) {
  for (const raw of String(code || '').split('\n')) {
    const line = raw.trim();
    if (!line || BOILERPLATE.test(line)) continue;
    const text = line.replace(COMMENT, '').replace(/\*\/\s*$/, '').trim();
    if (text) return oneLine(text);
  }
  return '';
}

// What a run is called: the model's description, else the first line of the code that says something,
// else the language.
export function runLabel(card) {
  const data = card?.data || {};
  const described = oneLine(data.description);
  if (described) return clip(described, 90);
  const fromCode = firstMeaningfulLine(data.code);
  return fromCode ? clip(fromCode, 64) : `${data.language || 'code'} run`;
}

export const runKey = card => `run:${card?.data?.tool_call_id || `n${card?.seq ?? 0}`}`;

const lastLine = text => String(text || '').split('\n').map(line => line.trim()).filter(Boolean).pop() || '';

// The line a failed run shows without being opened: errors end with the line that names them.
export function errorLine(card) {
  const data = card?.data || {};
  return data.success ? '' : clip(lastLine(data.stderr) || lastLine(data.stdout) || 'The run failed.', 200);
}

export function runSummary(runs) {
  const list = runs || [];
  const failed = list.filter(run => !run.data?.success).length;
  const seconds = list.reduce((sum, run) => sum + (Number.parseFloat(run.data?.exec_time) || 0), 0);
  return { count: list.length, failed, passed: list.length - failed, seconds };
}

export function summaryText(summary) {
  const result = !summary.failed ? 'all passed' : !summary.passed ? 'all failed' : `${summary.passed} passed, ${summary.failed} failed`;
  return `${result} · ${summary.seconds.toFixed(1)}s`;
}

// The cards of one part of a reply, with consecutive code runs gathered. A file card stays where it was.
export function groupCards(cards) {
  const items = [];
  for (const card of cards || []) {
    const last = items[items.length - 1];
    if (card.type === 'file_ready') items.push({ type: 'file', card });
    else if (last?.type === 'runs') last.runs.push(card);
    else items.push({ type: 'runs', runs: [card] });
  }
  return items;
}

// Which runs of a long list are shown before "Show N more": the newest (the ones the text after the list
// is about, and the one that just finished while a reply is running) and every failed one.
export function foldRuns(runs, { limit = 5, showAll = false } = {}) {
  const list = runs || [];
  const all = list.map((run, index) => ({ run, index }));
  if (showAll || list.length <= limit + 1) return { rows: all, hidden: 0 };
  const from = list.length - limit;
  const rows = all.filter(({ run, index }) => index >= from || !run.data?.success);
  return { rows, hidden: list.length - rows.length };
}

const detailOf = event => {
  const value = event?.data?.detail;
  if (typeof value !== 'string') return value || null;
  try { return JSON.parse(value); } catch { return null; }
};
const isRunEnd = event => event?.type === 'tool_end' && event.data?.tool === 'execute_code';

// Replies saved before cards carried their code: the "end" event sent just before each output card has
// it. Returns the same array when nothing was added.
export function withRunCode(events) {
  const list = events || [];
  let pending = null, changed = false;
  const out = list.map(event => {
    if (isRunEnd(event)) { pending = detailOf(event); return event; }
    if (event?.type !== 'code_output') return event;
    const code = pending?.code;
    pending = null;
    if (event.data?.code || !code) return event;
    changed = true;
    return { ...event, data: { ...event.data, code } };
  });
  return changed ? out : list;
}

// What is saved with a reply. The "end" event of a run repeats the output its card already holds; with
// forty rounds allowed that doubled the size of a long reply's saved events.
export function slimSaved(events) {
  const list = events || [];
  const carded = new Set(list.filter(event => event?.type === 'code_output' && event.data?.tool_call_id && event.data?.code != null)
    .map(event => event.data.tool_call_id));
  if (!carded.size) return list;
  return list.map(event => {
    if (!isRunEnd(event) || !carded.has(event.data.tool_call_id)) return event;
    const detail = detailOf(event);
    if (!detail || (detail.stdout == null && detail.stderr == null)) return event;
    const { stdout, stderr, ...rest } = detail;
    return { ...event, data: { ...event.data, detail: JSON.stringify(rest) } };
  });
}
