// Which Daedalus job cards and job events belong to a reply.
//
// A job belongs to the reply that started it (`origin_message_id` on the job row). The chat's event stream
// also carries events of jobs that are running in the background: when one delivers, its `file_ready`
// event (with the job's `workflow_id`) arrives while some LATER reply is the newest one. That reply used
// to pick the job up from the event: it showed a copy of the job's card and, because a message with a
// job hides its own tool list, lost its own code output and download cards (found 2026-11-13). The same
// events were saved into that reply's metadata, so the copy survived a reload.

const sameId = (a, b) => a != null && b != null && String(a) === String(b);
const origin = job => (job && job.origin_message_id != null && job.origin_message_id !== '' ? job.origin_message_id : null);

// True unless the job is known to have started under another message. An unknown job (just created, the
// list not refreshed yet) and a job with no recorded origin are given the benefit of the doubt, and so is
// a reply whose own id is not known yet.
export function ownsJob(job, messageId) {
  const started = origin(job);
  return started == null || messageId == null || sameId(started, messageId);
}

const byId = workflows => new Map((workflows || []).map(job => [job.id, job]));

// Events of a job that belongs to another message. Returns the same array when there are none, so
// memoized children see no change.
export function ownEvents(events, workflows, messageId) {
  const list = events || [];
  if (messageId == null || !list.length || !(workflows || []).length) return list;
  const known = byId(workflows);
  const foreign = event => {
    const id = event?.data?.workflow_id;
    return !!id && known.has(id) && !ownsJob(known.get(id), messageId);
  };
  return list.some(foreign) ? list.filter(event => !foreign(event)) : list;
}

// Ids of the jobs a reply shows a card for: saved with the reply, seen on the live stream while it is the
// newest reply, or started by it according to the job list. Never a job that started elsewhere.
export function messageJobIds({ messageId = null, metadata = {}, liveEvents = [], workflows = [] } = {}) {
  const known = byId(workflows);
  const seen = [
    ...(metadata.workflow_ids || []),
    ...(metadata.saved_events || []).concat(liveEvents || []).map(event => event?.data?.workflow_id),
  ].filter(Boolean);
  const started = (workflows || []).filter(job => sameId(origin(job), messageId)).map(job => job.id);
  return [...new Set([...seen, ...started])].filter(id => ownsJob(known.get(id), messageId));
}

// A job the list does not hold yet. Not "queued": that showed "Planning" with a Stop button on every old
// chat until the list loaded, and counted as a running job for the workbench's auto-open.
export const loadingJob = id => ({ id, workflow_version: 3, state: 'loading' });

// A reply is drawn as Daedalus output (a card instead of the tool list, the text hidden for old-style
// runs) only when there is a card to draw. A refused tool call, such as a project question asked while a
// job is running, leaves tool events and nothing else: the reply must stay readable.
export function hasJobCard({ workflows = [], runIds = [] } = {}) {
  return workflows.length > 0 || runIds.length > 0;
}

// The message a reply answers owns the job too: editing it would cut the job's card out of the chat
// while the job keeps running. `index` is a user or assistant row of `messages`.
export function startsJob(messages, index, workflows) {
  const list = messages || [], row = list[index];
  if (!row) return false;
  const known = byId(workflows);
  // Only a persistent job (version 3) keeps running on its own; an unknown id is one the list has not loaded.
  const owns = message => message?.role === 'assistant' && messageJobIds({
    messageId: message.id ?? null, metadata: metadataOf(message), workflows,
  }).some(id => (known.get(id)?.workflow_version ?? 3) === 3);
  if (row.role !== 'user') return owns(row);
  for (let next = index + 1; next < list.length && list[next].role !== 'user'; next += 1) {
    if (owns(list[next])) return true;
  }
  return false;
}

function metadataOf(message) {
  const value = message?.metadata;
  if (!value) return {};
  if (typeof value === 'string') { try { return JSON.parse(value) || {}; } catch { return {}; } }
  return value;
}
