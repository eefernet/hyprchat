import {useCallback,useEffect,useRef,useState} from 'react';
import {draggedWidth,keyWidth} from './paneResize.js';

// The drag handle on the right-hand pane's left edge.
//
// Movement over an iframe (the preview is one) goes to the framed page and never reaches HyprChat.
// The old handle listened for mousemove on the window, so dragging over the preview did nothing and
// the width only jumped once the cursor came back out (2026-11-19). Two things now keep the drag
// here, and either is enough on its own:
//   - the handle captures the pointer, so the browser sends it every move and the release;
//   - while a drag runs the pane is marked data-resizing and its frames let the pointer through
//     (htmlPreview.css), so the window sees the movement even where a frame is underneath.
//
// Returns the ref for the pane, whether a drag is running (the pane also turns off its width
// transition while it is), and the props for the handle element.
export default function usePaneResize(width,setWidth,onDone){
  const pane=useRef(null),drag=useRef(null);
  const [resizing,setResizing]=useState(false);

  const measure=()=>{
    const element=pane.current;
    if(!element)return {scale:1,room:0};
    let scale=Number(element.currentCSSZoom)||0;
    if(!scale){try{scale=parseFloat(getComputedStyle(element).getPropertyValue('--hc-ui-scale'))||1;}catch{scale=1;}}
    const beside=element.previousElementSibling;
    return {scale,room:element.offsetWidth+(beside?beside.offsetWidth:0)};
  };

  const restoreBody=held=>{
    if(!held)return;
    document.body.style.userSelect=held.select;document.body.style.cursor=held.cursor;
  };

  const stopListening=useRef(()=>{});
  const finish=useCallback(event=>{
    const held=drag.current;
    if(!held||(event&&event.pointerId!==undefined&&event.pointerId!==held.id))return;
    drag.current=null;
    stopListening.current();
    restoreBody(held);
    try{held.handle?.releasePointerCapture?.(held.id);}catch{}
    setResizing(false);
    onDone?.(held.last);
  },[onDone]);

  // A pane closed in the middle of a drag must not leave the page unselectable or listening.
  useEffect(()=>()=>{stopListening.current();restoreBody(drag.current);drag.current=null;},[]);

  const onMove=useCallback(event=>{
    const held=drag.current;
    if(!held||event.pointerId!==held.id)return;
    const next=draggedWidth(held.startWidth,held.startX,event.clientX,held.scale,held.room);
    if(next!==held.last){held.last=next;setWidth(next);}
  },[setWidth]);

  const onPointerDown=event=>{
    if(event.pointerType==='mouse'&&event.button!==0)return;
    if(drag.current)finish();
    event.preventDefault();
    const {scale,room}=measure();
    drag.current={id:event.pointerId,handle:event.currentTarget,startX:event.clientX,startWidth:width,last:width,scale,room,
      select:document.body.style.userSelect,cursor:document.body.style.cursor};
    try{event.currentTarget.setPointerCapture(event.pointerId);}catch{}
    window.addEventListener('pointermove',onMove);
    window.addEventListener('pointerup',finish);
    window.addEventListener('pointercancel',finish);
    stopListening.current=()=>{
      window.removeEventListener('pointermove',onMove);
      window.removeEventListener('pointerup',finish);
      window.removeEventListener('pointercancel',finish);
      stopListening.current=()=>{};
    };
    document.body.style.userSelect='none';document.body.style.cursor='col-resize';
    setResizing(true);
  };
  const onKeyDown=event=>{
    const next=keyWidth(width,event.key,measure().room);
    if(next===null)return;
    event.preventDefault();
    if(next!==width){setWidth(next);onDone?.(next);}
  };

  return {pane,resizing,
    handle:{onPointerDown,onKeyDown}};
}
