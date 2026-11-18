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

test('the warning amber and deleted diff lines are readable in every theme',async()=>{
  const {degradedColor,codeThemeTokens,contrastRatio,UI_THEMES}=await import('./theme.js');
  for(const [name,theme] of Object.entries(UI_THEMES)){
    const amber=degradedColor(theme);
    for(const surface of [theme.bg,theme.bgDeep,theme.surface])
      assert.ok(contrastRatio(amber,surface)>=4.5,`${name}: warning text ${amber} on ${surface}`);
    assert.equal(degradedColor(theme),amber);                       // cached per theme
    assert.ok(contrastRatio(codeThemeTokens(theme)['--code-deleted'],theme.bgDeep)>=4.5,`${name}: deleted diff line`);
  }
  // The raw amber stays as it is on dark themes where it already reads well.
  assert.equal(degradedColor(UI_THEMES.midnight).toLowerCase(),'#f0a030');
});

test('fixed badge colours become readable text on every theme',async()=>{
  const {readableOn,contrastRatio,UI_THEMES}=await import('./theme.js');
  for(const [name,theme] of Object.entries(UI_THEMES)){
    for(const badge of ['#eab308','#2ecc71','#c792ea','#3498db','#ff6600','#888888']){
      const text=readableOn(theme,badge);
      for(const surface of [theme.bg,theme.bgDeep,theme.surface])
        assert.ok(contrastRatio(text,surface)>=4.5,`${name}: ${badge} as ${text} on ${surface}`);
    }
  }
  assert.equal(readableOn(UI_THEMES.nord,'var(--hc-accent)'),'var(--hc-accent)');
  assert.equal(readableOn(UI_THEMES.nord,'#888'),'#888');
});
