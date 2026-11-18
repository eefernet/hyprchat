// Download links a reply writes for files it delivered.
//
// A delivered file has the address /api/downloads/<8 hex>-<name> (backend/output_paths.py) and its own
// file card. Found 2026-11-17: the reply's own "[Download name](address)" line was drawn as a second card,
// labelled with the address ("f8725496-sourdough-weekend.docx").

const PREFIX = /^[0-9a-f]{8}-(?=.)/;
const DOWNLOAD = '/api/downloads/';

function lastSegment(url) {
  const path = String(url || '').split(/[?#]/)[0];
  const name = path.slice(path.lastIndexOf('/') + 1);
  try { return decodeURIComponent(name); } catch { return name; }
}

// The file name without the address prefix.
export function plainDownloadName(url) {
  return lastSegment(url).replace(PREFIX, '');
}

// What a download link is called: the link's own text when it names the file, else the plain file name.
export function downloadLabel(linkText, url) {
  const text = String(linkText || '').replace(/^\s*(?:📎|📁)\s*/u, '').replace(/^Download\s+/i, '').trim();
  const segment = lastSegment(url);
  if (text && text !== segment && !/^(?:download|here|link|file)$/i.test(text)) return text;
  return plainDownloadName(url);
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// The reply text without its links to files that already have a card. `urls` are the cards' addresses.
// A line that held nothing but the link goes with it; a link inside a sentence leaves its text behind.
export function withoutLinkedFiles(text, urls) {
  let out = String(text || '');
  const wanted = [...new Set((urls || []).filter(url => String(url || '').startsWith(DOWNLOAD)))];
  if (!wanted.length || !out.includes(DOWNLOAD)) return out;
  for (const url of wanted) {
    const address = escapeRegExp(url);
    const link = `\\*{0,2}\\[([^\\]\\n]*)\\]\\(${address}\\)\\*{0,2}`;
    // A line of its own (optionally a list bullet): remove the whole line.
    out = out.replace(new RegExp(`(^|\\n)[ \\t]*(?:[-*+] +)?(?:${link}|${address})[ \\t]*(?=\\n|$)`, 'g'), '$1');
    // Inside other text: keep the words, drop the address.
    out = out.replace(new RegExp(link, 'g'), (_m, label) => label.replace(/^Download\s+/i, ''));
  }
  return out.replace(/\n{3,}/g, '\n\n');
}
