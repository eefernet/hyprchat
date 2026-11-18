// File rows of a knowledge base as the panel holds them.
//
// The server keeps one row per file name in a knowledge base: a second upload under the same name replaces
// the first (2026-11-18; it used to leave two rows pointing at one file). The panel mirrors that.

// The list with `file` added, or put in place of the row it replaces (same id, else same name).
export function withFile(files, file) {
  const list = files || [];
  const name = file.filename || file.name;
  const at = list.findIndex(f => (file.id != null && f.id === file.id) || (name && (f.filename || f.name) === name));
  if (at === -1) return [...list, file];
  return list.map((f, i) => (i === at ? { ...f, ...file } : f));
}

// The list without one row. Rows are removed by id; a row that has none (never saved) goes by name.
export function withoutFile(files, file) {
  return (files || []).filter(f => (file.id != null ? f.id !== file.id : (f.filename || f.name) !== (file.filename || file.name)));
}

// What a failed request should tell the user: the server's reason when it gave one.
export async function failureDetail(response) {
  try { const body = await response.json(); if (body?.detail) return String(body.detail); } catch { /* no JSON body */ }
  return `HTTP ${response.status}`;
}
