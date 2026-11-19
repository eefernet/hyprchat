import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchAnalytics} from './analytics.js';

const summary=()=>Object.fromEntries(['all_time','today','daily_30d','by_model_all','by_persona_all'].map(key=>[key,[]]));

test('empty successful telemetry is valid and retains its query identity',async()=>{
  const calls=[],signal=new AbortController().signal;
  const request=async(path,options)=>{calls.push([path,options.signal]);return path.endsWith('/summary')?summary():[];};
  const data=await fetchAnalytics(request,7,'model',signal);
  assert.deepEqual(data,{summary:summary(),grouped:[],days:7,group:'model'});
  assert.deepEqual(calls,[['/api/analytics/tokens/summary',signal],['/api/analytics/tokens?days=7&group_by=model',signal]]);
});

test('malformed summaries and grouped rows never become empty usage',async()=>{
  for(const invalid of [null,{},[],{detail:'Unavailable'},{...summary(),all_time:[null]}, {...summary(),by_model_all:{}}]){
    await assert.rejects(fetchAnalytics(async path=>path.endsWith('/summary')?invalid:[],30,'day'),/invalid statistics/);
  }
  for(const invalid of [{detail:'Unavailable'},null,[null],[[]],[{}],[{total_tokens:'42'}],[{total_tokens:NaN}],[{total_tokens:-1}]]){
    await assert.rejects(fetchAnalytics(async path=>path.endsWith('/summary')?summary():invalid,30,'day'),/invalid statistics/);
  }
});

test('checked API failures propagate for either endpoint, including retry after a partial failure',async()=>{
  globalThis.localStorage={getItem:()=>null};
  globalThis.window={location:{origin:'http://localhost'},dispatchEvent(){},fetch:globalThis.fetch};
  const {apiJson}=await import('./api.js');
  const original=globalThis.fetch;
  try{
    for(const endpoint of ['/summary','grouped']){
      for(const mode of [403,500,'json','network']){
        globalThis.fetch=async url=>{
          const isSummary=url.endsWith('/summary');
          if(isSummary===(endpoint==='/summary')){
            if(mode==='network')throw new TypeError('Network unavailable');
            if(mode==='json')return new Response('not json');
            return new Response(JSON.stringify({detail:'Statistics service unavailable'}),{status:mode});
          }
          return new Response(JSON.stringify(isSummary?summary():[]));
        };
        await assert.rejects(fetchAnalytics(apiJson,30,'day'),/unavailable|unreadable/i);
      }
    }
    globalThis.fetch=async url=>new Response(JSON.stringify(url.endsWith('/summary')?summary():[{model:'local',total_tokens:42}]));
    assert.equal((await fetchAnalytics(apiJson,0,'model')).grouped[0].total_tokens,42);
  }finally{globalThis.fetch=original;delete globalThis.window;delete globalThis.localStorage;}
});
