// Pure helpers for the live HTML preview of fenced code in chat replies.
// The page runs in a sandboxed iframe (allow-scripts, never allow-same-origin);
// the CSP meta bounds what it may load. Both policies forbid network calls,
// form posts and nested frames; "cdn" additionally lets https: scripts/styles
// load so library demos render, "strict" allows inline code only.
export const CSP_POLICIES={
  cdn:"default-src 'none'; script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; img-src data: blob: https:; font-src data: https:; media-src data: blob: https:; connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'",
  strict:"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'",
};
export const PREVIEW_WIDTHS=[{id:'full',label:'Fill',width:0},{id:'tablet',label:'768',width:768},{id:'phone',label:'390',width:390}];

export function isHtmlFence(lang=''){
  return ['html','htm','xhtml'].includes(String(lang||'').trim().toLowerCase());
}

// A whole page, not a snippet: only these open the pane on their own.
export function isFullHtmlDocument(code=''){
  return /^﻿?\s*(?:<!doctype\s+html|<html[\s>])/i.test(String(code||''));
}

export function withPreviewCsp(html='',mode='cdn'){
  const policy=CSP_POLICIES[mode]||CSP_POLICIES.cdn;
  const meta=`<meta http-equiv="Content-Security-Policy" content="${policy}">`;
  const source=String(html||'');
  if(/<head[^>]*>/i.test(source))return source.replace(/<head[^>]*>/i,match=>`${match}${meta}`);
  if(/<html[^>]*>/i.test(source))return source.replace(/<html[^>]*>/i,match=>`${match}<head>${meta}</head>`);
  return `<!doctype html><html><head>${meta}</head><body>${source}</body></html>`;
}

export function previewTitle(filename='',code=''){
  if(filename)return String(filename).split('/').pop();
  const title=String(code||'').match(/<title[^>]*>([^<]{1,120})<\/title>/i);
  return title?title[1].trim():'Preview';
}
