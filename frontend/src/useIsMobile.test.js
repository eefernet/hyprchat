import test from 'node:test';
import assert from 'node:assert/strict';
import {observeMobileViewport} from './useIsMobile.js';

function fixture(withViewport=true,{standalone=false,legacyStandalone=false,innerHeight=844,viewportHeight=innerHeight}={}) {
  const target=()=>({events:new Map(),addEventListener(k,fn){this.events.set(k,fn);},removeEventListener(k){this.events.delete(k);},fire(k){this.events.get(k)?.();}});
  const doc={...target(),activeElement:null};
  const mode={...target(),matches:standalone};
  let frame=null;
  const win={...target(),document:doc,innerWidth:390,innerHeight,
    navigator:{standalone:legacyStandalone},matchMedia(query){assert.equal(query,'(display-mode: standalone)');return mode;},
    requestAnimationFrame(fn){frame=fn;return 1;},cancelAnimationFrame(){frame=null;},
    visualViewport:withViewport?{...target(),height:viewportHeight,offsetTop:0,scale:1}:undefined};
  const states=[];
  const stop=observeMobileViewport(win,s=>states.push(s));
  return {win,doc,mode,states,stop,flush(){const f=frame;frame=null;f?.();},focus(){doc.activeElement={tagName:'TEXTAREA'};doc.fire('focusin');}};
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
  assert.equal(f.win.events.size+f.doc.events.size+f.win.visualViewport.events.size+f.mode.events.size,0);
});

test('standalone launch measures the usable viewport through either install signal',()=>{
  for(const install of [{standalone:true},{legacyStandalone:true}]){
    const f=fixture(true,{...install,innerHeight:874,viewportHeight:812});
    assert.deepEqual(f.states.at(-1),{height:812,offsetTop:0,keyboardOpen:false});
    f.focus();f.flush(); // The status-bar difference is not a software keyboard.
    assert.deepEqual(f.states.at(-1),{height:812,offsetTop:0,keyboardOpen:false});
    f.stop();
  }
});

test('standalone keyboard dismissal restores the measured height, safe-area state, and origin',()=>{
  const f=fixture(true,{standalone:true,innerHeight:874,viewportHeight:812});
  f.focus();Object.assign(f.win.visualViewport,{height:420,offsetTop:37});
  f.win.visualViewport.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:420,offsetTop:37,keyboardOpen:true});
  f.doc.activeElement=null;f.doc.fire('focusout');f.flush();
  assert.equal(f.states.at(-1).keyboardOpen,true);
  f.win.visualViewport.height=812;f.win.visualViewport.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:812,offsetTop:0,keyboardOpen:false});
  f.stop();
});

test('standalone remeasures idle height after rotation and background return',()=>{
  const f=fixture(true,{standalone:true,innerHeight:874,viewportHeight:812});
  f.win.innerWidth=874;f.win.innerHeight=402;f.win.visualViewport.height=381;
  f.win.fire('orientationchange');f.flush();
  assert.deepEqual(f.states.at(-1),{height:381,offsetTop:0,keyboardOpen:false});
  f.win.visualViewport.height=365;f.win.fire('pageshow');f.flush();
  assert.equal(f.states.at(-1).height,365);
  f.win.visualViewport.height=381;f.doc.fire('visibilitychange');f.flush();
  assert.equal(f.states.at(-1).height,381);
  const count=f.states.length;
  Object.assign(f.win.visualViewport,{height:190,scale:2});f.win.visualViewport.fire('resize');f.flush();
  assert.equal(f.states.length,count);
  Object.assign(f.win.visualViewport,{height:381,scale:1});f.win.visualViewport.fire('resize');f.flush();
  assert.equal(f.states.at(-1).height,381);
  f.stop();
});

test('standalone falls back to innerHeight without VisualViewport and handles resizing keyboards',()=>{
  const f=fixture(false,{legacyStandalone:true});
  assert.deepEqual(f.states.at(-1),{height:844,offsetTop:0,keyboardOpen:false});
  f.focus();f.win.innerHeight=430;f.win.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:430,offsetTop:0,keyboardOpen:true});
  f.win.innerHeight=844;f.win.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:844,offsetTop:0,keyboardOpen:false});
  f.stop();
});

test('browser toolbar changes stay CSS-owned and a display-mode change remeasures',()=>{
  const f=fixture(true,{innerHeight:874,viewportHeight:812});
  f.win.visualViewport.height=760;f.win.visualViewport.fire('resize');f.flush();
  assert.deepEqual(f.states.at(-1),{height:null,offsetTop:0,keyboardOpen:false});
  f.mode.matches=true;f.mode.fire('change');f.flush();
  assert.deepEqual(f.states.at(-1),{height:760,offsetTop:0,keyboardOpen:false});
  f.mode.matches=false;f.mode.fire('change');f.flush();
  assert.deepEqual(f.states.at(-1),{height:null,offsetTop:0,keyboardOpen:false});
  f.stop();
});
