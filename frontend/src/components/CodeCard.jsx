import React,{createContext,useContext,useEffect,useId,useMemo,useRef,useState,useSyncExternalStore} from 'react';
import {codeLines,codeDiffKind} from '../syntaxHighlight.js';
import {IC} from './icons.jsx';
import '../codeBlocks.css';

// One preference for all mounted cards, including memoized historical replies.
const wrapListeners=new Set();
let memoryWrap=false;
const readWrap=()=>{try{return localStorage.getItem('hc-code-wrap')==='true';}catch{return memoryWrap;}};
const emitWrap=()=>wrapListeners.forEach(fn=>fn());
const subscribeWrap=fn=>{
  wrapListeners.add(fn);
  const storage=e=>{if(e.key==='hc-code-wrap'||e.key===null)fn();};
  window.addEventListener('storage',storage);
  return()=>{wrapListeners.delete(fn);window.removeEventListener('storage',storage);};
};
const Visit=createContext(null),Message=createContext(null);

// Lives above mdStream -> MemoMD so finalizing an answer cannot collapse it.
// The caller keys this provider by user/conversation; revisiting resets choices.
export function CodeCardSession({children}){
  const states=useRef(new Map());
  return <Visit.Provider value={states.current}>{children}</Visit.Provider>;
}
export function CodeMessageBoundary({identity,live=false,children}){
  const value=useMemo(()=>({identity,live}),[identity,live]);
  return <Message.Provider value={value}>{children}</Message.Provider>;
}

export default function CodeCard({code,lang='',filename='',blockKey,streaming=false}){
  const visit=useContext(Visit),message=useContext(Message),local=useRef(new Map());
  const stateMap=visit&&message?visit:local.current;
  const key=`${message?.identity||'standalone'}:${blockKey}`;
  if(!stateMap.has(key))stateMap.set(key,{expanded:streaming||!!message?.live?true:undefined});
  const saved=stateMap.get(key);
  const live=streaming||!!message?.live;
  if(live&&!saved.live)saved.expanded=true;
  saved.live=live;
  const [,refresh]=useState(0),[copyState,setCopyState]=useState('');
  const copyTimer=useRef(0),mounted=useRef(true),bodyId=useId();
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;clearTimeout(copyTimer.current);};},[]);
  const wrap=useSyncExternalStore(subscribeWrap,readWrap,()=>false);
  const sourceLines=useMemo(()=>String(code).split('\n'),[code]);
  const lines=useMemo(()=>codeLines(code,lang,window.Prism),[code,lang]);
  const long=lines.length>40,expanded=saved.expanded??!long;
  const visible=expanded?lines:lines.slice(0,32),diff=lang==='diff';
  const toggleWrap=()=>{memoryWrap=!wrap;try{localStorage.setItem('hc-code-wrap',String(memoryWrap));}catch{}emitWrap();};
  const toggleExpanded=()=>{saved.expanded=!expanded;refresh(n=>n+1);};
  const copy=async()=>{
    try{
      await navigator.clipboard.writeText(code);
      if(!mounted.current)return;
      setCopyState('Copied');
    }catch{if(mounted.current)setCopyState('Copy failed');}
    clearTimeout(copyTimer.current);copyTimer.current=setTimeout(()=>{if(mounted.current)setCopyState('');},2200);
  };
  return <section className={`hc-code-card${wrap?' is-wrapped':''}${lines.length<3?' is-short':''}`} aria-label={`${filename||lang||'Code'} code block`}>
    <div className="hc-code-header">
      <span className="hc-code-language"><IC.Terminal/>{lang||'code'}</span>
      {filename&&<span className="hc-code-filename" title={filename}>{filename}</span>}
      <span className="hc-code-count">{lines.length} {lines.length===1?'line':'lines'}</span>
      <div className="hc-code-actions">
        <button type="button" aria-pressed={wrap} onClick={toggleWrap} title="Wrap long lines">Wrap</button>
        <button type="button" onClick={copy} aria-label="Copy code">{copyState==='Copied'?<IC.Check/>:<IC.Copy/>}<span>{copyState||'Copy'}</span></button>
      </div>
      <span className="hc-sr-only" role="status">{copyState}</span>
    </div>
    <div id={bodyId} className="hc-code-scroll" tabIndex={0} aria-label="Code">
      <pre className="hc-code"><code>{visible.map((tokens,i)=>{
        const kind=diff?codeDiffKind(sourceLines[i]):'';
        const marker=kind==='add'?'+':kind==='delete'?'−':kind==='hunk'?'@@':'';
        return <span key={i} className={`hc-code-line${kind?` is-${kind}`:''}`}>
          <span className="hc-code-number" aria-hidden="true">{i+1}</span>
          {diff&&<span className="hc-code-diff-marker" aria-hidden="true">{marker}</span>}
          <span className="hc-code-source">{tokens.map((token,j)=><span key={j} className={token.classes.join(' ')||undefined}>{token.text}</span>)}{i<visible.length-1?'\n':null}</span>
        </span>;
      })}</code></pre>
    </div>
    {long&&<button type="button" className="hc-code-expand" aria-expanded={expanded} aria-controls={bodyId} onClick={toggleExpanded}>{expanded?'Collapse code':`Show all ${lines.length} lines`}<IC.ChevDown/></button>}
  </section>;
}
