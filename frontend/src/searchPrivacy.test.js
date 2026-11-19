import test from 'node:test';
import assert from 'node:assert/strict';

test('source icons and thumbnails stay on the authenticated local proxy',async()=>{
  const oldWindow=globalThis.window,oldStorage=globalThis.localStorage;
  globalThis.localStorage={getItem:()=>null};
  globalThis.window={location:{origin:'https://chat.test'},dispatchEvent:()=>{},fetch:async()=>new Response('')};
  try{
    const {sourceFaviconUrl,proxiedImageUrl,hcSetUserContext}=await import('./session.js');
    hcSetUserContext('alice','token-a');
    const icon=new URL(sourceFaviconUrl('https://www.example.org/private/path?q=secret'),'https://chat.test');
    assert.equal(icon.origin,'https://chat.test');
    assert.equal(icon.pathname,'/api/img-proxy');
    assert.equal(icon.searchParams.get('u'),'https://www.example.org/favicon.ico');
    assert.equal(icon.searchParams.get('user_id'),'alice');
    assert.equal(icon.searchParams.get('session'),'token-a');
    assert.equal(icon.href.includes('google.com'),false);
    assert.equal(icon.href.includes('secret'),false);
    hcSetUserContext('bob','token-b');
    const image=new URL(proxiedImageUrl('https://images.example.org/preview.png'),'https://chat.test');
    assert.equal(image.pathname,'/api/img-proxy');
    assert.equal(image.searchParams.get('user_id'),'bob');
    assert.equal(image.searchParams.get('session'),'token-b');
    assert.equal(sourceFaviconUrl(''), '');
    assert.equal(sourceFaviconUrl('https://user:pass@example.org'), '');
    assert.equal(sourceFaviconUrl('javascript:alert(1)'), '');
  }finally{globalThis.window=oldWindow;globalThis.localStorage=oldStorage;}
});
