// Recency buckets for the conversation sidebar. JSX-free so node:test can
// import it. Boundaries are LOCAL calendar days computed from nowMs: today =
// the same local day, yesterday = the day before, week = the 7 days before
// today, older = everything else (including unparsable times — getMs should
// return NaN or 0 for those, as datetime.parseUtcishMs does).
export const RECENCY_GROUPS = [['today', 'Today'], ['yesterday', 'Yesterday'], ['week', 'Previous 7 days'], ['older', 'Older']];

export function groupByRecency(items, nowMs, getMs) {
  const now = new Date(nowMs);
  const y = now.getFullYear(), m = now.getMonth(), d = now.getDate();
  const startToday = new Date(y, m, d).getTime();
  const startYesterday = new Date(y, m, d - 1).getTime();
  const startWeek = new Date(y, m, d - 7).getTime();
  const buckets = {today: [], yesterday: [], week: [], older: []};
  for (const it of items || []) {
    const ms = Number(getMs(it));
    let key = 'older';
    if (Number.isFinite(ms)) {
      if (ms >= startToday) key = 'today';
      else if (ms >= startYesterday) key = 'yesterday';
      else if (ms >= startWeek) key = 'week';
    }
    buckets[key].push(it);
  }
  return RECENCY_GROUPS.filter(([key]) => buckets[key].length).map(([key, label]) => ({key, label, items: buckets[key]}));
}
