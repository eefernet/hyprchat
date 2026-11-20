import React,{useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {withLinkGuard,readFrameMessage,linkNote,LEAVE_WAIT_MS} from '../previewLinks.js';
import {IC} from './icons.jsx';
import './htmlPreview.css';

let serial=0;
const LEFT_NOTE='The page tried to open another page, so the preview was put back.';

// The sandboxed frame a page is previewed in, for the live HTML preview and for a delivered .html file.
// It adds the link guard (previewLinks.js) to the page and is the parent side of it:
//   - a "#section" link scrolls inside the page (the guard does that on its own);
//   - any other link is stopped, and a line above the page says so. A web address is offered as a
//     link the reader clicks; the page itself can never open anything;
//   - a page that leaves anyway (a script setting location, a meta refresh) is put back once. If it
//     leaves again the preview stops with a Reload button instead of going round in a loop.
// Without the guard the frame loaded HyprChat itself on the first nav click (2026-11-19).
export default function PreviewFrame({doc,title,width=0,reloadKey=0,sandbox='allow-scripts'}){
  const frame=useRef(null);
  const [bounces,setBounces]=useState(0),[restarts,setRestarts]=useState(0),[note,setNote]=useState(null);
  // One token per document put into the frame: it ties the guard's messages to that document.
  const token=useMemo(()=>`hc${++serial}-${Math.random().toString(36).slice(2,8)}`,[doc,reloadKey,bounces,restarts]);
  const guarded=useMemo(()=>withLinkGuard(doc,token),[doc,token]);
  // The token of the document that is in the frame now, and the timer that runs after it said "leaving".
  const current=useRef(token),leaving=useRef(0);
  useLayoutEffect(()=>{
    current.current=token;
    return()=>{clearTimeout(leaving.current);leaving.current=0;};
  },[token]);

  // New content or a reload by the reader starts over.
  useEffect(()=>{setBounces(0);setNote(null);},[doc,reloadKey]);

  useEffect(()=>{
    const onMessage=event=>{
      // A sandboxed page has no origin of its own, which reads as "null" here.
      if(!frame.current||event.origin!=='null')return;
      const message=readFrameMessage(event.data,current.current);
      if(!message)return;
      // Messages come from our frame. The one exception is "leaving": it is delivered after the
      // document that sent it is gone, so the browser gives it no sender (seen in Chromium); the
      // token, which only that document knew, stands in for it.
      const fromFrame=event.source===frame.current.contentWindow;
      if(!fromFrame&&!(message.kind==='leaving'&&event.source===null))return;
      if(message.kind==='ready'){clearTimeout(leaving.current);leaving.current=0;return;}
      if(message.kind==='leaving'){
        clearTimeout(leaving.current);
        leaving.current=setTimeout(()=>{leaving.current=0;setNote({kind:'left'});setBounces(count=>count+1);},LEAVE_WAIT_MS);
        return;
      }
      setNote(message);
    };
    window.addEventListener('message',onMessage);
    return()=>window.removeEventListener('message',onMessage);
  },[]);

  // Notes that offer nothing to click go away by themselves.
  useEffect(()=>{
    if(!note||note.kind==='external')return undefined;
    const timer=setTimeout(()=>setNote(current=>current===note?null:current),7000);
    return()=>clearTimeout(timer);
  },[note]);

  const stopped=bounces>=2;
  return <>
    {note&&!stopped&&<div className="hp-note" role="status">
      <span className="hp-note-text">{note.kind==='left'?LEFT_NOTE:linkNote(note)}</span>
      {note.kind==='external'&&<a href={note.url} target="_blank" rel="noopener noreferrer" title={note.url} onClick={()=>setNote(null)}>Open in new tab</a>}
      <button type="button" onClick={()=>setNote(null)} aria-label="Dismiss" title="Dismiss"><IC.X/></button>
    </div>}
    {stopped
      ?<div className="hp-halt" role="status">
        <p>This page keeps trying to open another page, so the preview was stopped.</p>
        <button type="button" onClick={()=>{setNote(null);setBounces(0);setRestarts(count=>count+1);}}>Reload preview</button>
      </div>
      :<div className="hp-stage">
        <iframe key={`${reloadKey}:${restarts}:${bounces}`} ref={frame} className="hp-frame" sandbox={sandbox} referrerPolicy="no-referrer" srcDoc={guarded}
          title={title} style={{width:width?`${width}px`:'100%',maxWidth:'100%'}}/>
      </div>}
  </>;
}
