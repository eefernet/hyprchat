import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {transformSync} from 'esbuild';

// Run the component's actual effect; only browser/React primitives and drawing are instrumented.
const source=readFileSync(new URL('./components/BackgroundCanvas.jsx',import.meta.url),'utf8');
const code=transformSync(source.replace(/^import .*;\n/gm,''),{loader:'jsx',format:'cjs'}).code;
function mount({reduced=false,hidden=false,still=false}={}){
  let draws=0,cleanup,refs=0,next=0,width=390;
  const target=()=>({listeners:new Map(),addEventListener(k,f){this.listeners.set(k,f);},removeEventListener(k){this.listeners.delete(k);},fire(k){this.listeners.get(k)?.();}});
  const frames=new Map(),queries=new Map(),ctx=new Proxy({},{get:()=>()=>{},set:()=>true});
  const canvas={getContext:()=>ctx,getBoundingClientRect:()=>({width,height:844})};
  const doc={...target(),hidden};
  const win={...target(),devicePixelRatio:1,matchMedia(q){if(!queries.has(q))queries.set(q,{...target(),matches:q.includes('reduced-motion')?reduced:true});return queries.get(q);}};
  const sandbox={module:{exports:{}},exports:{},React:{createElement:()=>null},useRef:()=>({current:refs++===0?canvas:0}),useEffect:f=>cleanup=f(),
    CANVAS_EFFECTS:['grid'],effectOpacity:()=>1,isLightTheme:()=>false,EFFECT_MODULES:{grid:()=>({seed(){},draw(){draws++;}})},
    window:win,document:doc,performance:{now:()=>100},requestAnimationFrame:f=>{frames.set(++next,f);return next;},cancelAnimationFrame:id=>frames.delete(id)};
  vm.runInNewContext(code,sandbox);sandbox.module.exports.default({effect:'grid',t:{acc:'#ffffff',warm:'#ffffff'},still});
  return {frames,win,doc,queries,draws:()=>draws,cleanup:()=>cleanup(),
    motion(value){const mq=win.matchMedia('(prefers-reduced-motion: reduce)');mq.matches=value;mq.fire('change');},
    hidden(value){doc.hidden=value;doc.fire('visibilitychange');},
    resize(value){width=value;win.fire('resize');},
    frame(){for(const [id,fn] of [...frames]){frames.delete(id);fn(150);}}};
}

test('motion preference toggles stop and restart exactly one animation loop',()=>{
  const f=mount();assert.equal(f.frames.size,1);
  f.motion(true);f.frame();assert.equal(f.frames.size,0);assert.ok(f.draws()>1);
  f.motion(false);f.frame();assert.equal(f.frames.size,1);
  f.motion(false);assert.equal(f.frames.size,1);f.cleanup();assert.equal(f.frames.size,0);
  assert.equal(f.win.listeners.size+f.doc.listeners.size,0);
  for(const mq of f.queries.values())assert.equal(mq.listeners.size,0);
});
test('a reduced-motion still survives resize and starts animating when permitted',()=>{
  const f=mount({reduced:true});assert.equal(f.frames.size,0);const before=f.draws();
  f.resize(320);assert.ok(f.draws()>before);assert.equal(f.frames.size,0);
  f.motion(false);assert.equal(f.frames.size,1);f.cleanup();
});
test('preference changes while hidden never restart frames until visible',()=>{
  const f=mount();f.hidden(true);assert.equal(f.frames.size,0);
  f.motion(true);f.motion(false);assert.equal(f.frames.size,0);
  f.hidden(false);f.hidden(false);assert.equal(f.frames.size,1);f.cleanup();
});
test('mounting hidden does not schedule frames; gallery stills never animate',()=>{
  const hidden=mount({hidden:true});assert.equal(hidden.frames.size,0);hidden.hidden(false);assert.equal(hidden.frames.size,1);hidden.cleanup();
  const f=mount({still:true});f.motion(true);f.motion(false);f.hidden(true);f.hidden(false);
  assert.equal(f.frames.size,0);assert.equal(f.win.listeners.has('pointermove'),false);f.cleanup();
});
