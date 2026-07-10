// Shared Jarvis timestamp formatters. The backend sends two kinds of strings
// and the distinction matters — don't mix them up:
//   naive-UTC ISO (task next_run/last_run, run started_at, notification
//   created_at)                       → fmtUtcMinute (labelled " UTC")
//   assistant-timezone `*_local` ISO (notes/calendar) and email Date headers
//                                     → fmtLocalMinute (no suffix)
export const fmtUtcMinute = s => (s ? String(s).replace('T', ' ').slice(0, 16) + ' UTC' : '');
export const fmtLocalMinute = s => (s ? String(s).replace('T', ' ').slice(0, 16) : '');
