import useFocusScope from '../useFocusScope.js';
import React, {useEffect,useId,useRef,useState} from 'react';
import {createPortal} from 'react-dom';

// A portaled menu avoids clipping inside scrollable cards and scaled shells.
export default function ActionMenu({label='More actions',items,compact=false}) {
  const [open,setOpen]=useState(false),[position,setPosition]=useState({});
  const trigger=useRef(null),id=useId();
  const close=(restore=true)=>{setOpen(false);if(restore)trigger.current?.focus();};
  const menu=useFocusScope(open,()=>close());
  useEffect(()=>{
    if(!open)return;
    const place=()=>{
      const r=trigger.current.getBoundingClientRect();
      const scale=Number(getComputedStyle(document.documentElement).getPropertyValue('--hc-ui-scale'))||1;
      const width=Math.min(280*scale,window.innerWidth-24);
      setPosition({zoom:scale,width:width/scale,left:Math.max(12,Math.min(r.right-width,window.innerWidth-width-12))/scale,top:Math.min(r.bottom+8,window.innerHeight-100)/scale,maxHeight:Math.max(80,window.innerHeight-r.bottom-20)/scale});
    };
    place();
    const frame=requestAnimationFrame(()=>menu.current?.querySelector('button:not(:disabled)')?.focus());
    const outside=e=>{if(!menu.current?.contains(e.target)&&!trigger.current?.contains(e.target))close(false);};
    const escape=e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();}};
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape,true);
    window.addEventListener('resize',place);
    return()=>{cancelAnimationFrame(frame);document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape,true);window.removeEventListener('resize',place);};
  },[open]);
  const keyboard=e=>{
    const nodes=[...menu.current.querySelectorAll('button:not(:disabled)')];
    const index=nodes.indexOf(document.activeElement);
    if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {
      e.preventDefault();nodes[e.key==='Home'?0:e.key==='End'?nodes.length-1:(index+(e.key==='ArrowDown'?1:-1)+nodes.length)%nodes.length]?.focus();
    }
    if(e.key==='Tab')close();
  };
  return <>
    <button ref={trigger} type="button" className="hc-button" aria-label={label} aria-haspopup="menu" aria-expanded={open} aria-controls={open?id:undefined} onClick={e=>{e.stopPropagation();setOpen(v=>!v);}}>{compact?<span aria-hidden="true">•••</span>:<>{label} <span aria-hidden="true">⌄</span></>}</button>
    {open&&createPortal(<div ref={menu} id={id} role="menu" aria-label={label} className="hc-popover" style={position} onKeyDown={keyboard}>
      {items.filter(Boolean).map((item,i)=><button key={item.label||i} type="button" role={item.checked===undefined?'menuitem':'menuitemcheckbox'} aria-checked={item.checked} disabled={item.disabled} style={item.destructive?{color:'var(--hc-danger)'}:undefined} onClick={e=>{e.stopPropagation();close();item.onClick();}}>{item.checked!==undefined&&<span aria-hidden="true">{item.checked?'✓':'○'}</span>}{item.label}</button>)}
    </div>,document.body)}
  </>;
}
