import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

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
    convs: [{id: 'conv-1', messages: []}], sendPrepRef: {current: null},
    ghostMode: false, isGhostConv: c => !!c?.ephemeral,
    pendingEffort: null, pendingToolIds: [], pendingPersona: null, pendingUseMemories: false,
    pendingChatModel: '', modelChoiceRef: {current: {byConv: {}}},
    storedLastModel: () => '', models: ['local-model'], API: '', quickSearch: false,
    inpRef: {current: null}, chatScrollRef: {current: null},
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
  vm.createContext(context);
  context.cancelSendPreparation = handler('cancelSendPreparation', context);
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
