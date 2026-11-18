// Which events belong to one tool call.
//
// Since 2026-11-15 the server puts a `tool_call_id` on every event of a call (backend/tool_calls.py).
// Replies saved before that have none, and one code run left three or more "start" events (the chat
// loop's, the sandbox's, one every 3 s while it ran), each counted as a tool: "26 tools" for twelve code
// runs and a search. This gives those older events the id they would have had.

// Tools the chat loop may run several of at once: without ids their events cannot be told apart by
// order, so they are left as they are.
const PARALLEL = new Set(['read_file', 'list_files', 'search_files', 'diff_files', 'git_diff', 'research', 'fetch_url', 'write_file']);
const NOT_A_CALL = new Set(['processing', 'generating', 'refinement']);
const TOOL_EVENTS = new Set(['tool_start', 'tool_progress', 'tool_status', 'tool_end', 'tool_done', 'tool_error']);
const TERMINAL = new Set(['tool_end', 'tool_done', 'tool_error']);

export const callId = data => data?.tool_call_id || data?.call_id || data?.operation_id || data?.run_id || null;

// Returns the same array when nothing needs an id.
export function withLegacyIds(events) {
  const list = events || [];
  const identified = new Set();
  for (const event of list) if (TOOL_EVENTS.has(event?.type) && callId(event.data)) identified.add(event.data.tool);
  const open = new Map();
  let counter = 0, lastRun = null, changed = false;
  const out = list.map(event => {
    const data = event?.data, tool = data?.tool;
    if (event?.type === 'code_output') {
      const id = lastRun;
      lastRun = null;
      if (!id || callId(data)) return event;
      changed = true;
      return { ...event, data: { ...data, tool_call_id: id } };
    }
    // A tool that has ids on some events is from the newer server: its id-less events are handled by
    // the Activity list's own matching.
    if (!TOOL_EVENTS.has(event?.type) || !tool || callId(data) || NOT_A_CALL.has(tool) || PARALLEL.has(tool) || identified.has(tool)) return event;
    let id = open.get(tool);
    if (!id) {
      if (event.type !== 'tool_start') return event;
      id = `legacy-${++counter}`;
      open.set(tool, id);
    }
    if (TERMINAL.has(event.type)) {
      open.delete(tool);
      if (tool === 'execute_code') lastRun = id;
    }
    changed = true;
    return { ...event, data: { ...data, tool_call_id: id } };
  });
  return changed ? out : list;
}
