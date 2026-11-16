// A run that is still going on the server (deep research, an old-style workflow). The persistent Daedalus
// job has its own card and Stop, so its operation rows do not put the composer into "replying".
export const runIsLive = run => (run?.status === 'running' || run?.status === 'queued')
  && !String(run?.workflow_id || '').startsWith('cw3-');

// How a reply's stream ended, as the browser saw it.
//   'stopped'      the user pressed Stop
//   'failed'       the server reported an error
//   'interrupted'  the connection closed before the server said the reply was done (a restart, a lost
//                  network): this used to be recorded as a finished reply, with no Continue
//   'rounds'       finished at the tool-round limit        'cutoff'  finished at the output limit
//   'job'          finished, and handed work to a background job
//   'completed'
export function replyOutcome({ stopped, failed, ended, roundLimit, truncated, startedJob } = {}) {
  if (stopped) return 'stopped';
  if (failed) return 'failed';
  if (!ended) return 'interrupted';
  if (roundLimit) return 'rounds';
  if (truncated) return 'cutoff';
  return startedJob ? 'job' : 'completed';
}

// The reply's saved status, and the one its activity line shows, for an outcome.
export const deliveryStatus = outcome => (outcome === 'stopped' || outcome === 'interrupted' ? 'interrupted' : outcome === 'failed' ? 'failed' : 'completed');
export const activityStatus = outcome => (['stopped', 'failed', 'interrupted'].includes(outcome) ? outcome : 'completed');
