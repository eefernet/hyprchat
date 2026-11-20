// Links inside a preview frame.
//
// A previewed page is handed to its iframe as text (srcDoc), so it has no address of its own:
// the browser resolves every link against HyprChat's address. href="#home" becomes
// https://<hyprchat>/#home, which is another page than the frame's, and the frame then loads
// HyprChat itself. The page cannot correct that (the preview policy has base-uri 'none').
//
// linkGuard runs inside the frame. It keeps "#section" links in the page and tells the parent
// about every other link instead of following it. PreviewFrame.jsx is the parent side.

// Runs inside the frame, embedded as text: it must not use anything outside its own body.
export function linkGuard(token){
  var send=function(type,href){
    try{parent.postMessage({hcPreview:1,type:type,token:token,href:href||''},'*');}catch(error){}
  };
  var find=function(name){
    if(!name||name==='top')return null;
    var found=document.getElementById(name);
    if(found)return found;
    var named=document.getElementsByName(name);
    return named&&named.length?named[0]:null;
  };
  var jump=function(hash){
    var name=hash.slice(1);
    try{name=decodeURIComponent(name);}catch(error){}
    var target=find(name);
    // The frame's own address plus the fragment: the browser scrolls, :target and hashchange work.
    try{if(location.hash!==hash)location.hash=hash;}catch(error){}
    if(target&&target.scrollIntoView)target.scrollIntoView();
    else if(!name||name==='top')window.scrollTo(0,0);
  };
  send('ready');
  // Sent as this document is replaced. A "ready" follows when the page only reloaded itself; when
  // none does, the frame is showing something else (a script set location, a meta refresh).
  window.addEventListener('pagehide',function(){send('leaving');});
  window.addEventListener('click',function(event){
    // A page that handles its own links (smooth scrolling, tabs) is left alone.
    if(event.defaultPrevented)return;
    var node=event.target;
    while(node&&node.nodeType===1&&!(node.getAttribute&&node.getAttribute('href')!==null&&/^(a|area)$/i.test(node.tagName||'')))node=node.parentNode;
    if(!node||node.nodeType!==1)return;
    var href=String(node.getAttribute('href')||'').trim();
    event.preventDefault();
    if(href===''||href.charAt(0)==='#'){jump(href||'#');return;}
    if(/^javascript:/i.test(href))return;
    send('link',href);
  });
}

function guardTag(token){
  // "<" is written as an escape so no token can close the script tag.
  return `<script>(${linkGuard.toString()})(${JSON.stringify(String(token)).replace(/</g,'\\u003c')})</script>`;
}

// The guard goes first in <head>, so it is listening before any of the page's own code runs.
export function withLinkGuard(html='',token=''){
  const tag=guardTag(token);
  const source=String(html||'');
  if(/<head[^>]*>/i.test(source))return source.replace(/<head[^>]*>/i,match=>`${match}${tag}`);
  if(/<html[^>]*>/i.test(source))return source.replace(/<html[^>]*>/i,match=>`${match}<head>${tag}</head>`);
  return `<!doctype html><html><head>${tag}</head><body>${source}</body></html>`;
}

// What a message from the frame means. The page can post anything, so nothing here is trusted
// beyond "show this to the user": a web address is only ever offered as a link they click.
export function readFrameMessage(data,token=''){
  if(!data||typeof data!=='object'||data.hcPreview!==1)return null;
  if(String(data.token||'')!==String(token))return null;
  if(data.type==='ready')return {kind:'ready'};
  if(data.type==='leaving')return {kind:'leaving'};
  if(data.type!=='link'||typeof data.href!=='string')return null;
  const href=data.href.trim().slice(0,2000);
  if(!href)return null;
  if(/^https?:\/\//i.test(href)){
    try{
      const url=new URL(href);
      if(url.protocol==='http:'||url.protocol==='https:')return {kind:'external',url:url.href,host:url.hostname.replace(/^www\./,'')};
    }catch{}
    return null;
  }
  if(/^(mailto|tel):/i.test(href))return {kind:'other',href,what:'contact'};
  return {kind:'other',href,what:'page'};
}

export function linkNote(link){
  if(!link)return '';
  if(link.kind==='external')return `This page links to ${link.host}.`;
  if(link.what==='contact')return 'Mail and phone links do not open from a preview.';
  return 'Links to other pages do not work in a preview.';
}

// How long a "ready" may take to follow a "leaving" before the frame counts as gone. The guard is the
// first thing in the page, so a reload answers in a few milliseconds; this is generous on purpose.
// (The frame's load event cannot be used for this: it does not say which document loaded, and it
// arrives before or after the guard's "ready" depending on the browser's process layout.)
export const LEAVE_WAIT_MS=900;
