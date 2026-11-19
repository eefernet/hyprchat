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

// 2026-11-18: four interface fonts were added. The saved choice is a position in UI_FONTS, so the
// first three must stay where they are or an existing user's font silently changes.
test('interface fonts keep their positions and each one can be loaded', async () => {
  const {UI_FONTS} = await import('./theme.js');
  assert.deepEqual(UI_FONTS.slice(0, 3).map(f => f.name), ['Inter', 'Geist', 'System']);
  assert.deepEqual(UI_FONTS.slice(3).map(f => f.name), ['Figtree', 'Hanken Grotesk', 'Instrument Sans', 'DM Sans']);
  for (const font of UI_FONTS) {
    assert.ok(font.name && /sans-serif$/.test(font.v), font.name);
    if (font.i !== null) {
      assert.match(font.i, /^[A-Za-z+]+:wght@400;500;600;700$/, font.name);      // a Google Fonts family spec, spaces as "+"
      assert.equal(font.i.split(':')[0].replace(/\+/g, ' '), font.name);
      assert.ok(font.v.includes(`'${font.name}'`), font.name);
    }
  }
});

// 2026-11-19: the interface starts in a sans face. A browser that only ever had the old default is
// switched once; a sans choice is kept; choosing "match code font" after the switch sticks.
test('the interface font default is sans, once, without overriding a real choice', async () => {
  const {defaultUiFont, DEFAULT_UI_FONT, UI_FONTS} = await import('./theme.js');
  assert.equal(UI_FONTS[DEFAULT_UI_FONT].name, 'Figtree');
  assert.equal(defaultUiFont(null, false), DEFAULT_UI_FONT);       // new browser
  assert.equal(defaultUiFont('-1', false), DEFAULT_UI_FONT);       // the old default, written on first load
  assert.equal(defaultUiFont('0', false), 0);                      // chose Inter: kept
  assert.equal(defaultUiFont('2', true), 2);
  assert.equal(defaultUiFont('-1', true), -1);                     // chose monospace after the switch: kept
  assert.equal(defaultUiFont('99', false), DEFAULT_UI_FONT);       // a position that no longer exists
});
