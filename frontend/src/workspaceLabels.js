// Human labels + tones for the status vocabularies the workspace screens show.
// JSX-free so node:test can import it. Values mirror what the backend emits:
// source kinds and readiness from backend/workspace_service.py (+ the
// indexing_jobs states that flow through it), job status/kind from
// backend/job_registry.py and panels/JobsPanel.jsx, report status from the
// research_reports rows. A tone is one of TONES and maps onto the theme's
// t.ok / t.warm / t.err / t.mut / t.acc colours at the render site.
export const TONES = ['ok', 'warm', 'err', 'mut', 'acc'];

export const SOURCE_KIND = {chat: 'Chat', note: 'Note', research: 'Report', artifact: 'Artifact', knowledge: 'Knowledge base', document: 'Document'};

export const READINESS = {
  ready: ['Ready', 'ok'],
  succeeded: ['Ready', 'ok'],
  done: ['Ready', 'ok'],
  completed: ['Ready', 'ok'],
  complete: ['Ready', 'ok'],
  partial: ['Partial', 'warm'],
  indexing: ['Indexing', 'acc'],
  queued: ['Queued', 'warm'],
  running: ['Indexing', 'acc'],
  'needs attention': ['Needs attention', 'err'],
  error: ['Needs attention', 'err'],
  failed: ['Failed', 'err'],
  interrupted: ['Interrupted', 'err'],
  cancelled: ['Cancelled', 'mut'],
  unindexed: ['Not indexed', 'mut'],
  unknown: ['Not indexed', 'mut'],
  empty: ['Empty', 'mut'],
  'unsupported — original available': ['Unsupported', 'mut'],
};

export const JOB_STATUS = {
  active: ['Active', 'acc'],
  queued: ['Queued', 'warm'],
  running: ['Running', 'acc'],
  cancelling: ['Cancelling', 'warm'],
  succeeded: ['Succeeded', 'ok'],
  partial: ['Partial', 'warm'],
  failed: ['Failed', 'err'],
  cancelled: ['Cancelled', 'mut'],
  interrupted: ['Interrupted', 'err'],
};

export const JOB_KIND = {research: 'Research', coding: 'Coding', coding_run: 'Coding run', document: 'Document', image: 'Image', indexing: 'Indexing', scheduled: 'Scheduled'};

export const REPORT_STATUS = {
  complete: ['Complete', 'ok'],
  running: ['Running', 'acc'],
  queued: ['Queued', 'warm'],
  failed: ['Failed', 'err'],
  cancelled: ['Cancelled', 'mut'],
};

const MAPS = {
  'source-kind': SOURCE_KIND,
  readiness: READINESS,
  'job-status': JOB_STATUS,
  'job-kind': JOB_KIND,
  'report-status': REPORT_STATUS,
  'job-phase': null, // free-form backend phase names: humanized only
};

// "needs_attention" / "coding-run" → "Needs attention" / "Coding run".
export function humanize(s) {
  const text = String(s ?? '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : '';
}

// → {label, tone}. Unknown values humanize with the muted tone; null/undefined
// give an empty label so callers can render nothing.
export function statusInfo(kind, value) {
  if (value == null) return {label: '', tone: 'mut'};
  const key = String(value).trim().toLowerCase();
  const hit = MAPS[kind]?.[key];
  if (hit == null) return {label: humanize(key), tone: 'mut'};
  return typeof hit === 'string' ? {label: hit, tone: 'mut'} : {label: hit[0], tone: hit[1]};
}
