// Rules for showing a document, a drawing or a table from a chat reply in the side pane, next to the
// live HTML preview (htmlPreview.js). Pure, so node:test can import them.
import {isHtmlFence} from './htmlPreview.js';

const BY_LABEL = {markdown: 'markdown', md: 'markdown', mdx: 'markdown', svg: 'svg', csv: 'table', tsv: 'table'};
const BY_EXT = {md: 'markdown', markdown: 'markdown', mdx: 'markdown', svg: 'svg', csv: 'table', tsv: 'table', html: 'html', htm: 'html'};

const extension = name => { const match = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/); return match ? match[1] : ''; };

// What the pane can show for a fenced block: 'html' | 'markdown' | 'svg' | 'table' | null.
// The fence's own label wins; a block that only carries a file name (```text notes.md) goes by its extension.
export function previewKindFor(label = '', filename = '') {
  const tag = String(label || '').trim().toLowerCase();
  if (isHtmlFence(tag)) return 'html';
  if (BY_LABEL[tag]) return BY_LABEL[tag];
  if (!tag || ['text', 'txt', 'plaintext', 'none', 'markup', 'xml'].includes(tag)) return BY_EXT[extension(filename)] || null;
  return null;
}

// A Markdown block opens on its own only when it is clearly a document: the author named a file, or it
// is long and starts with a heading. A short ```markdown snippet showing syntax must not take the pane.
export function looksLikeDocument(code = '', filename = '') {
  const text = String(code || '');
  if (!text.trim()) return false;
  if (filename) return true;
  const lines = text.split('\n');
  const first = lines.find(line => line.trim()) || '';
  return lines.length >= 25 && /^#{1,3}\s+\S/.test(first.trim());
}

// Opens by itself: only documents. A drawing or a table keeps its Preview button.
export const opensByItself = (kind, code, filename) => kind === 'markdown' && looksLikeDocument(code, filename);

const FALLBACK_TITLE = {markdown: 'Document', svg: 'Drawing', table: 'Table'};
export function docTitle(kind, filename = '', code = '') {
  if (filename) return String(filename).split('/').pop();
  const text = String(code || '');
  if (kind === 'markdown') { const heading = text.match(/^\s{0,3}#{1,6}\s+(.{1,120}?)\s*#*\s*$/m); if (heading) return heading[1].trim(); }
  if (kind === 'svg') { const title = text.match(/<title[^>]*>([^<]{1,120})<\/title>/i); if (title) return title[1].trim(); }
  return FALLBACK_TITLE[kind] || 'Preview';
}

const DEFAULT_EXT = {html: 'html', markdown: 'md', svg: 'svg', table: 'csv'};
// The file name used for Download and Save: the author's name when there is one, else the title.
export function fileNameFor(kind, title = '', filename = '', code = '') {
  const given = String(filename || '').split('/').pop();
  if (given && /\.[A-Za-z0-9]+$/.test(given)) return given;
  const ext = kind === 'table' && delimiterOf('', '', code) === '\t' ? 'tsv' : (DEFAULT_EXT[kind] || 'txt');
  const stem = String(given || title || 'preview').replace(/\.[A-Za-z0-9]+$/, '').replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'preview';
  return `${stem}.${ext}`;
}

// A drawing is shown as an image, so nothing inside it can run. It is drawn once the closing tag is there.
export const svgComplete = code => /<svg[\s>]/i.test(String(code || '')) && /<\/svg>\s*$/i.test(String(code || '').trim());
export const svgDataUrl = code => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(String(code || ''))}`;

export function delimiterOf(label = '', filename = '', code = '') {
  if (String(label).toLowerCase() === 'tsv' || extension(filename) === 'tsv') return '\t';
  const first = String(code || '').split('\n', 1)[0] || '';
  return first.includes('\t') && !first.includes(',') ? '\t' : ',';
}

// Delimited text as rows. Handles quoted fields, doubled quotes and line breaks inside quotes; the first
// row is the header. `maxRows` bounds what is drawn: `total` says how many data rows there are.
export function parseDelimited(text, {delimiter = ',', maxRows = 500} = {}) {
  const source = String(text || '').replace(/\r\n?/g, '\n');
  const rows = [];
  let row = [], field = '', quoted = false, i = 0;
  const endField = () => { row.push(field); field = ''; };
  const endRow = () => { endField(); if (row.length > 1 || row[0] !== '') rows.push(row); row = []; };
  while (i < source.length) {
    const ch = source[i];
    if (quoted) {
      if (ch === '"') { if (source[i + 1] === '"') { field += '"'; i += 2; continue; } quoted = false; i += 1; continue; }
      field += ch; i += 1; continue;
    }
    if (ch === '"' && field === '') { quoted = true; i += 1; continue; }
    if (ch === delimiter) { endField(); i += 1; continue; }
    if (ch === '\n') { endRow(); i += 1; continue; }
    field += ch; i += 1;
  }
  if (field !== '' || row.length) endRow();
  const header = rows[0] || [];
  const body = rows.slice(1);
  const width = Math.max(header.length, ...body.slice(0, maxRows).map(r => r.length), 0);
  const pad = r => (r.length < width ? [...r, ...Array(width - r.length).fill('')] : r);
  return {header: pad(header), rows: body.slice(0, maxRows).map(pad), total: body.length, truncated: body.length > maxRows};
}

// Delivered files the pane opens by itself (file_ready). Archives and unknown types wait for a click.
const OPENS = new Set(['pdf', 'docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'md', 'markdown', 'html', 'htm', 'txt', 'csv', 'tsv', 'json']);
export const opensWhenDelivered = filename => OPENS.has(extension(filename));
// Shown through the artifact preview (Office renderer, table and JSON views) when the file has an artifact id.
const ARTIFACT_VIEW = new Set(['docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls', 'csv', 'tsv', 'json', 'pdf']);
export const usesArtifactView = filename => ARTIFACT_VIEW.has(extension(filename));

// Text files the pane fetches and shows itself. Anything else without an artifact has no preview here.
export const TEXT_PREVIEW_TYPES = ['md', 'markdown', 'mdx', 'txt', 'html', 'py', 'js', 'ts', 'json', 'css', 'sh', 'rs', 'go', 'java', 'c', 'cpp', 'yaml', 'toml', 'xml', 'csv', 'tsv', 'log', 'conf', 'ini', 'env', 'svg'];
// JSON for reading: indented when it parses, as written when it does not.
export function prettyJson(text) { try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return String(text ?? ''); } }
