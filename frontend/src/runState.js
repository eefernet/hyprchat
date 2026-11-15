// A run that is still going on the server (deep research, an old-style workflow). The persistent Daedalus
// job has its own card and Stop, so its operation rows do not put the composer into "replying".
export const runIsLive = run => (run?.status === 'running' || run?.status === 'queued')
  && !String(run?.workflow_id || '').startsWith('cw3-');
