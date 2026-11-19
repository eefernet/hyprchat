// How the left bar groups chats: Pinned, then one section per workspace, then the date groups for chats
// that are in no workspace. Pure, so node:test can import it.
//
// A chat's workspaces come from `workspace_ids` (membership). `effective_workspace` is something else:
// the one workspace whose context the chat uses, which a chat in two workspaces does not have.
import {groupByRecency} from './convGroups.js';

export const SECTION_PREVIEW = 5;

const isPinned = c => c?.pinned === '1' || c?.pinned === 1;
const idsOf = c => (Array.isArray(c?.workspace_ids) ? c.workspace_ids : []);
const byNewest = getMs => (a, b) => (Number(getMs(b)) || 0) - (Number(getMs(a)) || 0);

// `convs`: the chats to show, already filtered by the search box and tag filter.
// `filtering`: a search or tag filter is on. Sections then show every match, ignore "collapsed" and
// disappear when they have none, so a search result is never hidden behind a closed folder.
export function sidebarSections(convs, workspaces, {collapsed = {}, expanded = {}, filtering = false, activeId = null, nowMs = Date.now(), getMs = () => NaN} = {}) {
  const list = convs || [];
  const pinned = list.filter(isPinned);
  const rest = list.filter(c => !isPinned(c));
  const known = new Set((workspaces || []).map(w => w.id));
  const sections = [];
  for (const ws of workspaces || []) {
    const mine = rest.filter(c => idsOf(c).includes(ws.id)).sort(byNewest(getMs));
    const pinnedHere = pinned.filter(c => idsOf(c).includes(ws.id)).length;
    // `conv_count` is the server's count of every chat in the workspace, loaded here or not.
    const total = filtering ? mine.length : Math.max(mine.length, (Number(ws.conv_count) || 0) - pinnedHere);
    if (filtering && !mine.length) continue;
    const all = filtering || !!expanded[ws.id];
    let shown = all ? mine : mine.slice(0, SECTION_PREVIEW);
    // The open chat is always visible in its section, even when it is older than the first five.
    if (!all && activeId && !shown.some(c => c.id === activeId)) {
      const active = mine.find(c => c.id === activeId);
      if (active) shown = [...shown, active];
    }
    sections.push({
      id: ws.id, name: ws.name || 'Workspace', open: filtering || !collapsed[ws.id],
      chats: shown, total, more: Math.max(0, total - shown.length),
      // All loaded chats are on screen but the server has more: "Show more" must fetch them.
      needsFetch: total > mine.length, expanded: !filtering && !!expanded[ws.id] && mine.length > SECTION_PREVIEW,
    });
  }
  const loose = rest.filter(c => !idsOf(c).some(id => known.has(id)));
  return {pinned, sections, groups: groupByRecency(loose, nowMs, getMs)};
}

export const moreLabel = n => `Show ${n} more`;

// A chat was linked to or unlinked from a workspace.
export function withMembership(conv, workspaceId, member) {
  const ids = idsOf(conv).filter(id => id !== workspaceId);
  return {...conv, workspace_ids: member ? [...ids, workspaceId] : ids};
}

const LISTED_FIELDS = ['workspace_ids', 'effective_workspace', 'workspace_context', 'updated_at', 'pinned'];

// Fresh list rows from the server join the rows on screen. Known chats take the fields that workspace
// changes can alter and keep everything else (messages, a title being typed); unknown chats are added
// after the known ones (they are older or were loaded for a workspace). Nothing is ever dropped: a
// reply that is streaming and local-only chats are in `prev`, not in `listed`.
export function mergeRows(prev, listed, normalize = row => row) {
  const fresh = new Map((listed || []).map(row => [row.id, row]));
  const known = new Set();
  const merged = (prev || []).map(conv => {
    known.add(conv.id);
    const row = fresh.get(conv.id);
    if (!row) return conv;
    const next = {...conv};
    for (const field of LISTED_FIELDS) if (field in row) next[field] = row[field];
    return next;
  });
  const added = (listed || []).filter(row => !known.has(row.id)).map(row => ({...normalize(row), messages: []}));
  return added.length ? [...merged, ...added] : merged;
}

// Rows the server did not send again still have to forget a deleted workspace and learn a new name.
export function withWorkspaces(convs, workspaces) {
  const names = new Map((workspaces || []).map(w => [w.id, w.name]));
  let changed = false;
  const next = (convs || []).map(conv => {
    const ids = idsOf(conv), kept = ids.filter(id => names.has(id));
    const effective = conv.effective_workspace;
    const gone = effective && !names.has(effective.id);
    const renamed = effective && !gone && names.get(effective.id) !== effective.name;
    if (kept.length === ids.length && !gone && !renamed) return conv;
    changed = true;
    return {...conv, workspace_ids: kept, effective_workspace: gone ? null : renamed ? {...effective, name: names.get(effective.id)} : effective};
  });
  return changed ? next : convs;
}

// What makes the chat rows stale when it changes: which workspaces exist, their names and chat counts.
export const workspaceSignature = workspaces => (workspaces || []).map(w => `${w.id}:${w.name}:${w.conv_count || 0}`).join('|');

// A message was sent in this chat: it is the newest activity, so it leads the list (new chats are put
// at the front the same way). Pinned chats are drawn from their own filter, so their place is unchanged.
export function touched(convs, id, iso) {
  const index = (convs || []).findIndex(c => c.id === id);
  if (index < 0) return convs;
  const conv = {...convs[index], updated_at: iso};
  return [conv, ...convs.filter((_, i) => i !== index)];
}

// How many saved chats to ask for when the rows are refreshed: what is on screen, within the route's limits.
export const refreshLimit = convs => Math.min(500, Math.max(50, (convs || []).filter(c => !/^(ghost|l)-/.test(String(c?.id || ''))).length));
