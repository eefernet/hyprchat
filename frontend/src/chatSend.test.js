import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {documentAttachmentContext} from './documentFiles.js';

// Exercise the actual root-owned handlers without moving app state or adding
// a DOM framework. Delayed fetches expose races hidden by instant mocks.
const source = fs.readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
function handler(name, context) {
  const marker = `  const ${name}=`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} handler exists`);
  const end = source.indexOf('\n  };', start);
  return vm.runInContext(`(${source.slice(start + marker.length, end + 4)})`, context);
}

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

const staged = () => ({ok: true, json: async () => ({sandbox_path: '/root/chat_files/conv-1/upload/data.csv'})});
const attachment = (name = 'data.csv') => ({name, type: 'data', size: 3, file: new Blob(['a,b'])});

function setup(overrides = {}) {
  const calls = {sent: [], notices: [], progress: [], conversations: 0};
  const noop = () => {};
  const context = {
    console, Date, Math, Blob, FormData, AbortController, setTimeout: noop,
    streaming: false, councilRunning: false, loadingConv: false,
    inp: 'Analyze this', attachments: [attachment()], actId: 'conv-1', currentUserId: 'user-1',
    convs: [{id: 'conv-1', messages: []}], sendPrepRef: {current: null}, loadReqRef: {current: 0}, setLoadingConv: noop,
    chatCreationBusy:{current:false},chatNavigation:{current:'initial'},setWsPanel:noop,setWorkspaces:noop,setGhostMode:noop,
    persistProfileFirstMessage:async()=>null,
    ghostMode: false, isGhostConv: c => !!c?.ephemeral,
    documentsEnabled: false, documentAttachmentContext,
    pendingEffort: null, pendingToolIds: [], pendingPersona: null, pendingUseMemories: false,
    pendingChatModel: '', modelChoiceRef: {current: {byConv: {}}},
    storedLastModel: () => '', models: ['local-model'], API: '', quickSearch: false,
    inpRef: {current: null}, chatScrollRef: {current: null}, chatFollow: {follow: noop, release: noop},
    turnBusyRef: {current: false}, streamingCidRef: {current: null},
    notify: notice => calls.notices.push(notice),
    setPreparingSend: status => { context.preparingSend = status; calls.progress.push(status); },
    setInp: value => { context.inp = value; },
    setAttachments: value => { context.attachments = value; },
    setActId: id => { context.actId = id; },
    setConvs: fn => { context.convs = fn(context.convs); calls.conversations++; },
    uConv: (id, fn) => { context.convs = context.convs.map(c => c.id === id ? fn(c) : c); },
    setQuickResults: noop, setQuickSearchError: noop, setSearchLoading: noop,
    setPendingUseMemories: noop, setPanel: noop, setEvts: noop, setSessionTokens: noop,
    setCtxTokens: noop, setGenTokens: noop, setExpandedPill: noop, setCouncilRunning: noop,
    setCouncilResponses: noop, setCouncilHostContent: noop,
    sendMessages: async (...args) => { calls.sent.push(args); },
    sendCouncil: async () => { calls.sent.push('council'); },
    fetch: async () => staged(),
    ...overrides,
  };
  // send() reads the box through the drafts hook (chatDrafts.js); here that is the context's own inp/attachments.
  calls.adopted = [];
  context.chatDrafts = { get text() { return context.inp; }, get attachments() { return context.attachments; }, adopt: id => calls.adopted.push(id) };
  vm.createContext(context);
  context.cancelSendPreparation = handler('cancelSendPreparation', context);
  vm.runInContext(fs.readFileSync(new URL('./api.js',import.meta.url),'utf8').replace(/^import .*$/m,'').replace('export async function','async function'),context);
  context.createNewChat = handler('createNewChat', context);
  context.newChat = handler('newChat', context);
  return {context, calls, send: handler('send', context)};
}

test('double submission while uploading sends exactly one message', async () => {
  const upload = deferred();
  let uploads = 0;
  const {context, calls, send} = setup({fetch: async () => {uploads++; return upload.promise;}});
  const first = send();
  await send();
  assert.equal(uploads, 1);
  assert.equal(context.preparingSend, 'Uploading attachments…');
  assert.equal(context.inp, 'Analyze this');
  assert.equal(context.convs[0].messages.length, 0);
  upload.resolve(staged());
  await first;
  assert.equal(calls.sent.length, 1);
  assert.equal(context.inp, '');
  assert.equal(context.attachments.length, 0);
  assert.equal(context.convs[0].messages.length, 2);
  assert.equal(context.sendPrepRef.current, null);
});

test('conversation creation is also protected against double submission', async () => {
  const creation = deferred();
  let creates = 0;
  const {context, calls, send} = setup({actId: null, convs: [], attachments: [], fetch: async () => {creates++; return creation.promise;}});
  const first = send();
  await send();
  assert.equal(creates, 1);
  creation.resolve({ok: true, json: async () => ({id: 'new-conv'})});
  await first;
  assert.equal(calls.sent.length, 1);
  assert.equal(context.actId, 'new-conv');
  assert.equal(context.convs.length, 1);
});

test('failed creation preserves the draft and allows retry', async () => {
  const {context, calls, send} = setup({actId: null, convs: [], fetch: async () => ({ok: false, status: 503})});
  await send();
  assert.equal(context.inp, 'Analyze this');
  assert.equal(context.attachments.length, 1);
  assert.equal(context.convs.length, 0);
  assert.equal(calls.sent.length, 0);
  assert.equal(context.sendPrepRef.current, null);
  assert.match(calls.notices[0].detail, /503/);
});

for (const name of ['data.csv', 'book.xlsx']) {
  test(`failed ${name} upload preserves the draft without decoding a fallback`, async () => {
    const a = attachment(name);
    a.file.text = () => { throw new Error('full-file text fallback must never run'); };
    const {context, calls, send} = setup({attachments: [a], fetch: async () => ({ok: false, status: 502, json: async () => ({detail: 'Sandbox unavailable'})})});
    await send();
    assert.equal(calls.sent.length, 0);
    assert.equal(context.inp, 'Analyze this');
    assert.equal(context.attachments[0], a);
    assert.equal(context.sendPrepRef.current, null);
    assert.ok(calls.notices[0].detail.includes(name));
    context.fetch = async () => staged();
    await send();
    assert.equal(calls.sent.length, 1);
  });
}

test('one failed attachment aborts its siblings and never sends a partial batch', async () => {
  let secondSignal;
  const pending = deferred();
  let count = 0;
  const {context, calls, send} = setup({
    attachments: [attachment('one.csv'), attachment('two.csv')],
    fetch: async (_url, opts) => {
      if (++count === 1) return {ok: false, status: 502, json: async () => ({detail: 'offline'})};
      secondSignal = opts.signal;
      return pending.promise;
    },
  });
  await send();
  assert.equal(secondSignal.aborted, true);
  assert.equal(context.attachments.length, 2);
  assert.equal(calls.sent.length, 0);
  pending.resolve(staged());
});

test('late completion after cancellation cannot clear a newer draft or send', async () => {
  const upload = deferred();
  const {context, calls, send} = setup({fetch: async () => upload.promise});
  const first = send();
  context.cancelSendPreparation();
  context.actId = 'another-conversation';
  context.currentUserId = 'another-user';
  context.inp = 'New draft';
  context.attachments = [attachment('new.csv')];
  upload.resolve(staged());
  await first;
  assert.equal(context.inp, 'New draft');
  assert.equal(context.attachments[0].name, 'new.csv');
  assert.equal(calls.sent.length, 0);
  assert.equal(calls.notices.length, 0);
});

test('cancelled creation cannot activate its late conversation response', async () => {
  const creation = deferred();
  const {context, calls, send} = setup({actId: null, convs: [], fetch: async () => creation.promise});
  const first = send();
  context.cancelSendPreparation();
  context.actId = 'different-conv';
  creation.resolve({ok: true, json: async () => ({id: 'stale-conv'})});
  await first;
  assert.equal(context.actId, 'different-conv');
  assert.equal(context.convs.length, 0);
  assert.equal(calls.sent.length, 0);
});

test('ghost data attachments explain the limitation without creating a conversation', async () => {
  const {context, calls, send} = setup({ghostMode: true, actId: null, convs: [], fetch: async () => {throw new Error('unexpected request');}});
  await send();
  assert.match(calls.notices[0].detail, /saved conversation/);
  assert.equal(context.attachments.length, 1);
  assert.equal(calls.conversations, 0);
  assert.equal(calls.sent.length, 0);
});

test('malformed upload success does not discard the draft', async () => {
  const {context, calls, send} = setup({fetch: async () => ({ok: true, json: async () => ({})})});
  await send();
  assert.equal(calls.sent.length, 0);
  assert.equal(context.attachments.length, 1);
  assert.match(calls.notices[0].detail, /no file path/);
});


test('workspace creation sends defaults atomically without unrelated persona or tools', async()=>{
  let posted;
  const {context}=setup({pendingPersona:{model_config_id:'unrelated'},pendingToolIds:['codeagent'],
    fetch:async(url,opts)=>{if(opts?.method==='POST'){posted=JSON.parse(opts.body);return {ok:true,json:async()=>({id:'workspace-chat',model:'project-model',tool_ids:[],is_council:'0',workspace_context:{mode:'selected',workspace_id:'w'}})};}return {ok:true,json:async()=>[]};}});
  await context.newChat(true,{workspaceId:'w'});
  assert.deepEqual(posted,{title:'New Chat',use_memories:'0',workspace_context:{mode:'selected',workspace_id:'w'}});
  assert.equal(context.convs[0].model,'project-model');
  assert.equal(context.convs[0].is_council,false);
  assert.deepEqual(context.convs[0].tool_ids,[]);
});

test('workspace repeated clicks create once and late navigation never activates the response', async()=>{
  const pending=deferred();let creates=0;
  const {context}=setup({fetch:async(_url,opts)=>{if(opts?.method==='POST'){creates++;return pending.promise;}return {ok:true,json:async()=>[]};}});
  const first=context.newChat(true,{workspaceId:'w'});
  await context.newChat(true,{workspaceId:'w'});
  context.chatNavigation.current='another workspace';
  pending.resolve({ok:true,json:async()=>({id:'late',model:'project-model'})});
  assert.equal(await first,null);
  assert.equal(creates,1);assert.equal(context.actId,'conv-1');
  assert.equal(context.chatCreationBusy.current,false);
});

// ── A queued message (messageQueue.js) ─────────────────────────────────────────

test('a queued message is sent from the queue and leaves the box alone', async () => {
  const {context, calls, send} = setup({inp: 'still typing this', attachments: []});
  let accepted = 0;
  await send({queueId: 'q1', chatId: 'conv-1', text: 'queued earlier', attachments: [], accepted: () => { accepted++; }});
  assert.equal(calls.sent.length, 1);
  assert.equal(calls.sent[0][2], 'queued earlier');
  assert.equal(calls.sent[0][3].fromQueue, true);
  assert.equal(accepted, 1);
  assert.equal(context.inp, 'still typing this');           // what is in the box now is not the queued message
});

test('a queued message is not sent into a different chat', async () => {
  const {calls, send} = setup({inp: '', attachments: []});
  let accepted = 0;
  await send({queueId: 'q1', chatId: 'conv-other', text: 'queued in another chat', attachments: [], accepted: () => { accepted++; }});
  assert.equal(calls.sent.length, 0);
  assert.equal(accepted, 0);
});

test('a queued message that fails to upload is not reported as sent', async () => {
  const {calls, send} = setup({inp: '', attachments: [], fetch: async () => ({ok: false, status: 500, json: async () => ({detail: 'sandbox down'})})});
  let accepted = 0;
  await send({queueId: 'q1', chatId: 'conv-1', text: 'with a file', attachments: [attachment()], accepted: () => { accepted++; }});
  assert.equal(calls.sent.length, 0);
  assert.equal(accepted, 0);                                  // it stays in the queue
});

test('the Send button\'s click event is not mistaken for a queued message', async () => {
  const {context, calls, send} = setup({inp: 'typed in the box', attachments: []});
  await send({type: 'click', target: {}});
  assert.equal(calls.sent.length, 1);
  assert.equal(calls.sent[0][2], 'typed in the box');
  assert.equal(context.inp, '');
});

test('the first send of a new chat takes its draft with it', async () => {
  const {calls, send} = setup({actId: null, inp: 'first message of a new chat', attachments: [], convs: [],
    fetch: async () => ({ok: true, json: async () => ({id: 'conv-new', title: 'New Chat', messages: []})})});
  await send();
  assert.ok(calls.adopted.length >= 1, 'the draft was moved to the new chat');
});

// ── 2026-11-16 chat pass: deleting a chat ─────────────────────────────────────────────────────────
import {afterDelete} from './chatList.js';
function deleteSetup({answer = {ok: true, status: 200}, confirm = true, streamingCid = null} = {}) {
  const calls = {loaded: [], notices: [], stopped: [], asked: [], requests: []};
  const noop = () => {};
  const context = {
    console, Math, API: '', afterDelete,
    convsRef: {current: [{id: 'a', title: 'First'}, {id: 'b', title: 'Second'}, {id: 'c', title: 'Third'}]},
    actIdRef: {current: 'a'}, streamingCidRef: {current: streamingCid}, streamEndedRef: {current: new Map()}, deletedChatsRef: {current: new Set()},
    isGhostConv: c => !!c?.ephemeral,
    confirmAction: async opts => { calls.asked.push(opts); return confirm; },
    stopStrandedStream: ids => { calls.stopped.push(ids); },
    setConvs: fn => { context.convsRef.current = fn(context.convsRef.current); },
    setActId: id => { context.actIdRef.current = id; },
    loadConversation: id => { calls.loaded.push(id); context.actIdRef.current = id; },
    setCouncilRunning: noop, setCouncilResponses: noop, setCouncilHostContent: noop, setConvTags: noop,
    chatDrafts: {drop: noop}, messageQueue: {drop: noop},
    notify: notice => calls.notices.push(notice),
    fetch: async (url, init) => { calls.requests.push([url, init?.method]); if (answer instanceof Error) throw answer; return answer; },
  };
  vm.createContext(context);
  return {context, calls, delChat: handler('delChat', context)};
}

test('deleting the open chat asks first, then loads the next chat instead of showing it empty', async () => {
  const {context, calls, delChat} = deleteSetup();
  await delChat('a');
  assert.match(calls.asked[0].body, /First/);
  assert.deepEqual(calls.loaded, ['b']);
  assert.deepEqual(context.convsRef.current.map(c => c.id), ['b', 'c']);
  assert.deepEqual(calls.requests, [['/api/conversations/a', 'DELETE']]);
});

test('answering No to the delete question changes nothing', async () => {
  const {context, calls, delChat} = deleteSetup({confirm: false});
  await delChat('a');
  assert.deepEqual(context.convsRef.current.map(c => c.id), ['a', 'b', 'c']);
  assert.deepEqual(calls.requests, []);
  assert.deepEqual(calls.stopped, []);
});

test('deleting a chat that is still replying stops that reply and says so in the question', async () => {
  const {calls, delChat} = deleteSetup({streamingCid: 'b'});
  await delChat('b');
  assert.match(calls.asked[0].body, /reply still being written/);
  assert.deepEqual(calls.stopped.map(ids => Array.from(ids)), [['b']]);   // arrays made inside the vm context
  assert.deepEqual(calls.loaded, []);   // another chat was open: it stays
});

test('a chat the server refuses to delete comes back in its place with a notice', async () => {
  const {context, calls, delChat} = deleteSetup({answer: {ok: false, status: 500}});
  await delChat('b');
  assert.deepEqual(Array.from(context.convsRef.current, c => c.id), ['a', 'b', 'c']);
  assert.equal(calls.notices[0].text, 'Chat not deleted');
});
