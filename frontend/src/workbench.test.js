import test,{mock} from 'node:test';
import assert from 'node:assert/strict';
import {openWorkbench,closeWorkbench,setWorkbenchTab,configureWorkbench,shouldAutoOpen,updateLivePreview,rebindLivePreview,workbenchState,resetWorkbench,DEBOUNCE_MS} from './workbench.js';

test.beforeEach(()=>resetWorkbench());

test('open, tab and close; unknown kinds are ignored',()=>{
  openWorkbench({kind:'nonsense'});assert.equal(workbenchState().open,false);
  openWorkbench({kind:'daedalus',job:{id:'j1'}});
  assert.equal(workbenchState().tab,'Activity');assert.equal(workbenchState().job.id,'j1');
  setWorkbenchTab('Checks');
  openWorkbench({kind:'daedalus',job:{id:'j1'}});assert.equal(workbenchState().tab,'Checks','the same job keeps its tab');
  openWorkbench({kind:'daedalus',job:{id:'j2'}});assert.equal(workbenchState().tab,'Activity','another job starts on Activity');
  openWorkbench({kind:'html',html:{key:'m:c0',code:'<p>',title:'t'}});
  assert.equal(workbenchState().tab,'Preview');assert.equal(workbenchState().job,null);
  closeWorkbench();assert.equal(workbenchState().open,false);assert.equal(workbenchState().html,null);
});
test('auto-open fires once per key and only while allowed',()=>{
  assert(shouldAutoOpen('a'));assert(!shouldAutoOpen('a'));
  configureWorkbench({autoOpen:false});
  assert(!shouldAutoOpen('b'));
  configureWorkbench({autoOpen:true});
  assert(shouldAutoOpen('b'),'a key refused by the preference is not consumed');
});
test('streaming updates coalesce to one rewrite per debounce window; the final update is immediate',()=>{
  mock.timers.enable({apis:['setTimeout']});
  try{
    openWorkbench({kind:'html',html:{key:'m:c0',code:'<p>1',streaming:true}});
    const before=workbenchState().html;
    updateLivePreview('m:c0','<p>12',{streaming:true});
    updateLivePreview('m:c0','<p>123',{streaming:true});
    assert.equal(workbenchState().html,before,'nothing is rewritten before the window closes');
    mock.timers.tick(DEBOUNCE_MS);
    assert.equal(workbenchState().html.code,'<p>123');
    updateLivePreview('m:c0','<p>1234',{streaming:false});
    assert.equal(workbenchState().html.code,'<p>1234');assert.equal(workbenchState().html.streaming,false);
    updateLivePreview('other','<p>x',{streaming:false});
    assert.equal(workbenchState().html.code,'<p>1234','other blocks never touch the open preview');
  }finally{mock.timers.reset();}
});
test('a finalized message rebinds the same block under its saved id',()=>{
  openWorkbench({kind:'html',html:{key:'ghost-1:/c2',code:'<p>partial',streaming:true}});
  rebindLivePreview('msg-9:/c0','<p>other block');
  assert.equal(workbenchState().html.key,'ghost-1:/c2','a different block cannot steal the pane');
  rebindLivePreview('msg-9:/c2','<p>partial done');
  assert.equal(workbenchState().html.key,'msg-9:/c2');assert.equal(workbenchState().html.code,'<p>partial done');
  assert.equal(workbenchState().html.streaming,false);
  rebindLivePreview('msg-10:/c2','<p>unrelated later message');
  assert.equal(workbenchState().html.key,'msg-9:/c2','a settled preview only follows identical code');
});
