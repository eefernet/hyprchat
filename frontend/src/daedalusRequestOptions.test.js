import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as jobHelpers from './daedalusJobs.js';
const {finishedJobStates} = jobHelpers;

// Run the component's own helpers and choice hook (plain JS inside the .jsx) under a minimal hook runtime,
// without a DOM framework, the same way chatSend.test.js exercises main.jsx handlers.
const source = fs.readFileSync(new URL('./components/DaedalusRequestOptions.jsx', import.meta.url), 'utf8');
function extract(name) {
  const start = source.indexOf(`\nfunction ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  return source.slice(start, source.indexOf('\n}\n', start) + 3);
}
const changed = (before, after) => !before || before.length !== after.length || before.some((value, i) => !Object.is(value, after[i]));

function harness(server, window = new EventTarget()) {
  const slots = [], timers = new Map(), payloads = [], calls = [];
  let index = 0, effects = [], timerId = 0;
  const context = vm.createContext({
    API: '', finishedJobStates, window, setTimeout: fn => (timers.set(++timerId, fn), timerId), clearTimeout: id => timers.delete(id),
    fetch: async url => {
      calls.push(url);
      if (url === '/api/settings') return {ok: true, json: async () => ({daedalus_v3_enabled: true})};
      const reply = typeof server.projects === 'function' ? await server.projects() : server;
      return {ok: true, json: async () => ({projects: reply.list, active_project_id: reply.active})};
    },
    useState: initial => { const k = index++; const slot = slots[k] || (slots[k] = {value: initial});
      return [slot.value, next => { slot.value = typeof next === 'function' ? next(slot.value) : next; }]; },
    useRef: initial => slots[index++] ||= {current: initial},
    useCallback: (fn, deps) => { const k = index++; if (changed(slots[k]?.deps, deps)) slots[k] = {fn, deps}; return slots[k].fn; },
    useEffect: (fn, deps) => { const k = index++, slot = slots[k];
      if (!slot || changed(slot.deps, deps)) { slots[k] = {deps, cleanup: slot?.cleanup};
        effects.push(() => { slots[k].cleanup?.(); slots[k].cleanup = fn(); }); } },
  });
  vm.runInContext(['versionsDiffer', 'useRequestOptions'].map(extract).join('\n'), context);
  let props = {conversationId: 'conv-1', currentProjectId: '', refreshKey: undefined, jobIds: '', jobChanges: 0, onChange: p => payloads.push(p)};
  let view;
  const render = () => { index = 0; effects = []; view = context.useRequestOptions(props); effects.forEach(run => run()); };
  // Re-render until state, effects, fetches and the coalescing timer have all settled.
  const settle = async (next = {}) => {
    props = {...props, ...next};
    for (let round = 0; round < 8; round++) {
      render(); await new Promise(resolve => setImmediate(resolve));
      for (const [id, fn] of [...timers]) { timers.delete(id); fn(); }
    }
    return view;
  };
  return {settle, payloads, calls, view: () => view, projectFetches: () => calls.filter(url => url.startsWith('/api/coder/projects')).length};
}
const project = (accepted, working) => ({id: 'cp-1', name: 'Board', accepted_revision_id: accepted, working_revision_id: working});

test('a follow-up delivered in the same conversation offers the accepted version without a refresh', async () => {
  // The project row is created by the job, after the composer first loaded the list.
  const server = {list: [], active: ''};
  const h = harness(server);
  assert.equal((await h.settle()).versions, false);
  Object.assign(server, {list: [project('r1', 'r1')], active: 'cp-1'});
  let view = await h.settle({jobIds: 'cw3-build'});
  assert.equal(view.current, 'cp-1', 'the new project resolves without a conversation reload');
  assert.equal(view.versions, false);
  // The follow-up delivers a candidate: working moves to r2 while accepted stays r1.
  server.list = [project('r1', 'r2')];
  view = await h.settle({jobIds: 'cw3-edit|cw3-build', jobChanges: 1});
  assert.equal(view.versions, true);
  view.update('', 'inherit', 'accepted');
  view = await h.settle();
  assert.equal(h.payloads.at(-1).daedalus_source_pointer, 'accepted');
  // A later job change refetches without clearing the user's in-progress choice.
  const resets = h.payloads.filter(p => !Object.keys(p).length).length;
  view = await h.settle({jobChanges: 2});
  assert.equal(view.source, 'accepted');
  assert.equal(h.payloads.filter(p => !Object.keys(p).length).length, resets);
  assert.equal(h.payloads.at(-1).daedalus_source_pointer, 'accepted');
});

test('an Accept that makes both versions equal drops the hidden accepted choice', async () => {
  const server = {list: [project('r1', 'r2')], active: 'cp-1'};
  const h = harness(server);
  let view = await h.settle();
  view.update('cp-1', 'inherit', 'accepted');
  view = await h.settle();
  assert.equal(h.payloads.at(-1).daedalus_source_pointer, 'accepted');
  server.list = [project('r2', 'r2')];
  view = await h.settle({jobChanges: 1});
  assert.equal(view.versions, false);
  assert.equal(view.source, 'working');
  assert.equal(h.payloads.at(-1).daedalus_source_pointer, '');
  assert.equal(h.payloads.at(-1).daedalus_project_id, 'cp-1');
});

test("the server's active project wins over a stale conversation row", async () => {
  const stale = {id: 'cp-old', name: 'Old', accepted_revision_id: 'a', working_revision_id: 'a'};
  const h = harness({list: [stale, project('r1', 'r2')], active: 'cp-1'});
  const view = await h.settle({currentProjectId: 'cp-old'});
  assert.equal(view.current, 'cp-1');
  assert.equal(view.versions, true);
});

test('a slow earlier projects response never overwrites a newer one', async () => {
  let release;
  const replies = [{list: [project('r1', 'r1')], active: 'cp-1'}, {list: [project('r1', 'r2')], active: 'cp-1'}];
  let n = 0;
  const server = {projects: () => { const reply = replies[Math.min(n++, 1)];
    return n === 1 ? new Promise(resolve => { release = () => resolve(reply); }) : reply; }};
  const h = harness(server);
  await h.settle();
  const view = await h.settle({jobChanges: 1});
  assert.equal(view.versions, true);
  release();
  assert.equal((await h.settle()).versions, true);
  assert.equal(h.projectFetches(), 2);
});

test('a conversation or upload change still resets the choice; job changes only refetch', async () => {
  const h = harness({list: [project('r1', 'r2')], active: 'cp-1'});
  let view = await h.settle();
  view.update('cp-1', 'on', 'accepted');
  await h.settle({jobChanges: 1});
  assert.equal(h.view().target, 'cp-1');
  view = await h.settle({refreshKey: 'upload.zip'});
  assert.equal(Object.keys(h.payloads.at(-1)).length, 0);
  assert.equal(view.target, '');
  assert.equal(view.source, 'working');
  assert.equal(view.visual, 'inherit');
});

test('job signatures change on delivery and Accept but not on progress ticks', () => {
  const {jobSignature} = vm.runInContext(`${extract('jobSignature')};({jobSignature})`, vm.createContext({}));
  const running = {id: 'cw3-a', state: 'coding', revision_id: 'r1', seconds_used: 10, event_sequence: 4};
  assert.equal(jobSignature(running), jobSignature({...running, seconds_used: 99, event_sequence: 40, updated_at: 'later'}));
  const candidate = {...running, state: 'ready_for_review', candidate_artifact: {id: 'art-1'}};
  assert.notEqual(jobSignature(candidate), jobSignature(running));
  const accepted = {...candidate, state: 'completed', accepted_by: 'user', artifact: {id: 'art-2'}};
  assert.notEqual(jobSignature(accepted), jobSignature(candidate));
});

test('only recent v3 jobs that can still move a pointer are watched', () => {
  const {watchedJobs} = vm.runInContext(`${extract('watchedJobs')};({watchedJobs})`, vm.createContext({finishedJobStates}));
  const jobs = [{id: 'a', workflow_version: 3, state: 'completed'}, {id: 'b', workflow_version: 3, state: 'ready_for_review'},
    {id: 'c', workflow_version: 2, state: 'fixing'}, {id: 'd', workflow_version: 3, state: 'coding'},
    {id: 'e', workflow_version: 3, state: 'blocked'}, {id: 'f', workflow_version: 3, state: 'queued'}];
  assert.deepEqual(Array.from(watchedJobs(jobs), job => job.id), ['b', 'd', 'e']);
  assert.equal(watchedJobs(undefined).length, 0);
});

// The job store's own source, imports stripped, run without React: useSyncExternalStore only reads the snapshot.
function jobStore(fetch, window) {
  const code = fs.readFileSync(new URL('./daedalusJobStore.js', import.meta.url), 'utf8')
    .replace(/^import .*$/gm, '').replace(/^export /gm, '');
  return vm.runInContext(`${code};({useDaedalusJob})`, vm.createContext({...jobHelpers, fetch, window, CustomEvent,
    AbortController, TextDecoder, setTimeout, clearTimeout, userScopedUrl: url => url,
    useCallback: fn => fn, useSyncExternalStore: (subscribe, get) => get()}));
}
const parked = id => ({id, project_id: 'cp-1', workflow_version: 3, state: 'ready_for_review',
  presentation: {actions: {accept: {revision_id: 'r1'}}}});

test('a committed Accept notifies the composer even if the job refresh fails; a refused one does not', async () => {
  const window = new EventTarget(), seen = [];
  window.addEventListener('daedalus-project-changed', event => seen.push(event.detail.project_id));
  let refused = false;
  const {useDaedalusJob} = jobStore(async url => url.endsWith('/accept')
    ? {ok: !refused, json: async () => (refused ? {detail: 'Not a candidate'} : {ok: true})}
    : {ok: false, json: async () => ({detail: 'Unable to refresh this job'})}, window);
  const job = parked('cw3-J1');
  await useDaedalusJob(job).action('accept');
  assert.deepEqual(seen, ['cp-1']);
  assert.equal(useDaedalusJob(job).error, 'Unable to refresh this job');
  refused = true;
  await useDaedalusJob(job).action('accept');
  assert.deepEqual(seen, ['cp-1']);
  assert.equal(useDaedalusJob(job).error, 'Not a candidate');
});

test('an Accept on a parked candidate past the watch cap still offers the accepted version', async () => {
  const {watchedJobs} = vm.runInContext(`${extract('watchedJobs')};({watchedJobs})`, vm.createContext({finishedJobStates}));
  // updated_at DESC: three unaccepted follow-ups parked after the first build.
  const jobs = ['cw3-J4', 'cw3-J3', 'cw3-J2', 'cw3-J1'].map(parked), first = jobs[3];
  assert.ok(!watchedJobs(jobs).some(job => job.id === first.id), 'the first build has no JobWatch');
  const window = new EventTarget(), server = {list: [project(null, 'r4')], active: 'cp-1'};
  const h = harness(server, window);
  let view = await h.settle({jobIds: jobs.map(job => job.id).join('|')});
  assert.equal(view.versions, false);
  const before = h.projectFetches();
  const {useDaedalusJob} = jobStore(async url => {
    if (!url.endsWith('/accept')) return {ok: true, json: async () => ({...first, state: 'completed', accepted_by: 'user'})};
    server.list = [project('r1', 'r4')];
    return {ok: true, json: async () => ({ok: true})};
  }, window);
  await useDaedalusJob(first).action('accept');
  // Same job ids, no job change, same project: only the Accept event can trigger this refetch.
  view = await h.settle();
  assert.equal(h.projectFetches(), before + 1);
  assert.equal(view.versions, true);
  view.update('', 'inherit', 'accepted');
  await h.settle();
  assert.equal(h.payloads.at(-1).daedalus_source_pointer, 'accepted');
});
