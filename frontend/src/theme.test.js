import test from 'node:test';
import assert from 'node:assert/strict';
import {THEMES,UI_THEMES,contrastRatio,presentationTokens,FONTS} from './theme.js';

test('interface text has at least 4.5:1 contrast on every shared surface in every theme',()=>{
  for(const [name,t] of Object.entries(UI_THEMES)){
    for(const text of ['text','dim','mut','acc','acc2','warm','ok','err','pink','f1','f4']){
      for(const surface of ['bg','bgDeep','surface','sfHov','sfBri']){
        assert.ok(contrastRatio(t[text],t[surface])>=4.5,`${name}: ${text} on ${surface}`);
      }
    }
    for(const surface of ['bg','bgDeep','surface','sfHov','sfBri','brd'])assert.equal(t[surface],THEMES[name][surface]);
  }
});
test('selected monospace font applies to interface and code without changing UI scale',()=>{
  for(const {v:font} of FONTS){
    const tokens=presentationTokens(UI_THEMES.hyprflat,font,font,1.5);
    assert.equal(tokens['--hc-font-code'],font);
    assert.equal(tokens['--hc-font-ui'],font);
    assert.equal(tokens['--hc-ui-scale'],1.5);
  }
});

test('theme-derived code tokens meet 4.5:1 across all UI themes',()=>{
  const lum=hex=>hex.slice(1).match(/../g).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((s,x,i)=>s+x*[.2126,.7152,.0722][i],0);
  for(const theme of Object.values(UI_THEMES)){
    const tokens=presentationTokens(theme,'mono','mono');
    for(const [key,color] of Object.entries(tokens).filter(([key])=>key.startsWith('--code-'))){
      const a=lum(color),b=lum(theme.bgDeep);
      assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5,`${theme.name}: ${key}`);
    }
  }
});
