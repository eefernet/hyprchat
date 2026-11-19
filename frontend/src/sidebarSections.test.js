import test from 'node:test';
import assert from 'node:assert/strict';
import {sidebarSections, withMembership, mergeRows, withWorkspaces, workspaceSignature, touched, refreshLimit, moreLabel, SECTION_PREVIEW} from './sidebarSections.js';

const NOW = new Date(2026, 10, 18, 12).getTime();
const at = hoursAgo => NOW - hoursAgo * 3600e3;
const getMs = c => c.ms;
const chat = (id, hoursAgo, workspace_ids = [], extra = {}) => ({id, ms: at(hoursAgo), workspace_ids, ...extra});
const WS = [{id: 'a', name: 'Home lab', conv_count: 8}, {id: 'b', name: 'Hyprchat', conv_count: 1}];

test('a workspace chat is listed under its workspace and not in the date groups', () => {
  const convs = [chat('loose', 1), chat('c1', 2, ['a']), chat('c2', 30, ['b'])];
  const view = sidebarSections(convs, WS, {nowMs: NOW, getMs});
  assert.deepEqual(view.sections.map(s => [s.name, s.chats.map(c => c.id)]), [['Home lab', ['c1']], ['Hyprchat', ['c2']]]);
  assert.deepEqual(view.groups.map(g => [g.key, g.items.map(c => c.id)]), [['today', ['loose']]]);
});

test('pinned chats stay in Pinned, also when they are in a workspace', () => {
  const view = sidebarSections([chat('p', 1, ['a'], {pinned: '1'}), chat('c1', 2, ['a'])], WS, {nowMs: NOW, getMs});
  assert.deepEqual(view.pinned.map(c => c.id), ['p']);
  assert.deepEqual(view.sections[0].chats.map(c => c.id), ['c1']);
  assert.equal(view.sections[0].total, 7);          // the server's 8 minus the pinned one
});

test('a section shows its five newest chats and offers the rest', () => {
  const convs = Array.from({length: 7}, (_, i) => chat(`c${i}`, 7 - i, ['a']));   // c6 is newest
  const view = sidebarSections(convs, WS, {nowMs: NOW, getMs}).sections[0];
  assert.deepEqual(view.chats.map(c => c.id), ['c6', 'c5', 'c4', 'c3', 'c2']);
  assert.equal(view.chats.length, SECTION_PREVIEW);
  assert.equal(view.more, 3);                       // 8 on the server, 5 shown
  assert.equal(view.needsFetch, true);              // one of them is not loaded yet
  assert.equal(moreLabel(view.more), 'Show 3 more');
  const open = sidebarSections(convs, WS, {nowMs: NOW, getMs, expanded: {a: true}}).sections[0];
  assert.equal(open.chats.length, 7);
  assert.equal(open.expanded, true);
});

test('the open chat is visible even when it is older than the first five', () => {
  const convs = Array.from({length: 7}, (_, i) => chat(`c${i}`, 7 - i, ['a']));
  const view = sidebarSections(convs, WS, {nowMs: NOW, getMs, activeId: 'c0'}).sections[0];
  assert.deepEqual(view.chats.map(c => c.id), ['c6', 'c5', 'c4', 'c3', 'c2', 'c0']);
});

test('a workspace none of whose chats are loaded still shows its header and count', () => {
  const view = sidebarSections([chat('loose', 1)], WS, {nowMs: NOW, getMs}).sections;
  assert.deepEqual(view.map(s => [s.name, s.chats.length, s.total, s.needsFetch]), [['Home lab', 0, 8, true], ['Hyprchat', 0, 1, true]]);
});

test('a chat in two workspaces is listed under each', () => {
  const view = sidebarSections([chat('both', 1, ['a', 'b'])], WS, {nowMs: NOW, getMs});
  assert.deepEqual(view.sections.map(s => s.chats.map(c => c.id)), [['both'], ['both']]);
  assert.deepEqual(view.groups, []);
});

test('a closed section stays closed, except while searching', () => {
  const convs = [chat('c1', 2, ['a'])];
  assert.equal(sidebarSections(convs, WS, {nowMs: NOW, getMs, collapsed: {a: true}}).sections[0].open, false);
  const searching = sidebarSections(convs, WS, {nowMs: NOW, getMs, collapsed: {a: true}, filtering: true});
  assert.deepEqual(searching.sections.map(s => [s.id, s.open, s.more]), [['a', true, 0]]);   // no empty sections, no "more"
});

test('a chat whose workspace no longer exists falls back to the date groups', () => {
  const view = sidebarSections([chat('orphan', 1, ['gone'])], WS, {nowMs: NOW, getMs});
  assert.deepEqual(view.groups[0].items.map(c => c.id), ['orphan']);
});

test('linking and unlinking a chat changes its membership only', () => {
  const conv = chat('c', 1, ['a']);
  assert.deepEqual(withMembership(conv, 'b', true).workspace_ids, ['a', 'b']);
  assert.deepEqual(withMembership(conv, 'a', true).workspace_ids, ['a']);
  assert.deepEqual(withMembership(conv, 'a', false).workspace_ids, []);
  assert.deepEqual(conv.workspace_ids, ['a']);
});

test('fresh rows update workspace fields, keep messages and drop nothing', () => {
  const prev = [{id: 'l-local', messages: [1]}, {id: 'c1', title: 'Typed title', messages: [1, 2], workspace_ids: []}, {id: 'older', messages: []}];
  const listed = [{id: 'c1', title: 'Server title', workspace_ids: ['a'], effective_workspace: {id: 'a', name: 'Home lab'}, updated_at: 'now'}, {id: 'new', title: 'New'}];
  const merged = mergeRows(prev, listed, row => ({...row, normalized: true}));
  assert.deepEqual(merged.map(c => c.id), ['l-local', 'c1', 'older', 'new']);
  assert.deepEqual(merged[1], {id: 'c1', title: 'Typed title', messages: [1, 2], workspace_ids: ['a'], effective_workspace: {id: 'a', name: 'Home lab'}, updated_at: 'now'});
  assert.deepEqual(merged[3], {id: 'new', title: 'New', normalized: true, messages: []});
});

test('rows forget a deleted workspace and learn a renamed one', () => {
  const convs = [
    {id: 'c1', workspace_ids: ['a', 'gone'], effective_workspace: {id: 'gone', name: 'Old'}},
    {id: 'c2', workspace_ids: ['a'], effective_workspace: {id: 'a', name: 'Old name'}},
    {id: 'c3', workspace_ids: []},
  ];
  const next = withWorkspaces(convs, WS);
  assert.deepEqual(next[0], {id: 'c1', workspace_ids: ['a'], effective_workspace: null});
  assert.equal(next[1].effective_workspace.name, 'Home lab');
  assert.equal(next[2], convs[2]);
  assert.equal(withWorkspaces(next, WS), next);     // nothing to change: the same list, so no re-render
});

test('the signature changes with a rename, a count or a deleted workspace', () => {
  const base = workspaceSignature(WS);
  assert.notEqual(workspaceSignature([{...WS[0], name: 'Renamed'}, WS[1]]), base);
  assert.notEqual(workspaceSignature([{...WS[0], conv_count: 7}, WS[1]]), base);
  assert.notEqual(workspaceSignature([WS[0]]), base);
  assert.equal(workspaceSignature(WS.map(w => ({...w, description: 'x'}))), base);
});

test('a chat you send in becomes the newest', () => {
  const convs = [{id: 'a', updated_at: '1'}, {id: 'b', updated_at: '0'}, {id: 'c', updated_at: '0'}];
  const next = touched(convs, 'c', 'now');
  assert.deepEqual(next.map(c => [c.id, c.updated_at]), [['c', 'now'], ['a', '1'], ['b', '0']]);
  assert.equal(touched(convs, 'missing', 'now'), convs);
});

test('a refresh asks for the chats on screen, within the route limits', () => {
  assert.equal(refreshLimit([]), 50);
  assert.equal(refreshLimit(Array.from({length: 120}, (_, i) => ({id: `c${i}`}))), 120);
  assert.equal(refreshLimit(Array.from({length: 900}, (_, i) => ({id: `c${i}`}))), 500);
  assert.equal(refreshLimit([{id: 'ghost-1'}, {id: 'l-2'}]), 50);
});
