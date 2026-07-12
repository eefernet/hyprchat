// Shared Jarvis timestamp formatters. The backend sends two kinds of strings
// and the distinction matters — don't mix them up:
//   naive-UTC ISO (task next_run/last_run, run started_at, notification
//   created_at)                       → fmtUtcMinute (labelled " UTC")
//   assistant-timezone `*_local` ISO (notes/calendar) and email Date headers
//                                     → fmtLocalMinute (no suffix)
export const fmtUtcMinute = s => (s ? String(s).replace('T', ' ').slice(0, 16) + ' UTC' : '');
export const fmtLocalMinute = s => (s ? String(s).replace('T', ' ').slice(0, 16) : '');
// naive-UTC ISO → the BROWSER's local time (next_run/last_run, sync stamps,
// notification created_at). Falls back to the labelled-UTC form on bad input.
export const fmtUtcToLocal = s => {
  if (!s) return '';
  const d = new Date(String(s).replace(' ', 'T').slice(0, 16) + ':00Z');
  if (isNaN(d.getTime())) return fmtUtcMinute(s);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
