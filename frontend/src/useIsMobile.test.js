import test from 'node:test';
import assert from 'node:assert/strict';
import {observeMobileViewport} from './useIsMobile.js';

function fixture(withViewport=true) {
  const target=()=>({events:new Map(),addEventListener(k,fn){this.events.set(k,fn);},removeEventListener(k){this.events.delete(k);},fire(k){this.events.get(k)?.();}});
  const doc={...target(),activeElement:null};
  let frame=null;
  const win={...target(),document:doc,innerWidth:390,innerHeight:844,
    requestAnimationFrame(fn){frame=fn;return 1;},cancelAnimationFrame(){frame=null;},
    visualViewport:withViewport?{...target(),height:844,offsetTop:0,scale:1}:undefined};
  const states=[];
  const stop=observeMobileViewport(win,s=>states.push(s));
  return {win,doc,states,stop,flush(){const f=frame;frame=null;f?.();},focus(){doc.activeElement={tagName:'TEXTAREA'};doc.fire('focusin');}};
}

test('measures on mount; keyboard follows height and pan without scrolling the page',()=>{
  const f=fixture();assert.deepEqual(f.states.at(-1),{height:null,offsetTop:0,keyboardOpen:false});
  f.focus();Object.assign(f.win.visualViewport,{height:420,offsetTop:37});f.win.visualViewport.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:420,offsetTop:37,keyboardOpen:true});
  f.win.visualViewport.offsetTop=12;f.win.visualViewport.fire('scroll');f.flush();
  assert.equal(f.states.at(-1).offsetTop,12);
  // Blur arrives before the keyboard animation finishes: don't expand early.
  f.doc.activeElement=null;f.doc.fire('focusout');f.flush();assert.equal(f.states.at(-1).keyboardOpen,true);
  f.win.visualViewport.height=844;f.win.visualViewport.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:null,offsetTop:0,keyboardOpen:false});f.stop();
});

test('ignores pinch zoom, toolbar changes, and hardware-keyboard focus',()=>{
  const f=fixture();f.focus();f.flush();assert.equal(f.states.at(-1).keyboardOpen,false);
  f.win.visualViewport.height=780;f.win.visualViewport.fire('resize');f.flush();assert.equal(f.states.at(-1).keyboardOpen,false);
  Object.assign(f.win.visualViewport,{height:422,scale:2});const before=f.states.length;
  f.win.visualViewport.fire('resize');f.flush();assert.equal(f.states.length,before);f.stop();
});

test('supports content-resizing keyboards, including without VisualViewport',()=>{
  for(const vv of [true,false]){
    const f=fixture(vv);f.focus();f.win.innerHeight=430;
    if(vv)f.win.visualViewport.height=430;
    f.win.fire('resize');f.flush();assert.equal(f.states.at(-1).height,430);
    f.win.innerHeight=844;if(vv)f.win.visualViewport.height=844;
    f.win.fire('resize');f.flush();assert.equal(f.states.at(-1).keyboardOpen,false);f.stop();
  }
});

test('rotation resets the height baseline; returning from background remeasures',()=>{
  const f=fixture();f.focus();f.win.innerWidth=844;f.win.innerHeight=390;f.win.visualViewport.height=390;
  f.win.fire('orientationchange');f.flush();assert.equal(f.states.at(-1).keyboardOpen,false);
  f.win.visualViewport.height=200;f.win.fire('pageshow');f.flush();assert.equal(f.states.at(-1).height,200);
  f.win.visualViewport.height=390;f.doc.fire('visibilitychange');f.flush();assert.equal(f.states.at(-1).keyboardOpen,false);f.stop();
});

test('mounting with an already focused input measures its keyboard immediately; cleanup cancels work',()=>{
  const f=fixture();f.stop();f.doc.activeElement={tagName:'INPUT',type:'text'};f.win.visualViewport.height=400;
  const states=[];const stop=observeMobileViewport(f.win,s=>states.push(s));assert.equal(states[0].height,400);
  f.win.visualViewport.fire('resize');stop();f.flush();assert.equal(states.length,1);
  assert.equal(f.win.events.size+f.doc.events.size+f.win.visualViewport.events.size,0);
});
