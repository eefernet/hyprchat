import {IC} from './icons.jsx';
import useFocusScope from '../useFocusScope.js';
import usePresence from '../usePresence.js';
import React, {useEffect,useId,useRef,useState} from 'react';
import {createPortal} from 'react-dom';

// A portaled menu avoids clipping inside scrollable cards and scaled shells.
// It pops in from the trigger edge (motion.css .hc-pop, origin from placement)
// and stays mounted briefly while closing so it can fade out.
export default function ActionMenu({label='More actions',items,compact=false,triggerText,disabled=false,summary,className=''}) {
  const [open,setOpen]=useState(false),[position,setPosition]=useState({style:{},up:false});
  const trigger=useRef(null),id=useId();
  const presence=usePresence(open);
  const close=(restore=true)=>{setOpen(false);if(restore)trigger.current?.focus({preventScroll:true});};
  const menu=useFocusScope(open,()=>close());
  useEffect(()=>{
    if(!open)return;
    const place=()=>{
      const r=trigger.current.getBoundingClientRect();
      const scale=Number(getComputedStyle(document.documentElement).getPropertyValue('--hc-ui-scale'))||1;
      const width=Math.min(280*scale,window.innerWidth-24);
      const viewport=window.visualViewport, bottom=(viewport?.height||window.innerHeight)+(viewport?.offsetTop||0);
      const below=bottom-r.bottom-20, above=r.top-20, up=below<200&&above>below;
      setPosition({up,style:{zoom:scale,width:width/scale,left:Math.max(12,Math.min(r.right-width,window.innerWidth-width-12))/scale,...(up?{bottom:(window.innerHeight-r.top+8)/scale}:{top:(r.bottom+8)/scale}),maxHeight:Math.max(80,up?above:below)/scale}});
    };
    place();
    const frame=requestAnimationFrame(()=>menu.current?.querySelector('button:not(:disabled)')?.focus());
    const outside=e=>{if(!menu.current?.contains(e.target)&&!trigger.current?.contains(e.target))close(false);};
    const escape=e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();}};
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape,true);
    window.addEventListener('resize',place);
    const scroll=e=>{if(!menu.current?.contains(e.target))place();};
    window.addEventListener('scroll',scroll,true);window.visualViewport?.addEventListener('resize',place);
    return()=>{cancelAnimationFrame(frame);document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape,true);window.removeEventListener('resize',place);window.removeEventListener('scroll',scroll,true);window.visualViewport?.removeEventListener('resize',place);};
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
    <button ref={trigger} type="button" disabled={disabled} className={`hc-button${compact?' is-compact':''}${className?' '+className:''}`} title={typeof triggerText==='string'?triggerText:undefined} aria-label={label} aria-haspopup="menu" aria-expanded={open} aria-controls={open?id:undefined} onKeyDown={e=>{if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();setOpen(true);}}} onClick={e=>{e.stopPropagation();setOpen(v=>!v);}}>{compact?<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3" cy="8" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="13" cy="8" r="1.25"/></svg>:<><span className="hc-selection-value">{triggerText||label}</span><span aria-hidden="true" className="hc-chevron" data-open={open?"true":"false"} style={{display:"inline-flex"}}><IC.ChevDown/></span></>}</button>
    {presence.mounted&&createPortal(<div ref={menu} id={id} role="menu" aria-label={label} aria-describedby={summary?`${id}-summary`:undefined} className="hc-popover hc-pop" data-placement={position.up?'top':'bottom'} data-align="end" {...presence.props} style={position.style} onKeyDown={keyboard}>
      {summary&&<div id={`${id}-summary`} className="hc-menu-summary" role="note">{summary}</div>}
      {items.filter(Boolean).map((item,i)=><button key={item.key||item.label||i} type="button" role={item.role||(item.checked===undefined?'menuitem':'menuitemcheckbox')} aria-checked={item.checked} disabled={item.disabled} style={item.destructive?{color:'var(--hc-danger)'}:undefined} onClick={e=>{e.stopPropagation();close();item.onClick();}}>{item.checked!==undefined&&<span aria-hidden="true">{item.checked?'✓':'○'}</span>}<span className="hc-menu-label" title={typeof item.label==='string'?item.label:undefined}>{item.label}</span></button>)}
    </div>,document.body)}
  </>;
}
