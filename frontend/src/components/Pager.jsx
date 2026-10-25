import React from 'react';
import {IC} from './icons.jsx';

// Shared offset pager. Renders nothing on a single page. `count` is the number
// of rows on the current page (shows "1–30", "+ of total" when known);
// without it the range falls back to "Page N".
export default function Pager({page, pageSize, count, hasMore, total, busy = false, onPage, label = 'Pagination'}) {
  if (page === 0 && !hasMore) return null;
  const start = page * pageSize + 1;
  const range = count != null ? `${start}–${page * pageSize + count}${total != null ? ` of ${total}` : ''}` : `Page ${page + 1}`;
  return <nav className="hc-pager" aria-label={label}>
    <button type="button" className="hc-icon-btn" aria-label="Previous page" disabled={!page || busy} onClick={() => onPage(page - 1)}><IC.ChevLeft/></button>
    <span className="hc-pager-range">{range}</span>
    <button type="button" className="hc-icon-btn" aria-label="Next page" disabled={!hasMore || busy} onClick={() => onPage(page + 1)}><IC.ChevRight/></button>
  </nav>;
}
