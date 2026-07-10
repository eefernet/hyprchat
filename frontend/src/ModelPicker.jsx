import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import {
  cloudModelName,
  cloudModelProvider,
  formatModelCtx,
  isMoeModelName,
  modelContextLength,
} from './modelHelpers.js';

const RefreshIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="23 4 23 10 17 10"/>
    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
  </svg>
);

// ============================================================
// ModelPicker — custom dropdown replacing plain <select> for model selection
export default function ModelPicker({value,onChange,models,modelDetails,t,font,style={},compact=false,onRefresh}){
  const [open,setOpen]=useState(false);
  const [dropPos,setDropPos]=useState({top:0,left:0,width:220});
  const triggerRef=useRef(null);
  const dropRef=useRef(null);
  useEffect(()=>{
    if(!open)return;
    const handler=(e)=>{
      if(triggerRef.current&&triggerRef.current.contains(e.target))return;
      if(dropRef.current&&dropRef.current.contains(e.target))return;
      setOpen(false);
    };
    const close=()=>setOpen(false);
    document.addEventListener("mousedown",handler);
    window.addEventListener("resize",close);
    return()=>{document.removeEventListener("mousedown",handler);window.removeEventListener("resize",close);};
  },[open]);
  const openDropdown=()=>{
    if(triggerRef.current){
      const r=triggerRef.current.getBoundingClientRect();
      // Clamp against the dropdown's max possible width (it grows past minWidth
      // up to maxWidth from content), so the right edge stays on screen.
      const maxW=Math.min(380,window.innerWidth-16);
      const width=Math.min(Math.max(r.width,240),maxW);
      const left=Math.max(8,Math.min(r.left,window.innerWidth-maxW-8));
      setDropPos({top:r.bottom+4,left,width});
    }
    const willOpen=!open;
    setOpen(p=>!p);
    if(willOpen&&onRefresh)onRefresh();
  };
  const famIcon=(n)=>{const b=(n||"").toLowerCase();if(b==="auto")return"🧭";if(b.startsWith("openai:")||b==="openai")return"◎";if(b.startsWith("anthropic:")||b==="anthropic")return"✦";if(b.startsWith("custom:")||b==="custom")return"⌬";if(b.includes("qwen"))return"🌸";if(b.includes("llama"))return"🦙";if(b.includes("mistral")||b.includes("mixtral"))return"💨";if(b.includes("gemma"))return"💎";if(b.includes("phi"))return"φ";if(b.includes("deepseek"))return"🔍";if(b.includes("coder")||b.includes("codestral"))return"💻";if(b.includes("wizard"))return"🧙";if(b.includes("hf.co"))return"🤗";return"🤖";};
  const szCol=(tag)=>{const l=(tag||"").toLowerCase();if(l.match(/70b|65b|72b/))return t.err;if(l.match(/30b|32b|34b/))return t.warm;if(l.match(/13b|14b|27b/))return t.acc;if(l.match(/7b|8b/))return t.ok;if(l.match(/1b|3b|4b/))return t.f1;return t.mut;};
  const modelCaps=(m)=>{const b=(m||"").toLowerCase();const caps=[];
    if(b==="auto")return[{emoji:"🧭",color:"#5ad0ff",label:"Router"}];
    if(b.startsWith("openai:")||b.startsWith("anthropic:")||b.startsWith("custom:"))return[{emoji:"☁",color:"#4aa3ff",label:"Cloud"}];
    if(b.match(/embed/))return[{emoji:"🔢",color:"#9b59b6",label:"Embed"}];
    if(isMoeModelName(m,modelDetails))caps.push({emoji:"MoE",color:"#66d9ef",label:"Expert"});
    if(b.match(/llava|vision|[\-:]vl[\-:$]/)||b.match(/vl\b/))caps.push({emoji:"👁",color:"#e67e22",label:"Vision"});
    if(b.match(/qwen3|deepseek-r1|r1[\-:]|qwq/))caps.push({emoji:"💭",color:"#c792ea",label:"Thinking"});
    if(b.match(/coder|codestral|starcoder|deepseek-coder/))caps.push({emoji:"💻",color:"#2ecc71",label:"Code"});
    if(!b.match(/embed/)&&b.match(/qwen|llama3|llama-3|mistral|mixtral|command|hermes|deepseek|phi3|phi-3|wizardlm|gemma|llama3\./))caps.push({emoji:"🔧",color:"#3498db",label:"Tools"});
    return caps;};
  const curProvider=cloudModelProvider(value);
  const [name,tag="latest"]=curProvider?[curProvider,cloudModelName(value)]:(value||"").split(":");
  const sc=szCol(tag);
  const caps=modelCaps(value||"");
  const md=modelDetails?.[value]||{};
  const paramSz=md.details?.parameter_size||"";
  const ctxLen=modelContextLength(modelDetails,value);
  const isMissing=value&&models.length>0&&!models.includes(value);
  return <div ref={triggerRef} style={{position:"relative",...style}}>
    <div onClick={openDropdown} style={{display:"flex",alignItems:"center",gap:6,padding:compact?"3px 8px":"5px 10px",background:isMissing?`${t.err}15`:open?`${t.acc}15`:t.bgDeep,border:`1px solid ${isMissing?t.err:open?t.acc:t.brd}${open||isMissing?"55":"33"}`,borderRadius:8,cursor:"pointer",minWidth:compact?100:150,transition:"all .15s",userSelect:"none"}}>
      <span style={{fontSize:compact?12:14,flexShrink:0}}>{isMissing?"⚠️":famIcon(name)}</span>
      <div style={{flex:1,minWidth:0}}>
        <div style={{fontSize:compact?10:11,fontWeight:600,color:isMissing?t.err:t.acc,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{isMissing?`${curProvider?tag:name} (deleted)`:(curProvider?tag:(name||"Select model"))}</div>
        {!compact&&<div style={{display:"flex",gap:3,flexWrap:"wrap",marginTop:1}}>
          <span style={{fontSize:8,color:sc,fontWeight:700}}>{tag}</span>
          {paramSz&&<span style={{fontSize:8,color:t.mut}}>{paramSz}</span>}
          {ctxLen>0&&<span style={{fontSize:8,color:t.mut}}>{formatModelCtx(ctxLen)} ctx</span>}
          {caps.map(c=><span key={c.label} style={{fontSize:7,padding:"0px 3px",borderRadius:3,background:`${c.color}20`,color:c.color,fontWeight:700,border:`1px solid ${c.color}33`}}>{c.emoji}</span>)}
        </div>}
      </div>
      {onRefresh&&<span onClick={e=>{e.stopPropagation();onRefresh();}} style={{fontSize:12,color:t.mut,flexShrink:0,cursor:"pointer",opacity:.5,display:"flex",alignItems:"center",padding:"0 2px"}} title="Refresh models"><RefreshIcon/></span>}
      <span style={{fontSize:9,color:t.mut,flexShrink:0,transition:"transform .2s",transform:open?"rotate(180deg)":"none"}}>▾</span>
    </div>
    {open&&(()=>{
      const getMaker=(m)=>{
        if(m==="auto")return"Auto";
        if(m.startsWith("openai:"))return"OpenAI";
        if(m.startsWith("anthropic:"))return"Anthropic";
        if(m.startsWith("custom:"))return modelDetails?.[m]?.details?.provider_label||"Custom";
        if(m.startsWith("hf.co/")){const parts=m.replace("hf.co/","").split("/");return parts[0]||"HuggingFace";}
        const b=m.toLowerCase();
        if(b.includes("qwen"))return"Alibaba";
        if(b.includes("llama"))return"Meta";
        if(b.includes("mistral")||b.includes("mixtral"))return"Mistral AI";
        if(b.includes("gemma"))return"Google";
        if(b.includes("phi"))return"Microsoft";
        if(b.includes("deepseek"))return"DeepSeek";
        if(b.includes("command"))return"Cohere";
        if(b.includes("hermes"))return"NousResearch";
        if(b.includes("wizard")||b.includes("vicuna"))return"WizardLM";
        if(b.includes("codestral")||b.includes("starcoder"))return"Mistral AI";
        return"Other";
      };
      const grouped={};
      [...models].sort((a,b)=>a.localeCompare(b)).forEach(m=>{
        const mk=getMaker(m);
        if(!grouped[mk])grouped[mk]=[];
        grouped[mk].push(m);
      });
      const sortedMakers=Object.keys(grouped).sort((a,b)=>{
        // HuggingFace authors after main makers, "Other" last
        if(a==="Other")return 1;if(b==="Other")return-1;
        return a.localeCompare(b);
      });
      return createPortal(<div ref={dropRef} style={{position:"fixed",top:dropPos.top,left:dropPos.left,minWidth:dropPos.width,maxWidth:"min(380px, calc(100vw - 16px))",background:t.bgDeep,border:`1px solid ${t.brd}55`,borderRadius:10,boxShadow:`0 8px 32px #00000077`,zIndex:99999,maxHeight:360,overflowY:"auto",padding:"4px",fontFamily:font}}>
        {sortedMakers.map(maker=><div key={maker}>
          <div style={{padding:"5px 10px 3px",fontSize:8,fontWeight:700,color:t.mut,textTransform:"uppercase",letterSpacing:.8,borderTop:maker===sortedMakers[0]?"none":`1px solid ${t.brd}22`,marginTop:maker===sortedMakers[0]?0:4,paddingTop:maker===sortedMakers[0]?5:7}}>{maker}</div>
          {grouped[maker].map(m=>{
            const isHF=m.startsWith("hf.co/");
            const provider=cloudModelProvider(m);
            const [mn,mt="latest"]=provider?[provider,cloudModelName(m)]:m.split(":");
            const displayName=provider?mt:(isHF?m.replace("hf.co/","").split("/").pop()?.split(":")[0]||mn:mn);
            const sc2=szCol(mt);const sel=m===value;
            const mc=modelCaps(m);const mdi=modelDetails?.[m]||{};const ps=mdi.details?.parameter_size||"";
            const ctx2=modelContextLength(modelDetails,m);
            return <div key={m} onClick={()=>{onChange(m);setOpen(false);}}
              style={{display:"flex",alignItems:"center",gap:8,padding:"6px 9px",borderRadius:7,cursor:"pointer",background:sel?`${t.acc}18`:isHF?`#ff660008`:"transparent",border:`1px solid ${sel?t.acc:isHF?"#ff660033":"transparent"}`,marginBottom:1,transition:"background .1s"}}
              onMouseEnter={e=>{if(!sel)e.currentTarget.style.background=isHF?`#ff660015`:`${t.surface}88`;}}
              onMouseLeave={e=>{if(!sel)e.currentTarget.style.background=isHF?`#ff660008`:"transparent";}}>
              <span style={{fontSize:15,flexShrink:0}}>{isHF?"🤗":famIcon(provider||mn)}</span>
              <div style={{flex:1,minWidth:0}}>
                <div style={{display:"flex",alignItems:"center",gap:4}}>
                  <div style={{fontSize:11,fontWeight:sel?700:500,color:sel?t.acc:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{displayName}</div>
                  {isHF&&<span style={{fontSize:7,padding:"1px 4px",borderRadius:3,background:"#ff660022",color:"#ff6600",fontWeight:700,flexShrink:0,border:"1px solid #ff660033"}}>HF</span>}
                  {provider&&(()=>{const pc=provider==="openai"?t.ok:provider==="anthropic"?t.pink:"#4aa3ff";const pl=provider==="openai"?"OpenAI":provider==="anthropic"?"Claude":(mdi.details?.provider_label||"Custom");return <span style={{fontSize:7,padding:"1px 4px",borderRadius:3,background:`${pc}22`,color:pc,fontWeight:700,flexShrink:0,border:`1px solid ${pc}33`,maxWidth:80,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{pl}</span>;})()}
                </div>
                <div style={{display:"flex",gap:3,alignItems:"center",marginTop:1,flexWrap:"wrap"}}>
                  <span style={{fontSize:8,color:sc2,fontWeight:700}}>{mt}</span>
                  {ps&&<span style={{fontSize:8,color:t.mut}}>{ps}</span>}
                  {ctx2>0&&<span style={{fontSize:8,color:t.mut}}>{formatModelCtx(ctx2)} ctx</span>}
                  {mc.map(c=><span key={c.label} style={{fontSize:8,padding:"1px 4px",borderRadius:4,background:`${c.color}20`,color:c.color,fontWeight:600,border:`1px solid ${c.color}33`}}>{c.emoji} {c.label}</span>)}
                </div>
              </div>
              {sel&&<span style={{fontSize:10,color:t.acc,flexShrink:0}}>✓</span>}
            </div>;
          })}
        </div>)}
      </div>, document.body);
    })()}
  </div>;
}

