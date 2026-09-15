import test from 'node:test';
import assert from 'node:assert/strict';
import Prism from './prism-languages.js';
import { codeTokenStyle, highlightCode, normalizeCodeLanguage, parseCodeFence } from './syntaxHighlight.js';
import { THEMES } from './theme.js';

test('language aliases and metadata do not enter the code body',()=>{
  for(const [label,lang] of [['Swift','swift'],['JS','javascript'],['py','python'],['c++','cpp'],['C#','csharp'],['shell','bash']]){
    const code='  x < y && y > 0\n  return "hello"';
    assert.deepEqual(parseCodeFence('```'+label+' filename=example\n'+code+'\n```'),{lang,code});
  }
  assert.deepEqual(parseCodeFence('```\nhello\n```'),{lang:'',code:'hello'});
  assert.deepEqual(parseCodeFence('```Swift\r\nlet x = 1\r\n```'),{lang:'swift',code:'let x = 1'});
  assert.equal(normalizeCodeLanguage('<script>'),'');
  assert.equal(normalizeCodeLanguage('constructor'),'constructor');
  assert.equal(normalizeCodeLanguage('__proto__'),'__proto__');
});

test('Swift and existing languages emit token colors, including incomplete streams',()=>{
  for(const [lang,code] of [['swift','func example() -> String { return "hello" }'],['python','def example():\n    return "hello"'],['js','function example() { return "hello"; }']]){
    const html=highlightCode(code,lang,Prism);
    assert.match(html,/token keyword/);
    assert.match(html,/token string/);
    assert.ok(highlightCode(code.slice(0,-4),lang,Prism));
  }
});

test('code cannot insert raw HTML; missing grammars fall back to React text',()=>{
  const source='const example = "<img src=x onerror=alert(1)><script>alert(1)</script>";';
  const html=highlightCode(source,'javascript',Prism);
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.equal(highlightCode(source,'unlisted-language',Prism),null);
  assert.equal(highlightCode(source,'constructor',Prism),null);
  assert.equal(highlightCode(source,'swift',null),null);
});

test('light and dark token palettes remain legible against every bundled theme',()=>{
  const luminance=hex=>{
    const rgb=hex.slice(1).match(/../g).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
  };
  for(const theme of Object.values(THEMES)){
    const background=luminance(theme.bgDeep);
    for(const [kind,color] of Object.entries(codeTokenStyle(theme.bgDeep))){
      const foreground=luminance(color);
      const contrast=(Math.max(background,foreground)+.05)/(Math.min(background,foreground)+.05);
      assert.ok(contrast>=4.5,`${theme.name} ${kind}: ${contrast}`);
    }
  }
});
