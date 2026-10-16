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
