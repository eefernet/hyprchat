// Rules of the Service Monitor view (components/ServiceMonitor.jsx). Pure, so node:test can import it.

export const MONITOR_DAYS = 90;

const dayKey = date => date.toISOString().slice(0, 10);

// The server sends only the days it has checks for. The strip always shows the last `count` days, oldest
// first, so a day without checks is drawn as "no data" instead of shifting the others. Days are UTC
// dates, like the `day` strings the server sends.
export function paddedDays(days, now = new Date(), count = MONITOR_DAYS) {
  const byDay = new Map((days || []).map(day => [day.day, day]));
  const y = now.getUTCFullYear(), m = now.getUTCMonth(), d = now.getUTCDate();
  const out = [];
  for (let back = count - 1; back >= 0; back--) {
    const key = dayKey(new Date(Date.UTC(y, m, d - back)));
    out.push(byDay.get(key) || {day: key, ok_pct: 0, degraded_pct: 0, error_pct: 0, total_checks: 0, avg_ms: 0});
  }
  return out;
}

// How one day's bar is coloured: 'none' (no checks), 'down' (down more than half the day),
// 'some-down', 'degraded' (degraded more than 30 %), 'some-degraded', or 'ok'.
export function barTone(day) {
  if (!day || !(Number(day.total_checks) > 0)) return 'none';
  if (day.error_pct > 50) return 'down';
  if (day.error_pct > 0) return 'some-down';
  if (day.degraded_pct > 30) return 'degraded';
  if (day.degraded_pct > 0) return 'some-degraded';
  return 'ok';
}

export function barTitle(day) {
  if (barTone(day) === 'none') return `${day.day}\nNo data`;
  return `${day.day}\n${day.ok_pct}% ok, ${day.degraded_pct}% degraded, ${day.error_pct}% down\n${day.total_checks} checks, avg ${day.avg_ms}ms`;
}

// A service's state now. A rate-limited search service still answers, so it is not "down".
export function serviceState(status, rateLimited = false) {
  if (rateLimited) return {tone: 'degraded', label: 'Rate limited'};
  if (status === 'ok') return {tone: 'ok', label: 'Operational'};
  if (status === 'degraded') return {tone: 'degraded', label: 'Degraded'};
  return {tone: 'down', label: 'Down'};
}
