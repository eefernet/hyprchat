import test from 'node:test';
import assert from 'node:assert/strict';
import {linkGuard,withLinkGuard,readFrameMessage,linkNote} from './previewLinks.js';
import {withPreviewCsp} from './htmlPreview.js';

// A stand-in for the frame: enough of window / document / location for the guard to run in.
function frame({ids={},names={},hash=''}={}){
  const posted=[],scrolled=[];let listener=null,hide=null;
  const element=name=>({name,scrollIntoView(){scrolled.push(name);}});
  const world={
    parent:{postMessage:(message,origin)=>posted.push({message,origin})},
    document:{
      getElementById:id=>ids[id]?element(id):null,
      getElementsByName:name=>names[name]?[element(name)]:[],
    },
    location:{hash},
    window:{addEventListener:(type,fn)=>{if(type==='click')listener=fn;if(type==='pagehide')hide=fn;},scrollTo:(x,y)=>scrolled.push(`top:${x},${y}`)},
  };
  // The guard is shipped as text, so run it from its text here too.
  new Function('parent','document','location','window',`(${linkGuard.toString()})("tok")`)(world.parent,world.document,world.location,world.window);
  const link=(href,tag='A')=>({nodeType:1,tagName:tag,getAttribute:name=>name==='href'?href:null,parentNode:null});
  const click=(target,prevented=false)=>{
    const event={target,defaultPrevented:prevented,stopped:false,preventDefault(){this.stopped=true;}};
    listener(event);return event;
  };
  return {posted,scrolled,world,link,click,leave:()=>hide()};
}

test('the guard says it is ready, with its token',()=>{
  const f=frame();
  assert.deepEqual(f.posted,[{message:{hcPreview:1,type:'ready',token:'tok',href:''},origin:'*'}]);
});

test('a #section link stays in the page: no navigation, the frame address gets the fragment',()=>{
  const f=frame({ids:{core:true}});
  const event=f.click(f.link('#core'));
  assert(event.stopped);
  assert.equal(f.world.location.hash,'#core');
  assert.deepEqual(f.scrolled,['core']);
  assert.equal(f.posted.length,1,'nothing is reported for an in-page link');
});

test('a click inside the link (an icon or span) counts as the link',()=>{
  const f=frame({ids:{core:true}});
  const anchor=f.link('#core');
  const span={nodeType:1,tagName:'SPAN',getAttribute:()=>null,parentNode:anchor};
  assert(f.click(span).stopped);
  assert.deepEqual(f.scrolled,['core']);
});

test('named anchors, encoded names, # and #top',()=>{
  const f=frame({names:{old:true},ids:{'my section':true}});
  f.click(f.link('#old'));f.click(f.link('#my%20section'));f.click(f.link('#'));f.click(f.link('#top'));f.click(f.link(''));
  assert.deepEqual(f.scrolled,['old','my section','top:0,0','top:0,0','top:0,0']);
});

test('a missing target does nothing and never leaves the page',()=>{
  const f=frame();
  assert(f.click(f.link('#nowhere')).stopped);
  assert.deepEqual(f.scrolled,[]);
});

test('every other link is stopped and reported, never followed',()=>{
  const f=frame();
  for(const href of ['/','index.html','./about.html','https://example.com/x','mailto:a@b.c']){
    assert(f.click(f.link(href)).stopped,href);
  }
  assert.deepEqual(f.posted.slice(1).map(entry=>[entry.message.type,entry.message.href]),
    [['link','/'],['link','index.html'],['link','./about.html'],['link','https://example.com/x'],['link','mailto:a@b.c']]);
});

test('javascript: links are stopped and not reported',()=>{
  const f=frame();
  assert(f.click(f.link('javascript:void(0)')).stopped);
  assert.equal(f.posted.length,1);
});

test('a click the page already handled is left alone',()=>{
  const f=frame({ids:{core:true}});
  const event=f.click(f.link('#core'),true);
  assert(!event.stopped);
  assert.deepEqual(f.scrolled,[]);assert.equal(f.world.location.hash,'');
});

test('clicks that are not on a link are left alone',()=>{
  const f=frame();
  const button={nodeType:1,tagName:'BUTTON',getAttribute:()=>null,parentNode:{nodeType:9}};
  assert(!f.click(button).stopped);
  // An element that merely has an href attribute (svg <use>, <link>) is not a link.
  assert(!f.click({nodeType:1,tagName:'use',getAttribute:()=>'#icon',parentNode:null}).stopped);
});

test('the guard is the first thing in head, in front of the policy, in all three document shapes',()=>{
  const full=withLinkGuard(withPreviewCsp('<html><head><title>x</title></head><body></body></html>'),'t1');
  assert.match(full,/^<html><head><script>\(function linkGuard\(token\)[\s\S]+\)\("t1"\)<\/script><meta http-equiv="Content-Security-Policy"/);
  assert.match(withLinkGuard('<html lang="en"><body>b</body></html>','t'),/^<html lang="en"><head><script>[\s\S]+<\/script><\/head><body>b/);
  assert.match(withLinkGuard('<p>frag</p>','t'),/^<!doctype html><html><head><script>[\s\S]+<\/script><\/head><body><p>frag<\/p><\/body><\/html>$/);
});

test('the embedded guard is self-contained and cannot be closed early by its token',()=>{
  const doc=withLinkGuard('<p>x</p>','a"b');
  const script=doc.match(/<script>([\s\S]+?)<\/script>/)[1];
  assert.doesNotThrow(()=>new Function('parent','document','location','window',script));
  assert.equal((doc.match(/<\/script>/g)||[]).length,1);
});

test('messages: only ours, only with the current token',()=>{
  assert.equal(readFrameMessage(null,'t'),null);
  assert.equal(readFrameMessage('ready','t'),null);
  assert.equal(readFrameMessage({type:'ready',token:'t'},'t'),null,'no marker');
  assert.equal(readFrameMessage({hcPreview:1,type:'ready',token:'old'},'t'),null,'another document');
  assert.deepEqual(readFrameMessage({hcPreview:1,type:'ready',token:'t'},'t'),{kind:'ready'});
  assert.equal(readFrameMessage({hcPreview:1,type:'link',token:'t',href:7},'t'),null);
  assert.equal(readFrameMessage({hcPreview:1,type:'link',token:'t',href:'  '},'t'),null);
  assert.equal(readFrameMessage({hcPreview:1,type:'open',token:'t',href:'https://x.y'},'t'),null);
});

test('messages: a web address is offered, anything else is only described',()=>{
  const read=href=>readFrameMessage({hcPreview:1,type:'link',token:'t',href},'t');
  assert.deepEqual(read('https://www.example.com/a?b=1'),{kind:'external',url:'https://www.example.com/a?b=1',host:'example.com'});
  assert.equal(read('HTTP://Example.com').kind,'external');
  assert.equal(read('https://'),null,'not an address');
  assert.deepEqual(read('index.html'),{kind:'other',href:'index.html',what:'page'});
  assert.deepEqual(read('/'),{kind:'other',href:'/',what:'page'});
  assert.equal(read('mailto:a@b.c').what,'contact');
  // Never something a click could run or read a local file with.
  for(const href of ['javascript:alert(1)','data:text/html,x','file:///etc/passwd','//evil.example/x']){
    assert.notEqual(read(href)?.kind,'external',href);
  }
  assert.equal(read('x'.repeat(5000)).href.length,2000);
});

test('the note says what happened',()=>{
  assert.equal(linkNote({kind:'external',host:'example.com'}),'This page links to example.com.');
  assert.match(linkNote({kind:'other',what:'page'}),/other pages/);
  assert.match(linkNote({kind:'other',what:'contact'}),/Mail and phone/);
  assert.equal(linkNote(null),'');
});

test('the guard says when its document is being replaced',()=>{
  const f=frame();
  f.leave();
  assert.deepEqual(f.posted.map(entry=>entry.message.type),['ready','leaving']);
  assert.deepEqual(readFrameMessage(f.posted[1].message,'tok'),{kind:'leaving'});
  assert.equal(readFrameMessage(f.posted[1].message,'newer'),null,'a document that was replaced by a newer one is not "leaving"');
});
