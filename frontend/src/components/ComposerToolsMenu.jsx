import React,{useEffect,useId,useRef,useState} from 'react';
import {IC} from './icons.jsx';
import {SwitchTrack} from './Switch.jsx';
import usePresence from '../usePresence.js';
import {splitComposerTools,filterConnectorTools,connectorLabel,toolsTitle} from '../composerTools.js';

const MAX_CONNECTOR_ROWS=80;

// Tools icon inside the composer box. Its popover holds a switch per tool and,
// when connectors are configured, the searchable connector operations. Rows are
// flat list items (surfaces.css .hc-tool-row); the switch carries the on state.
export default function ComposerToolsMenu({tools,activeToolIds,onToggle,busy,isMobile,t,font,inputS}){
  const [open,setOpen]=useState(false),[search,setSearch]=useState("");
  const rootRef=useRef(null),menuId=useId();
  const presence=usePresence(open);
  const {tools:toolRows,connectors,activeCount,activeNames}=splitComposerTools(tools,activeToolIds);
  const active=new Set(activeToolIds||[]);
  const matches=filterConnectorTools(connectors,search);
  const activeConnectors=connectors.filter(tl=>active.has(tl.id)).length;
  useEffect(()=>{
    if(!open)return;
    const outside=(e)=>{if(rootRef.current&&!rootRef.current.contains(e.target))setOpen(false);};
    const escape=(e)=>{if(e.key==="Escape"){e.stopImmediatePropagation();setOpen(false);rootRef.current?.querySelector("button")?.focus();}};
    document.addEventListener("mousedown",outside);
    document.addEventListener("keydown",escape,true);
    return()=>{document.removeEventListener("mousedown",outside);document.removeEventListener("keydown",escape,true);};
  },[open]);
  useEffect(()=>{if(busy)setOpen(false);},[busy]);
  const lit=open||activeCount>0;
  const sectionS={fontSize:"var(--hc-text-secondary)",fontWeight:600,color:t.mut};
  const row=(tl,col,label,title,idx)=>{
    const on=active.has(tl.id);
    return <button key={tl.id} type="button" role="switch" aria-checked={on} onClick={()=>onToggle(tl.id)} title={title} className="hc-tool-row" style={{"--tool-c":col,"--i":idx,fontFamily:font}}>
      <span style={{flex:1,minWidth:0}}>
        <span style={{display:"block",fontSize:"var(--hc-text-control)",fontWeight:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{label}</span>
        {!tl.connector&&tl.description&&<span style={{display:"block",fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{tl.description}</span>}
      </span>
      <SwitchTrack on={on} color={col}/>
    </button>;
  };
  return <div ref={rootRef} className="hc-composer-anchor hc-tools-anchor" style={{flexShrink:0}}>
    <span style={{position:"relative",display:"flex"}}>
      <button type="button" onClick={()=>{setOpen(p=>!p);setSearch("");}} aria-haspopup="dialog" aria-expanded={open} aria-controls={menuId} aria-label={`Tools, ${activeCount} on`} title={toolsTitle(activeNames)} className={`hc-icon-btn is-lg${open?" is-open":""}`} style={{color:lit?t.acc:t.mut,opacity:lit?1:.75}}><IC.Tool/></button>
      {activeCount>0&&<span aria-hidden="true" style={{position:"absolute",top:-3,right:-3,minWidth:15,height:15,padding:"0 4px",borderRadius:"var(--hc-radius-pill)",background:t.acc,color:t.bg,fontSize:"var(--hc-text-micro)",fontWeight:600,display:"flex",alignItems:"center",justifyContent:"center",boxSizing:"border-box",pointerEvents:"none"}}>{activeCount>99?"99+":activeCount}</span>}
    </span>
    {presence.mounted&&<div id={menuId} role="dialog" aria-label="Tools" className="hc-composer-menu hc-pop hc-elev-2" data-placement="top" {...presence.props} style={{position:"absolute",bottom:"calc(100% + 8px)",left:0,zIndex:310,width:320,maxWidth:"calc(100vw - 32px)",...(isMobile?{}:{maxHeight:"max(160px, min(380px, calc(100vh / var(--hc-ui-scale, 1) - 340px)))"}),overflow:"hidden",padding:6,display:"flex",flexDirection:"column",gap:4,boxSizing:"border-box"}}>
      <div style={{display:"flex",alignItems:"baseline",gap:7,padding:"4px 8px 2px",flexShrink:0}}>
        <span style={{...sectionS,color:t.text}}>Tools</span>
        <span style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{activeCount} on</span>
      </div>
      <div style={{overflowY:"auto",minHeight:0,display:"flex",flexDirection:"column",gap:2,paddingRight:2}}>
        {toolRows.map((tl,idx)=>row(tl,tl.id==="quick_search"?t.f1:t.warm,tl.name||tl.id,tl.description||tl.name,idx))}
        {!toolRows.length&&!connectors.length&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,textAlign:"center",padding:14}}>No tools available.</div>}
        {connectors.length>0&&<>
          <div style={{display:"flex",alignItems:"baseline",gap:7,padding:"8px 8px 2px",flexShrink:0}}>
            <span style={sectionS}>Connectors</span>
            <span style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{activeConnectors} active · {connectors.length} available</span>
          </div>
          <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search connector tools" aria-label="Search connector tools" style={{...inputS,fontSize:"var(--hc-text-control)",padding:"6px 8px",height:30,flexShrink:0}}/>
          {matches.slice(0,MAX_CONNECTOR_ROWS).map((tl,idx)=>row(tl,t.acc,connectorLabel(tl.name),`${tl.name}\n${tl.description||""}`,toolRows.length+idx))}
          {matches.length>MAX_CONNECTOR_ROWS&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,textAlign:"center",padding:6}}>Showing first {MAX_CONNECTOR_ROWS}. Search to narrow.</div>}
          {!matches.length&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,textAlign:"center",padding:14}}>No connector tools match.</div>}
        </>}
      </div>
    </div>}
  </div>;
}
