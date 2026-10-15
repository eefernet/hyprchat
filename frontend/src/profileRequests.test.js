import test from 'node:test';
import assert from 'node:assert/strict';
import {createPrefsSync} from './settingsSync.js';

const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));

test('debounced preferences retain origin and cancel on switch', async()=>{
  const previous=globalThis.fetch;
  const calls=[];
  globalThis.fetch=async(...args)=>{calls.push(args);return {ok:true};};
  try{
    let context={userId:'alice',epoch:1};
    const sync=createPrefsSync({api:'',getContext:()=>context});
    sync.queue('prompts',['private'],5);
    context={userId:'bob',epoch:2};
    await delay(15);
    assert.equal(calls.length,0);
    sync.queue('prompts',['bob'],5);
    sync.cancel();
    await delay(15);
    assert.equal(calls.length,0);
    await sync.put('prompts',['saved']);
    assert.equal(calls[0][1].headers['X-HyprChat-User'],'bob');
  }finally{globalThis.fetch=previous;}
});

test('preference HTTP failure is visible',async()=>{
  const previous=globalThis.fetch;
  globalThis.fetch=async()=>({ok:false,status:503});
  const errors=[];
  try{await createPrefsSync({api:'',onError:e=>errors.push(e.message)}).put('prompts',[]);assert.match(errors[0],/503/);}
  finally{globalThis.fetch=previous;}
});

test('switch aborts pending requests and rejects responses delivered after switching',async()=>{
  const previousWindow=globalThis.window, previousStorage=globalThis.localStorage;
  const requests=[];
  globalThis.localStorage={getItem:()=>null};
  globalThis.window={location:{origin:'http://localhost'},dispatchEvent:()=>{},fetch:(input,init)=>new Promise(resolve=>requests.push({input,init,resolve}))};
  try{
    const session=await import('./session.js');
    session.hcSetUserContext('alice','token-a');
    const pending=window.fetch('/api/prefs');
    session.hcSetUserContext('bob','token-b');
    assert.equal(requests[0].init.signal.aborted,true);
    assert.equal(requests[0].init.headers.get('X-HyprChat-User'),'alice');
    requests[0].resolve(new Response('{}'));
    await assert.rejects(pending,{name:'AbortError'});
    const next=window.fetch('/api/prefs');
    requests[1].resolve(new Response('{"private":"bob"}'));
    const response=await next;
    session.hcSetUserContext('alice','token-a');
    await assert.rejects(response.json(),{name:'AbortError'});
  }finally{globalThis.window=previousWindow;globalThis.localStorage=previousStorage;}
});
