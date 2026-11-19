// A short "how long ago" for a chat row: "now", "12m", "3h", then the weekday within a week ("Tue"),
// then the day and month ("3 Nov"), with the year once it is a different one ("3 Nov 2025").
// Pure: both times are milliseconds, and the weekday / date are in the browser's own time zone.
const MINUTE = 60e3, HOUR = 3600e3, DAY = 86400e3;
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function shortAgo(thenMs, nowMs = Date.now()) {
  const then = Number(thenMs);
  if (!Number.isFinite(then) || then <= 0) return '';
  const age = nowMs - then;
  if (age < MINUTE) return 'now';                      // also a clock that is a little ahead
  if (age < HOUR) return `${Math.floor(age / MINUTE)}m`;
  if (age < DAY) return `${Math.floor(age / HOUR)}h`;
  const date = new Date(then);
  if (age < 7 * DAY) return WEEKDAYS[date.getDay()];
  const day = `${date.getDate()} ${MONTHS[date.getMonth()]}`;
  return date.getFullYear() === new Date(nowMs).getFullYear() ? day : `${day} ${date.getFullYear()}`;
}

// The full date and time for the row's tooltip.
export function fullWhen(thenMs) {
  const then = Number(thenMs);
  if (!Number.isFinite(then) || then <= 0) return '';
  try { return new Date(then).toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit'}); }
  catch { return ''; }
}
