import test from 'node:test';
import assert from 'node:assert/strict';
import {createImageSettingsSync} from './imageSettingsSync.js';

const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fixture(request){
  const state={context:{userId:'one',epoch:1},values:{},ready:false,errors:[],saved:0,presets:{},calls:[]};
  const sync=createImageSettingsSync({request:(path,opts)=>{state.calls.push({path,opts});return request(path,opts);},
    getContext:()=>state.context,onValues:(values,attempted)=>{
      for(const [key,value] of Object.entries(values))if(!attempted||state.values[key]===attempted[key])state.values[key]=value;
    },onReady:value=>{state.ready=value;},
    onError:error=>state.errors.push(error),onSaved:()=>state.saved++,onPreset:(key,value)=>{state.presets[key]=value;}});
  return {state,sync};
}

test('hydration is authoritative and does not save browser values',async()=>{
  const {state,sync}=fixture(async()=>({image_chat_checkpoint:'server'}));
  state.values.image_chat_checkpoint='stale browser value';
  await sync.save('image_chat_checkpoint','before hydration');
  await sync.load();
  assert.equal(state.values.image_chat_checkpoint,'server');
  assert.equal(state.ready,true);assert.equal(state.saved,0);
  assert.deepEqual(state.calls,[{path:'/api/images/settings',opts:undefined}]);
});

test('failed save restores last confirmed value and exposes the server error',async()=>{
  const {state,sync}=fixture(async(path,opts)=>{if(opts)throw new Error('Saved image workflow was not found');return {image_chat_workflow:'saved'};});
  await sync.load();await sync.save('image_chat_workflow','missing');
  assert.equal(state.values.image_chat_workflow,'saved');assert.equal(state.saved,0);
  assert.deepEqual(state.errors,['Saved image workflow was not found']);
});

test('rapid edits serialize and a late response cannot replace a newer edit',async()=>{
  const first=deferred();let writes=0;
  const {state,sync}=fixture(async(path,opts)=>{
    if(!opts)return {image_chat_checkpoint:'original'};
    writes++;if(writes===1)return first.promise;
    throw new Error('second failed');
  });
  await sync.load();
  const a=sync.save('image_chat_checkpoint','first');
  const b=sync.save('image_chat_checkpoint','second');
  await Promise.resolve();assert.equal(writes,1);
  first.resolve({image_chat_checkpoint:'first'});await a;
  assert.equal(state.values.image_chat_checkpoint,'second');
  await b;assert.equal(state.values.image_chat_checkpoint,'first');
  assert.deepEqual(state.errors,['second failed']);
});

test('response for one field does not overwrite a pending edit to another',async()=>{
  const {state,sync}=fixture(async(path,opts)=>opts?{image_chat_checkpoint:'model',image_chat_vae:opts.body.image_chat_vae||'old-vae'}:{image_chat_checkpoint:'old',image_chat_vae:'old-vae'});
  await sync.load();
  const a=sync.save('image_chat_checkpoint','model'),b=sync.save('image_chat_vae','new-vae');
  await a;assert.equal(state.values.image_chat_vae,'new-vae');
  await b;assert.equal(state.values.image_chat_vae,'new-vae');
});

test('profile switch ignores stale hydration and queued writes',async()=>{
  const loading=deferred();let loads=0;
  const {state,sync}=fixture(async()=>++loads===1?loading.promise:{image_chat_checkpoint:'profile-two'});
  const oldLoad=sync.load();state.context={userId:'two',epoch:2};sync.reset();await sync.load();
  loading.resolve({image_chat_checkpoint:'profile-one'});await oldLoad;
  assert.equal(state.values.image_chat_checkpoint,'profile-two');
  const save=sync.save('image_chat_checkpoint','queued');
  state.context={userId:'three',epoch:3};sync.reset();await save;
  assert.equal(state.calls.length,2);assert.equal(state.saved,0);
});

test('in-flight save from previous profile cannot update current values or errors',async()=>{
  const writing=deferred();
  const {state,sync}=fixture(async(path,opts)=>opts?writing.promise:{image_chat_checkpoint:state.context.userId});
  await sync.load();const save=sync.save('image_chat_checkpoint','old-edit');await Promise.resolve();
  state.context={userId:'two',epoch:2};sync.reset();await sync.load();
  writing.reject(new Error('old profile failed'));await save;
  assert.equal(state.values.image_chat_checkpoint,'two');assert.deepEqual(state.errors,[]);assert.equal(state.saved,0);
});

test('failed initial load keeps editing gated until retry succeeds',async()=>{
  let fail=true;
  const {state,sync}=fixture(async()=>{if(fail)throw new Error('offline');return {image_chat_checkpoint:'saved'};});
  await sync.load();assert.equal(state.ready,false);
  await sync.save('image_chat_checkpoint','ignored');assert.equal(state.calls.length,1);
  fail=false;await sync.load();assert.equal(state.ready,true);
});

test('a save response preserves newer text that has not blurred yet',async()=>{
  const writing=deferred();
  const {state,sync}=fixture(async(path,opts)=>opts?writing.promise:{image_chat_prompt_prefix:'original'});
  await sync.load();const saving=sync.save('image_chat_prompt_prefix','submitted');await Promise.resolve();
  state.values.image_chat_prompt_prefix='still typing';
  writing.resolve({image_chat_prompt_prefix:'submitted'});await saving;
  assert.equal(state.values.image_chat_prompt_prefix,'still typing');
});

test('preset save restores prior values on error and ignores replies after profile switch',async()=>{
  const {state,sync}=fixture(async(path,opts)=>{if(opts)throw new Error('preset failed');return {};});
  await sync.load();sync.setPresets({'model':{prompt_prefix:'saved',steps:17}});
  await sync.savePreset('model',{prompt_prefix:'unsaved'});
  assert.deepEqual(state.presets.model,{prompt_prefix:'saved',steps:17});assert.equal(state.saved,0);
  assert.equal(state.calls[1].path,'/api/images/model-settings/model');
});
