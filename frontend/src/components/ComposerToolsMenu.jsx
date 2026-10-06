import React,{useEffect,useId,useRef,useState} from 'react';
import {IC} from './icons.jsx';
import {splitComposerTools,filterConnectorTools,connectorLabel,toolsTitle} from '../composerTools.js';

const MAX_CONNECTOR_ROWS=80;

// Tools icon inside the composer box. Its popover holds a switch per tool and,
// when connectors are configured, the searchable connector operations.
export default function ComposerToolsMenu({tools,activeToolIds,onToggle,busy,isMobile,t,font,inputS}){
  const [open,setOpen]=useState(false),[search,setSearch]=useState("");
  const rootRef=useRef(null),menuId=useId();
  const {tools:toolRows,connectors,activeCount,activeNames}=splitComposerTools(tools,activeToolIds);
  const active=new Set(activeToolIds||[]);
  const matches=filterConnectorTools(connectors,search);
  const activeConnectors=connectors.filter(tl=>active.has(tl.id)).length;
  useEffect(()=>{
    if(!open)return;
    const outside=(e)=>{if(rootRef.current&&!rootRef.current.contains(e.target))setOpen(false);};
    const escape=(e)=>{if(e.key==="Escape")setOpen(false);};
    document.addEventListener("mousedown",outside);
    document.addEventListener("keydown",escape);
    return()=>{document.removeEventListener("mousedown",outside);document.removeEventListener("keydown",escape);};
  },[open]);
  useEffect(()=>{if(busy)setOpen(false);},[busy]);
  const lit=open||activeCount>0;
  const sectionS={fontSize:9,fontWeight:800,color:t.mut,textTransform:"uppercase",letterSpacing:.6};
  const row=(tl,col,label,title)=>{
    const on=active.has(tl.id);
    return <button key={tl.id} type="button" role="switch" aria-checked={on} onClick={()=>onToggle(tl.id)} title={title} style={{display:"flex",alignItems:"center",gap:8,width:"100%",padding:"7px 9px",borderRadius:8,border:`1px solid ${on?col:t.brd}22`,background:on?`${col}13`:`${t.surface}55`,color:on?col:t.dim,cursor:"pointer",fontFamily:font,textAlign:"left",boxSizing:"border-box",flexShrink:0}}>
      <span style={{flex:1,minWidth:0}}>
        <span style={{display:"block",fontSize:12,fontWeight:on?700:600,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{label}</span>
        {!tl.connector&&tl.description&&<span style={{display:"block",fontSize:9,color:t.mut,marginTop:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{tl.description}</span>}
      </span>
      <span aria-hidden="true" style={{position:"relative",width:26,height:14,borderRadius:7,flexShrink:0,background:on?col:`${t.mut}40`,transition:"background .15s"}}>
        <span style={{position:"absolute",top:2,left:on?14:2,width:10,height:10,borderRadius:"50%",background:on?t.bg:t.dim,transition:"left .15s"}}/>
      </span>
    </button>;
  };
  return <div ref={rootRef} className="hc-composer-anchor hc-tools-anchor" style={{flexShrink:0}}>
    <span style={{position:"relative",display:"flex"}}>
      <button type="button" onClick={()=>{setOpen(p=>!p);setSearch("");}} aria-haspopup="dialog" aria-expanded={open} aria-controls={menuId} aria-label={`Tools, ${activeCount} on`} title={toolsTitle(activeNames)} style={{background:open?`${t.acc}18`:"none",border:`1px solid ${open?`${t.acc}44`:"transparent"}`,color:lit?t.acc:t.mut,cursor:"pointer",padding:"4px 6px",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,opacity:lit?1:.75,borderRadius:7,lineHeight:1}}><IC.Tool/></button>
      {activeCount>0&&<span aria-hidden="true" style={{position:"absolute",top:-5,right:-5,minWidth:14,height:14,padding:"0 3px",borderRadius:8,background:t.acc,color:t.bg,fontSize:8,fontWeight:800,display:"flex",alignItems:"center",justifyContent:"center",boxSizing:"border-box",pointerEvents:"none"}}>{activeCount>99?"99+":activeCount}</span>}
    </span>
    {open&&<div id={menuId} role="dialog" aria-label="Tools" className="hc-composer-menu" style={{position:"absolute",bottom:"calc(100% + 8px)",left:0,zIndex:310,width:300,maxWidth:"calc(100vw - 32px)",...(isMobile?{}:{maxHeight:"max(160px, min(380px, calc(100vh / var(--hc-ui-scale, 1) - 340px)))"}),overflow:"hidden",background:`${t.bgDeep}F7`,border:`1px solid ${t.brd}55`,borderRadius:10,boxShadow:"0 10px 28px #0009",padding:8,display:"flex",flexDirection:"column",gap:7,backdropFilter:"blur(8px)",animation:"fadeIn .16s ease",boxSizing:"border-box"}}>
      <div style={{display:"flex",alignItems:"baseline",gap:7,padding:"1px 3px 0",flexShrink:0}}>
        <span style={{...sectionS,color:t.acc,fontWeight:900,fontSize:10,letterSpacing:.7}}>Tools</span>
        <span style={{fontSize:9,color:t.mut}}>{activeCount} on</span>
      </div>
      <div style={{overflowY:"auto",minHeight:0,display:"flex",flexDirection:"column",gap:3,paddingRight:2}}>
        {toolRows.map(tl=>row(tl,tl.id==="quick_search"?t.f1:t.warm,tl.name||tl.id,tl.description||tl.name))}
        {!toolRows.length&&!connectors.length&&<div style={{fontSize:10,color:t.mut,textAlign:"center",padding:14}}>No tools available.</div>}
        {connectors.length>0&&<>
          <div style={{display:"flex",alignItems:"baseline",gap:7,padding:"7px 3px 1px",flexShrink:0}}>
            <span style={sectionS}>Connectors</span>
            <span style={{fontSize:9,color:t.mut}}>{activeConnectors} active · {connectors.length} available</span>
          </div>
          <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search connector tools" aria-label="Search connector tools" style={{...inputS,fontSize:10,padding:"6px 8px",height:30,flexShrink:0}}/>
          {matches.slice(0,MAX_CONNECTOR_ROWS).map(tl=>row(tl,t.acc,connectorLabel(tl.name),`${tl.name}\n${tl.description||""}`))}
          {matches.length>MAX_CONNECTOR_ROWS&&<div style={{fontSize:9,color:t.mut,textAlign:"center",padding:6}}>Showing first {MAX_CONNECTOR_ROWS}. Search to narrow.</div>}
          {!matches.length&&<div style={{fontSize:10,color:t.mut,textAlign:"center",padding:14}}>No connector tools match.</div>}
        </>}
      </div>
    </div>}
  </div>;
}
