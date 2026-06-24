import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { API, proxiedImageUrl } from '../session.js';
import { ArtifactCard } from './artifactComponents.jsx';
import { IC } from './icons.jsx';
import { Collapsible } from './markdownBlocks.jsx';

const Pill = ({ev,t,expanded,onToggle})=>{
  const scrollRef = useRef(null);
  const stickRef = useRef(true);  // sticky-bottom: auto-scroll to new steps unless user scrolled up
  const [copiedDetail,setCopiedDetail]=useState("");
  const isS = ev.type==="tool_start"||ev.type==="tool_progress"||ev.type==="thinking";
  const isE = ev.type==="tool_error"||ev.type==="error";
  const isD = ev.type==="tool_end"||ev.type==="tool_done"||ev.type==="complete"||ev.type==="thought_done";
  const isThink = ev.type==="thinking"||ev.type==="thought_done";
  const bg=isE?`${t.err}18`:isD?`${t.surface}44`:isThink?`${t.f4}10`:`${t.acc}10`;
  const bc=isE?`${t.err}40`:isD?`${t.brd}24`:isThink?`${t.f4}28`:`${t.acc}28`;
  const tc=isE?t.err:isD?t.ok:isThink?t.f4:t.acc;
  let detail = null;
  if(expanded && ev.data?.detail){try{detail=JSON.parse(ev.data.detail);}catch{detail=ev.data.detail;}}
  const isClickable = !!(ev.data?.detail) || isThink;
  const stepsLen = (detail && typeof detail==="object" && Array.isArray(detail.steps)) ? detail.steps.length : 0;
  // Reset sticky + jump to bottom when the panel is (re)opened.
  useEffect(()=>{
    if(!expanded)return;
    stickRef.current = true;
    const el=scrollRef.current;
    if(el)el.scrollTop = el.scrollHeight;
  },[expanded]);
  // When new steps arrive, auto-scroll only if user hasn't scrolled up.
  useEffect(()=>{
    if(!expanded||!stickRef.current)return;
    const el=scrollRef.current;
    if(el)el.scrollTop = el.scrollHeight;
  },[stepsLen,expanded]);
  const handlePanelScroll = (e)=>{
    const el=e.currentTarget;
    // 24px tolerance — treat "almost at bottom" as still sticky
    stickRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 24;
  };
  
  const toolName = ev.data?.tool||ev.type;
  const status = ev.data?.status||"";
  
  // Geeky expressive labels + icons
  const labelMap = {
    "thinking": ["🧠", "Reasoning"],
    "thought_done": ["🧠", "Reasoned"],
    "processing": isD ? ["⚡", "Initialized"] : ["🔮", "Connecting"],
    "execute_code": isD ? ["🎯", "Executed"] : ["⚡", "Sandbox"],
    "research": isD ? ["📡", "Signal Acquired"] : ["🔎", "Scanning"],
    "deep_research": isD ? ["📊", "Research Ready"] : ["🔬", "Agent Research"],
    "conspiracy_research": isD ? ["🕵️", "Dossier Ready"] : ["🔮", "Digging"],
    "generate_image": isD ? ["🖼️", "Image ready"] : ["🖼️", "Generating image"],
    "write_file": isD ? ["💾", "Committed"] : ["📝", "Writing"],
    "read_file": isD ? ["📖", "Loaded"] : ["📂", "Reading"],
    "list_files": isD ? ["📋", "Indexed"] : ["🗂️", "Scanning"],
    "run_shell": isD ? ["⚡", "Exited"] : ["🖥️", "Shell"],
    "fetch_url": isD ? ["🌐", "Fetched"] : ["🕸️", "Spidering"],
    "install_package": isD ? ["📦", "Installed"] : ["📥", "Installing"],
    "complete": ["✅", "Done"],
    "download_file": isD ? ["📎", "Ready"] : ["📤", "Packaging"],
    "delete_file": isD ? ["🗑️", "Deleted"] : ["🗑️", "Deleting"],
    "download_project": isD ? ["📦", "Packaged"] : ["📦", "Archiving"],
    "kb": isD ? ["📚", "Retrieved"] : ["📖", "Knowledge Base"],
    "memory": isD ? ["🧠", "Recalled"] : ["🧠", "Remembering"],
    "generate_code": isD ? ["🧬", "Code Ready"] : ["🧬", "Agent Coding"],
    "generating": ["✍️", "Generating"],
    "refinement": isD ? ["✨", "Refined"] : ["✨", "Refining"],
    "error": ["❌", "Error"],
    "tool_error": ["❌", "Error"],
  };
  const [icon, label] = labelMap[toolName] || (isE ? ["❌", toolName] : isD ? ["✓", toolName] : ["⏳", toolName]);
  const isActive = isS && !isD;
  // De-duplicate status: backend often emits "🔮 Connecting to neural oracle..." while the
  // labelMap also renders "🔮 Connecting" — strip a leading emoji and the label word from
  // the status so the pill doesn't read "Connecting Connecting...".
  const displayStatus = (()=>{
    if(!status)return "";
    // Strip leading emoji (incl. ZWJ U+200D and VS16 U+FE0F) and whitespace.
    let s = status.replace(/^[\p{Extended_Pictographic}‍️\s]+/u,"");
    if(label){
      const re = new RegExp("^" + label.replace(/[.*+?^${}()|[\]\\]/g,"\\$&") + "\\s*[:—\\-]?\\s*","i");
      s = s.replace(re,"");
    }
    return s.trim();
  })();
  const showMarquee = isThink && isActive && !expanded && displayStatus.length > 15;
  const getIconAnim = (name) => {
    if(name==="execute_code"||name==="run_shell") return "spin 1.2s linear infinite";
    if(name==="research"||name==="deep_research"||name==="fetch_url"||name==="conspiracy_research"||name==="kb") return "swing 1.5s ease-in-out infinite";
    if(name==="generate_code") return "pulse 1.5s ease-in-out infinite";
    if(name==="write_file"||name==="download_file"||name==="download_project") return "bounce 1s ease-in-out infinite";
    if(name==="memory") return "memoryPulse 1.8s ease-in-out infinite";
    if(name==="generating") return "writing 0.8s ease-in-out infinite";
    if(name==="thinking") return "bop 2s ease-in-out infinite";
    return "bop 1.5s ease-in-out infinite";
  };
  const marqueeKey = Math.floor(displayStatus.length / 30);
  const copyDetailText=(key,text)=>{
    try{
      navigator.clipboard?.writeText(String(text||""));
      setCopiedDetail(key);
      window.setTimeout(()=>setCopiedDetail(""),1200);
    }catch{}
  };
  const isImageDetail=detail&&typeof detail==="object"&&detail.tool==="generate_image";
  const ImagePromptDetail=()=>{
    const preS={margin:0,padding:8,background:`${t.surface}88`,borderRadius:5,whiteSpace:"pre-wrap",wordBreak:"break-word",color:t.dim,fontSize:11,maxHeight:150,overflow:"auto",border:`1px solid ${t.brd}18`};
    const block=(key,label,value)=>value?<div style={{marginBottom:8}}>
      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:4}}>
        <span style={{fontSize:10,fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.5}}>{label}</span>
        <button onClick={()=>copyDetailText(key,value)} style={{marginLeft:"auto",fontSize:9,padding:"2px 7px",borderRadius:6,border:`1px solid ${t.acc}33`,background:`${t.acc}10`,color:t.acc,cursor:"pointer",fontFamily:"inherit"}}>{copiedDetail===key?"Copied":"Copy"}</button>
      </div>
      <pre style={preS}>{value}</pre>
    </div>:null;
    const flags=[
      ["Size",detail.width&&detail.height?`${detail.width} x ${detail.height}`:""],
      ["Steps",detail.steps],
      ["CFG",detail.cfg],
      ["Sampler",detail.sampler],
      ["Scheduler",detail.scheduler],
      ["Model type",detail.model_sampling],
      ["Persona profile",detail.profile_active?"yes":"no"],
      ["Workflow",detail.workflow_active?(detail.profile_workflow?"persona":"global"):"default"],
      ["LoRAs",detail.loras_active?"yes":"no"],
      ["Fallback",detail.prompt_fallback?"yes":"no"],
    ].filter(([,v])=>v!==undefined&&v!==null&&v!=="");
    return <div>
      {block("prompt","Prompt sent to ComfyUI",detail.prompt)}
      {block("negative","Negative prompt",detail.negative_prompt)}
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(110px,1fr))",gap:6,marginTop:2}}>
        {flags.map(([k,v])=><div key={k} style={{border:`1px solid ${t.brd}22`,background:`${t.surface}55`,borderRadius:6,padding:"6px 8px",minWidth:0}}>
          <div style={{fontSize:8,color:t.mut,textTransform:"uppercase",letterSpacing:.5,marginBottom:2}}>{k}</div>
          <div style={{fontSize:10,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{String(v)}</div>
        </div>)}
      </div>
    </div>;
  };

  return <div style={{animation:"fadeIn .3s",marginBottom:2}}>
    <div onClick={isClickable?onToggle:undefined} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"4px 12px",background:bg,border:`1px solid ${bc}`,borderRadius:expanded?"9px 9px 0 0":9,fontSize:12,color:tc,cursor:isClickable?"pointer":"default",maxWidth:"100%",transition:"all .2s",animation:isActive?`fadeIn .3s, pillGlow 2s ease-in-out infinite`:"fadeIn .3s",boxShadow:isActive?`0 0 8px ${bc}`:"none"}}>
      {isS&&!isD&&<span style={{display:"inline-block",animation:getIconAnim(toolName),flexShrink:0,lineHeight:1,transformOrigin:"center"}}>{icon}</span>}
      {isD&&<><span style={{color:t.ok,flexShrink:0,lineHeight:1}}>✓</span><span style={{flexShrink:0,lineHeight:1}}>{icon}</span></>}
      <span style={{fontWeight:600,flexShrink:0}}>{label}</span>
      {showMarquee
        ? <span className="pill-marquee-wrap">
            <span key={marqueeKey} className="pill-marquee-inner" style={{color:tc,opacity:.75,fontStyle:"italic"}}>
              {displayStatus}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{displayStatus}
            </span>
          </span>
        : displayStatus&&<span style={{opacity:.6,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:350,fontStyle:isThink?"italic":"normal"}}>{displayStatus}</span>
      }
      {isClickable&&<span style={{opacity:.3,flexShrink:0,fontSize:8,marginLeft:2}}>{expanded?"▴":"▾"}</span>}
    </div>
    {expanded&&(detail||isThink)&&<div ref={scrollRef} onScroll={handlePanelScroll} style={{background:`${t.bgDeep}`,border:`1px solid ${bc}`,borderTop:"none",borderRadius:"0 0 8px 8px",padding:10,fontSize:11,maxHeight:300,overflowY:"auto",maxWidth:600}}>
      {!detail?<div style={{color:t.dim,fontStyle:"italic",lineHeight:1.6,whiteSpace:"pre-wrap",opacity:.7}}>{status||"Thinking..."}</div>
      :typeof detail==="string"?<pre style={{margin:0,whiteSpace:"pre-wrap",color:t.dim,fontFamily:"inherit"}}>{detail}</pre>
      :isImageDetail?<ImagePromptDetail/>
      :detail.thinking?<div ref={el=>{if(el&&isActive)el.scrollTop=el.scrollHeight;}} style={{color:t.dim,fontStyle:"italic",lineHeight:1.6,whiteSpace:"pre-wrap",maxHeight:250,overflowY:"auto"}}>{detail.thinking}</div>
      :detail.code?<div>
        {detail.language&&<div style={{color:t.mut,marginBottom:4,fontSize:10}}>🔧 <span style={{color:t.acc}}>{detail.language}</span></div>}
        <pre style={{margin:0,padding:8,background:`${t.surface}88`,borderRadius:4,whiteSpace:"pre-wrap",color:t.warm,fontSize:11,maxHeight:100,overflow:"auto",border:`1px solid ${t.brd}18`}}>{detail.code}</pre>
        {detail.stdout&&<><div style={{color:t.ok,marginTop:6,fontWeight:600,fontSize:10}}>📤 stdout:</div><pre style={{margin:0,padding:6,whiteSpace:"pre-wrap",color:t.dim,fontSize:11,background:`${t.ok}08`,borderRadius:4,marginTop:2}}>{detail.stdout}</pre></>}
        {detail.stderr&&<><div style={{color:t.err,marginTop:6,fontWeight:600,fontSize:10}}>⚠️ stderr:</div><pre style={{margin:0,padding:6,whiteSpace:"pre-wrap",color:t.err,fontSize:11,background:`${t.err}08`,borderRadius:4,marginTop:2}}>{detail.stderr}</pre></>}
      </div>
      :detail.results?<div>{detail.results.map((r,i)=><div key={i} style={{marginBottom:4}}><a href={r.url} target="_blank" style={{color:t.acc,textDecoration:"none",fontSize:11}}>🔗 {r.title}</a></div>)}</div>
      :detail.steps?<div>
        <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:4}}>
          <span style={{fontSize:10,fontWeight:700,color:t.acc}}>Agent Timeline</span>
          <span style={{fontSize:9,color:t.mut,background:`${t.surface}88`,padding:"1px 6px",borderRadius:8}}>{detail.steps.length} step{detail.steps.length!==1?"s":""}</span>
        </div>
        {detail.steps.length>0&&(()=>{const last=detail.steps[detail.steps.length-1];return <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:6,padding:"4px 8px",background:`${t.acc}11`,borderRadius:6,border:`1px solid ${t.acc}22`}}>
          <div style={{width:8,height:8,borderRadius:"50%",background:t.acc,animation:"pulse 1.4s infinite",flexShrink:0}}/>
          <span style={{fontSize:10,color:t.mut,fontWeight:600}}>Current:</span>
          <span style={{fontSize:11,fontWeight:700,color:t.acc}}>{last.icon} {last.label}</span>
          <span style={{fontSize:10,color:t.dim,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{last.detail}</span>
        </div>;})()}
        <div style={{display:"flex",flexDirection:"column",gap:0,paddingLeft:8,borderLeft:`2px solid ${t.acc}22`}}>
          {detail.steps.map((s,i)=>{const isLast=i===detail.steps.length-1;return <div key={i} style={{display:"flex",gap:6,alignItems:"flex-start",padding:"3px 0",position:"relative",opacity:isLast?1:.65}}>
            <span style={{position:"absolute",left:-13,top:6,width:isLast?8:6,height:isLast?8:6,borderRadius:"50%",background:isLast?t.acc:`${t.acc}55`,animation:isLast?"pulse 1.4s infinite":"none",transition:"all .2s"}}/>
            <span style={{flexShrink:0,fontSize:11,width:18,textAlign:"center"}}>{s.icon}</span>
            <span style={{color:isLast?t.acc:`${t.acc}99`,fontSize:10,fontWeight:isLast?700:600,flexShrink:0,minWidth:55}}>{s.label}</span>
            <span style={{color:isLast?t.text:t.dim,fontSize:10,fontWeight:isLast?600:400,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{s.detail}</span>
          </div>;})}
        </div>
      </div>
      :detail.method?<div>
        <div style={{color:t.mut,fontSize:10,marginBottom:4}}>⚙️ <span style={{color:t.acc}}>{detail.method}</span></div>
        {detail.args&&<pre style={{margin:0,padding:6,background:`${t.surface}66`,borderRadius:4,whiteSpace:"pre-wrap",color:t.dim,fontSize:10,maxHeight:60,overflow:"auto"}}>{JSON.stringify(detail.args,null,2)}</pre>}
        {detail.result&&<><div style={{color:t.ok,marginTop:4,fontWeight:600,fontSize:10}}>📤 Result:</div><pre style={{margin:0,padding:6,whiteSpace:"pre-wrap",color:t.dim,fontSize:10,maxHeight:80,overflow:"auto"}}>{detail.result}</pre></>}
      </div>
      :<pre style={{margin:0,whiteSpace:"pre-wrap",color:t.dim,fontSize:10}}>{JSON.stringify(detail,null,2)}</pre>}
    </div>}
  </div>;
};

// Reasoning is intentionally shown only in the compact brain-icon thinking pill.
const ReasoningPanel = ({text,t,streaming})=>null;

const PlanPanel = ({plan,t,streaming,md,onCopy})=>{
  const [expanded,setExpanded]=useState(streaming);
  const [copied,setCopied]=useState(false);
  useEffect(()=>{setExpanded(streaming);},[streaming]);
  if(!plan) return null;
  const bc=streaming?`${t.acc}66`:`${t.acc}38`;
  const bg=`${t.acc}0c`;
  const tc=t.acc;
  const copy=async(e)=>{
    e.stopPropagation();
    try{await navigator.clipboard.writeText(plan);setCopied(true);setTimeout(()=>setCopied(false),1400);}catch{}
    onCopy&&onCopy();
  };
  return <div style={{marginBottom:6,animation:"fadeIn .3s",borderRadius:10,border:`1px solid ${bc}`,background:bg,overflow:"hidden",maxWidth:"100%",boxShadow:streaming?`0 0 12px ${t.acc}22`:"none",transition:"all .3s"}}>
    <div onClick={()=>setExpanded(p=>!p)} style={{display:"flex",alignItems:"center",gap:8,padding:"6px 12px",cursor:"pointer",userSelect:"none",borderBottom:expanded?`1px solid ${bc}`:"none",animation:streaming?"pillGlow 2s ease-in-out infinite":"none"}}>
      <span style={{fontSize:13,lineHeight:1,animation:streaming?"bop 2s ease-in-out infinite":"none",transformOrigin:"center"}}>📐</span>
      <span style={{fontWeight:700,fontSize:11,color:tc,letterSpacing:.3}}>Architecture Plan</span>
      {streaming&&<span style={{fontSize:10,color:tc,opacity:.6,fontStyle:"italic"}}>building…</span>}
      <button onClick={copy} title="Copy plan" style={{marginLeft:"auto",background:copied?`${t.ok}22`:"transparent",border:`1px solid ${copied?t.ok:`${tc}33`}`,color:copied?t.ok:tc,padding:"2px 8px",borderRadius:6,fontSize:9,fontFamily:"inherit",cursor:"pointer",fontWeight:600,opacity:.85}}>{copied?"copied":"copy"}</button>
      <span style={{fontSize:9,color:tc,opacity:.45,marginLeft:4}}>{expanded?"▴":"▾"}</span>
    </div>
    {expanded&&<div style={{padding:"10px 14px",fontSize:12,lineHeight:1.65,color:t.dim,maxHeight:420,overflowY:"auto"}}>
      {md?<MDWrap>{md(plan)}</MDWrap>:<pre style={{margin:0,whiteSpace:"pre-wrap",fontFamily:"inherit",color:t.dim}}>{plan}</pre>}
    </div>}
  </div>;
};

// Coder Bot v2 — durable run card. Reads a `runs` row by id, subscribes to
// run-tagged live events from the parent's events stream, and renders
// status + collapsible event timeline. Survives browser disconnects: on mount
// it fetches /api/runs/{runId} so the timeline rebuilds even if the SSE chat
// stream dropped while the agent was still running.

// Role → (emoji, friendly label). Each agent gets a distinct visual identity
// so a stack of cards reads as a sequence of distinct phases instead of
// "🤖 Run · builder.feature succeeded" × N. Lookup tolerates minor variants
// (builder.scaffold, builder.feature, builder.continue) by
// falling back to the role's prefix before the dot.
const _ROLE_VISUALS = {
  "architect":         {icon: "📐", label: "Designing plan"},
  "builder":           {icon: "🏗", label: "Building project"},
  "builder.scaffold":  {icon: "🏗", label: "Building project"},
  "builder.continue":  {icon: "🔄", label: "Resuming build"},
  "builder.feature":   {icon: "✨", label: "Adding feature"},
  "builder.bugfix":    {icon: "🩹", label: "Bug fix build"},
  "reviewer":          {icon: "🔍", label: "Reviewing"},
  "acceptance":        {icon: "✅", label: "Acceptance"},
  "fixer":             {icon: "🛠", label: "Fixing issues"},
  "qa":                {icon: "❓", label: "Investigating"},
  "indexer":           {icon: "📚", label: "Indexing project"},
};
const _roleVisual = (role)=>{
  if(_ROLE_VISUALS[role]) return _ROLE_VISUALS[role];
  // Try the prefix before "." (e.g. unknown "builder.X" → "builder")
  const base = (role||"").split(".")[0];
  if(_ROLE_VISUALS[base]) return _ROLE_VISUALS[base];
  return {icon: "🤖", label: role || "Run"};
};

// Map OpenHands action names → small leading icon for step rows. Keeps the
// timeline scannable.
const _STEP_ACTION_ICONS = {
  "starting": "🚀",
  "thinking": "🧠",
  "terminal": "⚡",  "terminal_result": "📤",
  "file_create": "📄",  "file_edit": "✏️",  "file_view": "👁",
  "file_str_replace": "🔁",  "file_editor_result": "✅",
  "glob": "🔎",  "glob_result": "🔎",
  "grep": "🔎",  "grep_result": "🔎",
  "finish": "🏁",
  // Reviewer
  "detect": "🧭",  "detected": "🧭",
  "build": "🔨",  "build_done": "🔨",
  "test": "🧪",  "test_done": "🧪",
  "lint": "🧹",  "lint_done": "🧹",
  "read_files": "📖",
  "analyze": "🔬",
  "parse_fallback": "⚠",
  // Architect
  "planning": "📐",  "retry": "🔄",
  "language": "🌐",  "build_cmd": "🔨",  "test_cmd": "🧪",  "lint_cmd": "🧹",
  "manifest": "📋",  "file": "📄",
  "tests_required": "🧪",  "dependencies": "📦",  "dep": "📦",
  "risk": "⚠",  "success_criteria": "🎯",  "criterion": "🎯",
  // Fixer
  "issue": "🩹",  "writing": "✏️",  "calling": "🧠",
  // Indexer
  "indexing": "📚",  "found_files": "📁",  "reading": "📖",  "read_done": "📖",
  // QA
  "question": "❓",  "list_files": "📁",  "keywords": "🏷",
  "filename_targets": "🎯",  "matched_files": "🎯",  "snippets": "📑",
  "compose": "✍️",  "fallback": "↩",  "no_match": "❌",
  // Generic
  "start": "🚀",  "done": "✅",  "error": "❌",
};

const RunCard = ({runId, liveEvts, t, font, md, onOpenArtifact})=>{
  const [run, setRun] = useState(null);
  // expanded starts false; flips to true automatically once we've loaded the
  // run and detect it needs attention (failed, or reviewer-with-issues). The
  // user can still toggle either way after that.
  const [expanded, setExpanded] = useState(false);
  const [autoExpanded, setAutoExpanded] = useState(false);
  const [err, setErr] = useState(null);
  const [artifacts,setArtifacts]=useState([]);
  // Initial fetch + when run terminal status changes, refetch for envelope.
  useEffect(()=>{
    if(!runId) return;
    let cancelled = false;
    const load = async ()=>{
      try{
        const r = await fetch(`${API}/api/runs/${runId}`);
        if(!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if(!cancelled) setRun(d);
      }catch(e){if(!cancelled) setErr(e.message||"load failed");}
    };
    load();
    // Light poll while the run is still in flight — covers reconnect mid-run
    // when liveEvts haven't yet filled in. Stops as soon as we see a terminal status.
    const id = setInterval(()=>{
      setRun(cur=>{
        if(cur && (cur.status==="succeeded"||cur.status==="failed"||cur.status==="partial"||cur.status==="cancelled"||cur.status==="skipped")){clearInterval(id);return cur;}
        load(); return cur;
      });
    }, 4000);
    return ()=>{cancelled=true; clearInterval(id);};
  },[runId]);
  useEffect(()=>{
    if(!runId)return;
    let cancelled=false;
    fetch(`${API}/api/artifacts?run_id=${encodeURIComponent(runId)}&limit=20`)
      .then(r=>r.ok?r.json():[])
      .then(d=>{if(!cancelled)setArtifacts(Array.isArray(d)?d:[]);})
      .catch(()=>{});
    return()=>{cancelled=true;};
  },[runId,run?.status]);
  // Merge persisted events with live events that arrived for this run after mount.
  const persistedEvts = run?.events_log || [];
  const liveForRun = (liveEvts||[]).filter(e=>{
    const rid = e?.data?.run_id || e?.run_id;
    return rid && rid===runId && (e.type==="tool_progress"||e.type==="tool_start"||e.type==="step");
  });
  // Dedupe live events by step number against persisted log (persisted is ground truth on reconnect).
  const persistedSteps = new Set(persistedEvts.filter(p=>p.type==="step").map(p=>p.step));
  const newLive = liveForRun.filter(e=>{
    const step = e?.data?.step ?? e?.step;
    return step!==undefined && !persistedSteps.has(step);
  });
  const allSteps = [...persistedEvts.filter(p=>p.type==="step"),
                    ...newLive.map(e=>({type:"step", step:e.data?.step ?? e.step,
                                        action:e.data?.action ?? e.action,
                                        detail:e.data?.detail ?? e.detail}))];
  // Visual status colour
  const status = run?.status || "loading";
  const colour = status==="succeeded" ? t.ok :
                 status==="failed" ? t.err :
                 status==="partial" ? "#e9a45a" :
                 (status==="cancelled"||status==="skipped") ? t.mut :
                 t.acc;
  const statusLabel = {
    queued:"queued", running:"running…", succeeded:"succeeded",
    failed:"failed", partial:"partial", cancelled:"cancelled",
    skipped:"skipped", loading:"loading…",
  }[status] || status;
  const role = run?.role || "builder";
  const visual = _roleVisual(role);
  const env = run?.result_envelope || {};
  const filesN = (env.files_written||env.files_touched||[]).length;
  const dur = env.duration_s;
  const isStreaming = status==="running" || status==="queued";
  // Auto-expand cards that need attention: any failed run, OR a reviewer
  // that returned issues/error. Only fires once per card, and only after the
  // run has loaded — so a streaming run doesn't pre-pop open and then close
  // when it succeeds.
  const needsAttention = (
    status === "failed" ||
    (role === "reviewer" && (env.status === "issues" || env.status === "error"))
  );
  if (run && needsAttention && !autoExpanded) {
    setAutoExpanded(true);
    setExpanded(true);
  }
  const bc = isStreaming ? `${colour}66` : needsAttention ? `${colour}55` : `${t.brd}24`;
  const bg = isStreaming ? `${colour}0c` : needsAttention ? `${colour}10` : `${t.surface}36`;
  return <div style={{marginTop:6,marginBottom:6,animation:"fadeIn .3s",borderRadius:10,border:`1px solid ${bc}`,background:bg,overflow:"hidden",maxWidth:"100%",boxShadow:isStreaming?`0 0 12px ${colour}22`:"none",transition:"all .3s"}}>
    <div onClick={()=>setExpanded(p=>!p)} style={{display:"flex",alignItems:"center",gap:8,padding:"6px 12px",cursor:"pointer",userSelect:"none",borderBottom:expanded?`1px solid ${bc}`:"none",animation:isStreaming?"pillGlow 2s ease-in-out infinite":"none"}}>
      <span style={{fontSize:13,lineHeight:1,animation:isStreaming?"bop 2s ease-in-out infinite":"none"}}>{visual.icon}</span>
      <span style={{fontWeight:700,fontSize:11,color:colour,letterSpacing:.3}}>{visual.label}</span>
      <span style={{fontSize:10,color:t.mut,opacity:.6}}>· {role}</span>
      <span style={{fontSize:10,color:colour,opacity:.7,fontStyle:isStreaming?"italic":"normal"}}>· {statusLabel}</span>
      {filesN>0 && <span style={{fontSize:10,color:t.mut}}>· {filesN} file{filesN===1?"":"s"}</span>}
      {artifacts.length>0 && <span style={{fontSize:10,color:t.ok}}>· {artifacts.length} artifact{artifacts.length===1?"":"s"}</span>}
      {dur && <span style={{fontSize:10,color:t.mut}}>· {dur}s</span>}
      {err && <span style={{fontSize:10,color:t.err}}>· {err}</span>}
      {artifacts.length>0&&onOpenArtifact&&<button onClick={e=>{e.stopPropagation();onOpenArtifact(artifacts[0].id);}} style={{marginLeft:"auto",fontSize:10,color:t.ok,background:`${t.ok}10`,border:`1px solid ${t.ok}35`,borderRadius:6,padding:"2px 7px",fontWeight:800,cursor:"pointer",fontFamily:font}}>artifact</button>}
      <span style={{fontSize:9,color:colour,opacity:.45,marginLeft:artifacts.length>0&&onOpenArtifact?0:"auto"}}>{expanded?"▴":"▾"}</span>
    </div>
    {!expanded && needsAttention && <div style={{display:"flex",alignItems:"center",gap:8,padding:"6px 12px",borderTop:`1px solid ${bc}`,fontSize:10,color:t.err,background:`${t.err}08`}}>
      <span style={{fontWeight:800}}>Failed run</span>
      <span style={{color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{env.error||env.summary||"Open details for the recorded issue."}</span>
      <button onClick={()=>setExpanded(true)} style={{background:"none",border:`1px solid ${t.err}35`,color:t.err,cursor:"pointer",padding:"2px 8px",borderRadius:6,fontFamily:font,fontSize:9,fontWeight:700}}>Details</button>
    </div>}
    {expanded && <div style={{padding:"8px 12px",fontSize:11,lineHeight:1.55,color:t.dim,maxHeight:320,overflowY:"auto",fontFamily:font}}>
      {allSteps.length===0 && <div style={{color:t.mut,fontStyle:"italic"}}>No steps recorded yet…</div>}
      {allSteps.map((s,i)=>{
        const stepIcon = _STEP_ACTION_ICONS[s.action] || "·";
        return <div key={i} style={{padding:"3px 0",borderBottom:i<allSteps.length-1?`1px solid ${t.brd}12`:"none",display:"flex",alignItems:"baseline",gap:6}}>
          <span style={{color:t.mut,fontSize:10,minWidth:24,textAlign:"right"}}>#{s.step}</span>
          <span style={{fontSize:11,opacity:.85,minWidth:16,textAlign:"center"}}>{stepIcon}</span>
          <span style={{color:colour,fontWeight:600}}>{s.action||"?"}</span>
          <span style={{color:t.dim,flex:1,wordBreak:"break-word"}}>{(s.detail||"").slice(0,160)}</span>
        </div>;
      })}
      {env.summary && <div style={{marginTop:8,padding:"6px 8px",borderRadius:6,background:`${colour}10`,color:t.dim,fontStyle:"italic"}}>{env.summary}</div>}
      {env.error && <div style={{marginTop:8,padding:"6px 8px",borderRadius:6,background:`${t.err}10`,color:t.err}}>⚠ {env.error}</div>}
      {(env.manifest_missing||[]).length>0 && <div style={{marginTop:6,fontSize:10,color:t.mut}}>missing: {(env.manifest_missing||[]).slice(0,8).join(", ")}</div>}
      {role==="qa" && env.answer && <div style={{marginTop:10,padding:"10px 12px",borderRadius:8,background:`${colour}08`,border:`1px solid ${colour}22`,color:t.dim,fontSize:12,lineHeight:1.6}}>
        {md?<MDWrap>{md(env.answer)}</MDWrap>:<pre style={{margin:0,whiteSpace:"pre-wrap",fontFamily:"inherit"}}>{env.answer}</pre>}
        {env.looks_like_change_request && <div style={{marginTop:8,padding:"4px 8px",borderRadius:5,background:`${t.acc}18`,color:t.acc,fontSize:10,fontWeight:600,display:"inline-block"}}>⚡ Flagged as change request</div>}
        {(env.files_examined||[]).length>0 && <div style={{marginTop:8,fontSize:10,color:t.mut}}>📂 examined: {(env.files_examined||[]).slice(0,5).join(", ")}{(env.files_examined||[]).length>5?` +${(env.files_examined||[]).length-5} more`:""}</div>}
      </div>}
    </div>}
  </div>;
};

const WorkflowCard = ({workflow, t, font, onOpenArtifact})=>{
  const [wf,setWf]=useState(workflow);
  const [artifacts,setArtifacts]=useState([]);
  useEffect(()=>{setWf(workflow);},[workflow?.id,workflow?.updated_at]);
  useEffect(()=>{
    if(!wf?.id)return;
    const active=["planning","reviewing","fixing","accepting"].includes(wf.state);
    if(!active)return;
    let stop=false;
    const tick=async()=>{
      try{
        const r=await fetch(`${API}/api/coder/workflows/${wf.id}`);
        if(r.ok&&!stop)setWf(await r.json());
      }catch{}
    };
    const id=setInterval(tick,5000);tick();
    return()=>{stop=true;clearInterval(id);};
  },[wf?.id,wf?.state]);
  useEffect(()=>{
    if(!wf?.id)return;
    let stop=false;
    fetch(`${API}/api/artifacts?workflow_id=${encodeURIComponent(wf.id)}&limit=3`)
      .then(r=>r.ok?r.json():[])
      .then(d=>{if(!stop)setArtifacts(Array.isArray(d)?d:[]);})
      .catch(()=>{});
    return()=>{stop=true;};
  },[wf?.id,wf?.artifact_status,wf?.updated_at]);
  if(!wf)return null;
  const state=wf.state||"planning";
  const artifact=wf.artifact_status||"not_ready";
  const colour=state==="accepted"||artifact==="delivered"?t.ok:state==="blocked"||state==="cancelled"?t.err:artifact==="partial_delivered"?"#e9a45a":t.acc;
  const modeLabel=(wf.mode||"workflow").replaceAll("_"," ");
  const contract=wf.contract_json||{};
  return <div style={{marginTop:6,marginBottom:6,borderRadius:10,border:`1px solid ${colour}38`,background:`${colour}0c`,padding:"7px 12px",fontFamily:font,maxWidth:"100%"}}>
    <div style={{display:"flex",alignItems:"center",gap:8,minWidth:0}}>
      <span style={{fontSize:13}}>🧭</span>
      <span style={{fontWeight:700,fontSize:11,color:colour,letterSpacing:.3}}>Workflow</span>
      <span style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>· {modeLabel}</span>
      <span style={{fontSize:10,color:colour}}>· {state}</span>
      <span style={{fontSize:10,color:t.mut}}>· artifact: {artifact}</span>
      {artifacts.length>0&&<a href={artifacts[0].id?`${API}/api/artifacts/${artifacts[0].id}/download`:`${API}${artifacts[0].url}`} download={artifacts[0].filename} onClick={e=>e.stopPropagation()} style={{marginLeft:"auto",fontSize:10,color:t.ok,textDecoration:"none",border:`1px solid ${t.ok}35`,borderRadius:6,padding:"2px 7px",fontWeight:700}}>download</a>}
      {artifacts.length>0&&onOpenArtifact&&<button onClick={e=>{e.stopPropagation();onOpenArtifact(artifacts[0].id);}} style={{fontSize:10,color:t.acc,background:`${t.acc}10`,border:`1px solid ${t.acc}35`,borderRadius:6,padding:"2px 7px",fontWeight:800,cursor:"pointer",fontFamily:font}}>details</button>}
    </div>
    <div style={{display:"flex",gap:8,flexWrap:"wrap",marginTop:5,fontSize:10,color:t.mut}}>
      {wf.project_id&&<span>project: {wf.project_id}</span>}
      {contract.build_system&&<span>build: {contract.build_system}</span>}
      {contract.test_cmd&&<span>test: {contract.test_cmd.slice(0,60)}</span>}
      {wf.active_run_id&&<span>run: {wf.active_run_id}</span>}
    </div>
  </div>;
};

// Pluck unique run_ids out of a list of saved/live events. Each is an
// independent agent invocation that should render its own RunCard.
const _runIdsFromEvents = (evts)=>{
  if(!Array.isArray(evts)) return [];
  const seen = new Set();
  const out = [];
  for(const e of evts){
    const rid = e?.data?.run_id || e?.run_id;
    if(rid && !seen.has(rid)){seen.add(rid); out.push(rid);}
  }
  return out;
};
const _metaObj = (meta)=>{
  if(!meta)return{};
  if(typeof meta==="string"){try{return JSON.parse(meta)||{};}catch{return{};}}
  return meta||{};
};
const _qsHref = (url)=>{
  const raw=String(url||"").trim();
  if(!raw)return "";
  return /^https?:\/\//i.test(raw)?raw:`https://${raw}`;
};
const _qsHost = (url)=>{
  try{return new URL(_qsHref(url)).hostname.replace(/^www\./,"");}catch{return "";}
};
const _qsUrlKey = (url)=>{
  try{
    const u=new URL(_qsHref(url));
    u.hash="";
    u.hostname=u.hostname.replace(/^www\./,"");
    u.pathname=u.pathname.replace(/\/+$/,"")||"/";
    return u.toString().replace(/\/$/,"").toLowerCase();
  }catch{return String(url||"").trim().replace(/\/+$/,"").toLowerCase();}
};
const _qsFaviconUrl = (urlOrHost, size=32)=>{
  const host=_qsHost(urlOrHost)||String(urlOrHost||"").replace(/^www\./,"");
  return host?`https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=${size}`:"";
};
const _quickSearchPayloadFromEvents = (evts, fallbackResults=[])=>{
  const qs=(Array.isArray(evts)?evts:[]).filter(e=>e?.type==="search_results"&&e?.data?.source==="quick_search").pop()?.data;
  if(qs?.results?.length)return qs;
  return fallbackResults?.length?{source:"quick_search",results:fallbackResults}:null;
};
const _quickSearchResults = (payload)=>{
  const seen=new Set();
  const out=[];
  for(const r of payload?.results||[]){
    const href=_qsHref(r?.url);
    const key=_qsUrlKey(href);
    if(!href||seen.has(key))continue;
    seen.add(key);
    out.push({...r,url:href,domain:_qsHost(href)});
  }
  return out;
};
const _quickSourceForUrl = (url, results=[])=>{
  const key=_qsUrlKey(url);
  if(!key)return null;
  return _quickSearchResults({results}).find(r=>_qsUrlKey(r.url)===key)||null;
};
const _quickSearchCitationSources = (payload)=>{
  const out=[];
  for(const [idx,r] of (payload?.results||[]).entries()){
    const href=_qsHref(r?.url);
    const domain=_qsHost(href);
    out.push({
      ...r,
      kind:"quick_search",
      n:idx+1,
      url:href,
      domain,
      title:r?.title||domain||"Source",
      snippet:r?.snippet||"",
    });
  }
  return out;
};
function SourceFavicon({url,domain,size=18,theme,title}){
  const [failed,setFailed]=useState(false);
  const host=domain||_qsHost(url);
  const initial=(host||"?").slice(0,1).toUpperCase();
  const bg=theme?`${theme.surface}DD`:"#111";
  const border=theme?`1px solid ${theme.brd}55`:"1px solid rgba(255,255,255,.18)";
  const box={width:size,height:size,borderRadius:Math.max(4,Math.floor(size*.22)),border,background:bg,display:"inline-flex",alignItems:"center",justifyContent:"center",flexShrink:0,overflow:"hidden",boxSizing:"border-box"};
  if(!host||failed)return <span title={title||host} style={{...box,color:theme?.acc||"currentColor",fontSize:Math.max(8,Math.floor(size*.52)),fontWeight:800}}>{initial}</span>;
  return <span title={title||host} style={box}>
    <img src={_qsFaviconUrl(host,size>=24?64:32)} alt="" style={{width:"100%",height:"100%",display:"block"}} onError={()=>setFailed(true)}/>
  </span>;
}
function QuickSourceInlineChip({href,label,source,t,font,onPreview}){
  const host=source?.domain||_qsHost(href);
  const text=String(label||host||"source").replace(/\s+/g," ").trim();
  const short=text.length>34?`${text.slice(0,31)}...`:text;
  const title=[source?.title||text,host,source?.snippet].filter(Boolean).join("\n");
  return <span style={{display:"inline-flex",alignItems:"center",gap:0,margin:"2px 1px",verticalAlign:"middle",maxWidth:"100%"}}>
    <a href={href} target="_blank" rel="noopener" title={title} style={{display:"inline-flex",alignItems:"center",gap:5,padding:"3px 8px",background:`${t.f1}12`,border:`1px solid ${t.f1}38`,borderRadius:onPreview?"7px 0 0 7px":7,color:t.f1,textDecoration:"none",fontSize:11,fontWeight:750,maxWidth:260,overflow:"hidden",whiteSpace:"nowrap",boxSizing:"border-box"}}>
      <SourceFavicon url={href} domain={host} size={14} theme={t}/>
      <span style={{overflow:"hidden",textOverflow:"ellipsis"}}>{short}</span>
      <span style={{fontSize:8,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.4}}>{host}</span>
    </a>
    {onPreview&&<button type="button" onClick={()=>onPreview(source?.title||text,href)} title="Preview source" style={{display:"inline-flex",alignItems:"center",justifyContent:"center",height:24,padding:"0 7px",background:`${t.acc}10`,border:`1px solid ${t.f1}38`,borderLeft:"none",borderRadius:"0 7px 7px 0",color:t.acc,cursor:"pointer",fontFamily:font,fontSize:10}}>
      <IC.Search/>
    </button>}
  </span>;
}
function QuickSearchSourcesPanel({payload,t,font,defaultOpen=false}){
  const [open,setOpen]=useState(!!defaultOpen);
  const results=useMemo(()=>_quickSearchResults(payload),[payload]);
  if(!results.length)return null;
  const domains=[];
  for(const r of results){if(r.domain&&!domains.includes(r.domain))domains.push(r.domain);}
  const query=payload?.query||"Quick Search";
  const freshness=payload?.freshness_mode||payload?.resolved_date||"";
  const shown=results.slice(0,18);
  const meta=[freshness,domains.slice(0,3).join(", ")].filter(Boolean).join(" - ");
  return <div style={{margin:"12px 0 4px",border:`1px solid ${t.brd}44`,borderRadius:8,background:`${t.surface}42`,overflow:"hidden",boxShadow:"none"}}>
    <button type="button" aria-expanded={open} onClick={()=>setOpen(o=>!o)} style={{width:"100%",border:"none",background:`${t.surface}58`,color:t.dim,cursor:"pointer",fontFamily:font,padding:"7px 10px",display:"grid",gridTemplateColumns:"auto 1fr auto",alignItems:"center",gap:9,textAlign:"left"}}>
      <span style={{display:"inline-flex",alignItems:"center",gap:6,color:t.f1,fontSize:10,fontWeight:900,textTransform:"uppercase",letterSpacing:.6,whiteSpace:"nowrap"}}>
        <span style={{transition:"transform .16s",transform:open?"rotate(90deg)":"rotate(0)",display:"inline-block",fontSize:9}}>▶</span>
        <IC.Search/> Sources
        <span style={{color:t.mut,fontWeight:800,letterSpacing:0}}>({results.length})</span>
      </span>
      <span style={{minWidth:0,display:"flex",flexDirection:"column",gap:1}}>
        <span style={{fontSize:11,color:t.text,fontWeight:750,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{query}</span>
        {meta&&<span style={{fontSize:9,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{meta}</span>}
      </span>
      <span style={{display:"flex",alignItems:"center",justifyContent:"flex-end",minWidth:0,paddingLeft:4}}>
        {results.slice(0,6).map((r,i)=><span key={`${r.url}-${i}`} style={{marginLeft:i?-5:0,position:"relative",zIndex:10-i}}>
          <SourceFavicon url={r.url} domain={r.domain} size={20} theme={t} title={r.domain}/>
        </span>)}
      </span>
    </button>
    {open&&<div style={{padding:9,borderTop:`1px solid ${t.brd}28`,display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(240px,1fr))",gap:7}}>
      {shown.map((r,i)=>{
        const isMedia=r.type==="youtube"||r.type==="image";
        const thumb=r.thumbnail&&isMedia;
        const badges=[r.published_date,r.freshness,r.source_tier].filter(Boolean).join(" - ");
        return <a key={`${r.url}-${i}`} href={r.url} target="_blank" rel="noreferrer" title={r.score_reason||r.title||r.url} style={{textDecoration:"none",display:"grid",gridTemplateColumns:thumb?"24px minmax(0,1fr) 54px":"24px minmax(0,1fr)",gap:8,alignItems:"start",minWidth:0,padding:"8px 9px",borderRadius:7,border:`1px solid ${t.brd}32`,background:`${t.bgDeep}55`,color:t.dim,boxSizing:"border-box"}}>
          <SourceFavicon url={r.url} domain={r.domain} size={22} theme={t} title={r.domain}/>
          <span style={{minWidth:0,display:"flex",flexDirection:"column",gap:3}}>
            <span style={{fontSize:11,fontWeight:800,color:t.text,lineHeight:1.35,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>{r.title||r.domain||"Source"}</span>
            {r.snippet&&<span style={{fontSize:10,color:t.mut,lineHeight:1.4,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>{r.snippet}</span>}
            <span style={{display:"flex",alignItems:"center",gap:5,minWidth:0,fontSize:9,color:t.dim}}>
              <span style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",fontWeight:750,color:r.type==="youtube"?t.err:r.type==="image"?t.acc:t.f1}}>{r.domain||"web"}</span>
              {badges&&<span style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",color:t.mut}}>{badges}</span>}
              {typeof r.score==="number"&&<span style={{marginLeft:"auto",color:t.mut,flexShrink:0}}>{Math.round(r.score)}</span>}
            </span>
          </span>
          {thumb&&<img src={proxiedImageUrl(r.thumbnail)} alt="" loading="lazy" referrerPolicy="no-referrer" style={{width:54,height:42,objectFit:"cover",borderRadius:6,border:`1px solid ${t.brd}33`,background:t.bgDeep}} onError={e=>{e.currentTarget.style.display="none";}}/>}
        </a>;
      })}
    </div>}
  </div>;
}
const _DAEDALUS_TOOLS = new Set([
  "plan_project","generate_code","run_review","run_acceptance_review",
  "run_fixer","run_aider_fix","ask_project","download_project","download_file",
  "list_files","read_file","write_file","run_shell",
]);
const _DAEDALUS_ACTIVE_WORKFLOW_STATES = new Set(["queued","pending","running","planning","reviewing","fixing","accepting"]);
const _eventsHaveDaedalus = (evts)=>{
  if(!Array.isArray(evts))return false;
  return evts.some(e=>{
    const tool=e?.data?.tool||"";
    return _DAEDALUS_TOOLS.has(tool) || !!(e?.data?.run_id||e?.run_id);
  });
};
const _daedalusRunIdsForMessage = (meta,isLast,liveEvts)=>{
  const saved=Array.isArray(meta?.saved_events)?meta.saved_events:[];
  const ridsFromMeta=Array.isArray(meta?.run_ids)?meta.run_ids:[];
  const ridsFromEvts=_runIdsFromEvents(isLast&&liveEvts?.length?liveEvts:saved);
  const seen=new Set(),out=[];
  for(const rid of [...ridsFromMeta,...ridsFromEvts]){
    if(rid&&!seen.has(rid)){seen.add(rid);out.push(rid);}
  }
  return out;
};
const _isDaedalusOutput = ({meta={},savedEvents=[],liveEvents=[],runIds=[],workflows=[]}={})=>{
  return !!(
    runIds.length ||
    (Array.isArray(workflows)&&workflows.some(w=>_DAEDALUS_ACTIVE_WORKFLOW_STATES.has(String(w?.state||"").toLowerCase()))) ||
    _eventsHaveDaedalus(savedEvents) ||
    _eventsHaveDaedalus(liveEvents) ||
    (Array.isArray(meta.run_ids)&&meta.run_ids.length)
  );
};

const _activityIsTerminal = (status)=>["done","complete","completed","success","succeeded","error","failed","cancelled","canceled"].includes(String(status||"").toLowerCase());
const _activityStatusKey = (status)=>{
  const s=String(status||"queued").toLowerCase();
  if(["done","complete","completed","success","succeeded"].includes(s))return"complete";
  if(["error","failed"].includes(s))return"failed";
  if(["cancelled","canceled"].includes(s))return"canceled";
  if(["indexing","embedding","reindexing"].includes(s))return"indexing";
  if(["downloading","uploading","running","creating","working","patching"].includes(s))return"running";
  return"queued";
};
const _activityGroupKey = (item)=>{
  const kind=String(item?.kind||"").toLowerCase();
  const name=String(item?.name||"").toLowerCase();
  if(kind==="chat"||kind==="run"||name.includes("project upload"))return"Chat";
  if(kind==="indexing"||kind==="rag"||name.includes("kb upload")||name.includes("knowledge")||name.includes("rag "))return"Knowledge";
  if(kind==="cleanup"||name.includes("cleanup")||name.includes("purge"))return"Cleanup";
  if(kind==="model"||kind==="download"||kind==="patch"||name.includes("model")||name.includes("template")||name.includes("ollama")||name.includes("huggingface"))return"Models";
  return"Tools";
};
const _fmtElapsed = (start, now=Date.now())=>{
  if(!start)return"";
  const s=Math.max(0,Math.floor((now-start)/1000));
  if(s>=3600)return`${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
  if(s>=60)return`${Math.floor(s/60)}m ${s%60}s`;
  return`${s}s`;
};
const _parseUtcishMs = (value)=>{
  if(!value)return 0;
  if(typeof value==="number")return value>1000000000000?value:value*1000;
  const s=String(value).trim();
  const ms=Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)?s:`${s}Z`);
  return Number.isFinite(ms)?ms:0;
};
const _stripStatusEmoji = (txt="")=>String(txt||"").replace(/^[\p{Extended_Pictographic}\u200d\ufe0f\s]+/u,"").trim();
const _phaseFromEvent = (ev)=>{
  if(!ev)return null;
  const d=ev.data||{};
  const tool=d.tool||ev.type;
  const status=_stripStatusEmoji(d.label||d.status||"");
  if(ev.type==="thinking"||ev.type==="thought_done")return{phase:"thinking",label:ev.type==="thought_done"?"Reasoning complete":"Thinking",detail:status,pct:d.pct};
  if(ev.type==="search_results"&&d.source==="quick_search")return{phase:"searching",label:"Search complete",detail:`${(d.results||[]).length} sources`,pct:100};
  if(ev.type==="tool_error"||ev.type==="error")return{phase:"failed",label:"Failed",detail:status||tool,pct:d.pct};
  if(ev.type==="tool_end"||ev.type==="tool_done")return{phase:"complete",label:"Tool complete",detail:status||tool,pct:d.pct||100};
  if(ev.type==="tool_start"||ev.type==="tool_progress"||ev.type==="tool_status")return{phase:"tool",label:(tool||"tool").replaceAll("_"," "),detail:status,pct:d.pct};
  return null;
};

function ResearchLiveStatus({t,font,events=[],metrics={},sources=[],researchRunning=false,elapsedLabel="--",eventLabel}){
  const visible=(events||[]).filter(e=>e.type!=="research_token");
  const latestPhase=[...visible].reverse().find(e=>e.type==="research_phase");
  const phaseKey=latestPhase?.data?.phase||(
    researchRunning?"planning":(visible.some(e=>e.type==="research_done")?"complete":"idle")
  );
  const phaseMap={
    planning:["Route Plot","Mapping questions, criteria, and weak spots before the search fanout."],
    context:["Context Load","Pulling notes, files, and knowledge-base fragments into the evidence bay."],
    search:["Signal Sweep","Launching query waves and sorting early leads from background noise."],
    reading:["Page Dive","Opening high-value sources and extracting the clean text worth citing."],
    extracting:["Evidence Distill","Compressing source notes into claim-level findings with source IDs."],
    audit:["Claim Pressure Test","Stress-testing citations, caveats, missing evidence, and contradictions."],
    synthesis:["Report Forge","Writing the final brief; live markdown will stream into this pane."],
    complete:["Report Ready","Final synthesis landed."],
    idle:["Research Bay","No report body has been rendered yet."],
  };
  const [phaseTitle,phaseDetail]=phaseMap[phaseKey]||phaseMap.idle;
  const pct=Number.isFinite(Number(latestPhase?.data?.pct))?Math.max(0,Math.min(100,Number(latestPhase.data.pct))):(researchRunning?8:0);
  const metricLine=[
    ["Sources",sources.length||metrics.source_count||0,metrics.target_sources],
    ["Pages",metrics.pages_read||0,metrics.target_pages],
    ["Searches",metrics.searches||0,metrics.target_queries],
    ["Elapsed",elapsedLabel,null],
  ];
  const recent=visible.filter(e=>["research_phase","research_source_found","research_source_read","research_finding","research_audit","research_done","research_error"].includes(e.type)).slice(-6);
  return <div style={{border:`1px solid ${t.brd}33`,borderRadius:8,padding:22,background:`${t.surface}66`,color:t.mut,fontSize:12,lineHeight:1.55,animation:"fadeIn .2s"}}>
    <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:12}}>
      <div style={{width:34,height:34,borderRadius:8,border:`1px solid ${t.acc}33`,background:`${t.acc}12`,display:"flex",alignItems:"center",justifyContent:"center",color:t.acc,flexShrink:0}}><IC.Search/></div>
      <div style={{minWidth:0}}>
        <div style={{fontSize:11,fontWeight:900,textTransform:"uppercase",letterSpacing:.8,color:t.acc}}>{phaseTitle}</div>
        <div style={{fontSize:12,color:t.text,lineHeight:1.35}}>{latestPhase?.data?.detail||phaseDetail}</div>
      </div>
      {researchRunning&&<span style={{marginLeft:"auto",fontSize:9,padding:"3px 7px",borderRadius:7,border:`1px solid ${t.f1}33`,color:t.f1,background:`${t.f1}10`,textTransform:"uppercase",letterSpacing:.6}}>live</span>}
    </div>
    <div style={{height:6,borderRadius:99,background:`${t.bgDeep}cc`,border:`1px solid ${t.brd}24`,overflow:"hidden",marginBottom:12}}>
      <div style={{height:"100%",width:`${pct}%`,background:`linear-gradient(90deg, ${t.acc}, ${t.f1})`,boxShadow:`0 0 18px ${t.acc}66`,transition:"width .45s ease"}}/>
    </div>
    <div style={{display:"grid",gridTemplateColumns:"repeat(4,minmax(0,1fr))",gap:7,marginBottom:14}}>
      {metricLine.map(([label,value,target])=><div key={label} style={{border:`1px solid ${t.brd}24`,borderRadius:7,padding:"7px 8px",background:`${t.bgDeep}66`,minWidth:0}}>
        <div style={{fontSize:13,fontWeight:900,color:label==="Elapsed"?t.mut:t.text,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{target?`${value}/${target}`:value}</div>
        <div style={{fontSize:8,color:t.dim,textTransform:"uppercase",letterSpacing:.6,marginTop:2}}>{label}</div>
      </div>)}
    </div>
    <div style={{display:"flex",flexDirection:"column",gap:6}}>
      {recent.length?recent.map((ev,i)=>{const c=ev.type==="research_error"?t.err:ev.type==="research_done"?t.ok:ev.type==="research_source_read"?t.f1:ev.type==="research_audit"?t.warm:t.acc;return <div key={`${ev.type}-${i}`} style={{display:"flex",alignItems:"flex-start",gap:8,color:t.dim,fontSize:10,lineHeight:1.35}}>
        <span style={{width:6,height:6,borderRadius:"50%",background:c,boxShadow:`0 0 8px ${c}66`,marginTop:4,flexShrink:0}}/>
        <span style={{overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>{eventLabel?eventLabel(ev,i):(ev.type||"activity")}</span>
      </div>}):<div style={{fontSize:10,color:t.dim}}>Awaiting first research event.</div>}
    </div>
  </div>;
}

// Inline tool status: shows latest pill, expands to show all
const ToolStatus = ({evts,t,expandedPill,setExpandedPill,onPreview,onOpenArtifact,historical,md,savedEvts,msgContent})=>{
  const [showAll,setShowAll]=useState(false);
  if(!evts.length) return null;

  // Stable string key for expandedPill — avoids collapse when new events arrive
  // Guard: if evts only has source_links/search_results (no tool/thinking), merged will be empty
  const getPillKey = (ev, idx) => {
    if(ev.type==="thinking"||ev.type==="thought_done") return "thinking";
    if(ev.type==="complete") return "streaming";
    if(idx!==undefined) return `${ev.data?.tool||ev.type}_${idx}`;
    return ev.data?.tool || ev.type;
  };

  // Collapse events: keep only meaningful pills
  const merged = [];

  // First pass: group by category
  const thinkingEvts = evts.filter(e=>e.type==="thinking"||e.type==="thought_done");
  const toolEvts = evts.filter(e=>e.type==="tool_start"||e.type==="tool_progress"||e.type==="tool_end"||e.type==="tool_done"||e.type==="tool_error");
  const streamEvts = evts.filter(e=>e.type==="complete");
  const otherEvts = evts.filter(e=>!["thinking","thought_done","tool_start","tool_progress","tool_end","tool_done","tool_error","complete","code_output","file_ready","search_results","source_links","kb_sources","codeagent_note"].includes(e.type));

  // Thinking: collapse to just the final state
  if(thinkingEvts.length){
    const done = thinkingEvts.find(e=>e.type==="thought_done");
    merged.push(done || thinkingEvts[thinkingEvts.length-1]);
  }

  // Tools: pair each start→end sequentially (not by name, so repeated tools each get a step)
  const _pendingStarts = []; // stack of unmatched tool_start events
  for(const e of toolEvts){
    const key = e.data?.tool||"unknown";
    if(e.type==="tool_start"){
      _pendingStarts.push({key, evt: e});
    } else if(e.type==="tool_progress"){
      // Update the latest pending start for this tool
      const idx = [..._pendingStarts].reverse().findIndex(p=>p.key===key);
      if(idx>=0) _pendingStarts[_pendingStarts.length-1-idx].evt = e;
    } else { // tool_end, tool_done, tool_error
      // Match with the newest pending start for this tool. Some tools emit an
      // outer chat-level start and then an inner run-level start with run_id.
      const revIdx = [..._pendingStarts].reverse().findIndex(p=>p.key===key);
      if(revIdx>=0) _pendingStarts.splice(_pendingStarts.length-1-revIdx, 1);
      merged.push(e);
    }
  }
  // Remaining unmatched starts
  for(const {evt} of _pendingStarts){
    if(historical){
      merged.push({...evt, type:"tool_done", data:{...evt.data, status:"done"}});
    }else{
      merged.push(evt);
    }
  }

  // Streaming: collapse to final state
  if(streamEvts.length){
    const done = streamEvts.find(e=>e.type==="complete");
    merged.push(done || streamEvts[streamEvts.length-1]);
  }

  // Other events pass through
  merged.push(...otherEvts);

  const latest = merged.length ? merged[merged.length-1] : null;
  const completedCount = merged.filter(e=>e.type==="tool_end"||e.type==="tool_done"||e.type==="complete"||e.type==="thought_done").length;
  const totalSteps = merged.length;

  const codeOutputEvts = evts.filter(e=>e.type==="code_output");
  const fileReadyEvts = evts.filter(e=>e.type==="file_ready");
  const kbSourceEvts = evts.filter(e=>e.type==="kb_sources");
  const codeAgentNoteEvts = evts.filter(e=>e.type==="codeagent_note");
  // Skip search_agent's events here — Quick Search sources render as the
  // per-answer drawer. SearchResultCards is for the `research` tool's
  // mid-message results, which don't carry source="quick_search".
  const searchResultEvts = evts.filter(e=>e.type==="search_results"&&e.data?.source!=="quick_search");
  const sourceLinkEvts = evts.filter(e=>e.type==="source_links");

  // Surface architecture plans above the pill row. Reasoning stays in the
  // normal brain-icon thinking pill so it does not render twice.
  // Plan: latest plan_project tool_end with detail.plan
  // Live `evts` is a sliding window (last 200) so on long runs the early
  // plan_project / thought_done events get evicted. `savedEvts` (passed when
  // the parent has msg.metadata.saved_events) is the durable full record;
  // we merge it in here to make sure the panels survive multi-cycle runs.
  const _parseDetail = (ev)=>{const d=ev?.data?.detail;if(!d)return null;try{return typeof d==="string"?JSON.parse(d):d;}catch{return null;}};
  const _evtPool = (savedEvts && savedEvts.length) ? savedEvts.concat(evts) : evts;
  const _planEvts = _evtPool.filter(e=>(e.type==="tool_end"||e.type==="tool_done")&&e.data?.tool==="plan_project"&&_parseDetail(e)?.plan);
  const planText = _planEvts.length ? (_parseDetail(_planEvts[_planEvts.length-1])?.plan||"") : "";
  const isStreaming = !historical;
  const Panels = planText ? <div style={{display:"flex",flexDirection:"column",gap:0,marginBottom:4}}>
    {planText && <PlanPanel plan={planText} t={t} streaming={isStreaming} md={md}/>}
  </div> : null;

  // Historical renders: the per-message block at msg.metadata.saved_events renders
  // collapsible code output, so ToolStatus shouldn't render another copy here.
  const CodeOutputCards = ()=>historical?null:<>{codeOutputEvts.map((e,i)=>{
    const d=e.data||{};
    return <div key={i} style={{marginTop:6,borderRadius:8,overflow:"hidden",border:`1px solid ${d.success?t.ok:t.err}33`,background:t.bgDeep,fontSize:11}}>
      <div style={{display:"flex",alignItems:"center",gap:6,padding:"5px 10px",background:`${d.success?t.ok:t.err}10`,borderBottom:`1px solid ${d.success?t.ok:t.err}22`}}>
        <span>{d.success?"🎯":"❌"}</span>
        <span style={{fontWeight:600,color:d.success?t.ok:t.err}}>{d.language} output</span>
        <span style={{color:t.mut,marginLeft:"auto"}}>{d.exec_time?.toFixed?.(1)}s</span>
      </div>
      {d.stdout&&<>
        <div style={{padding:"4px 10px 2px",fontSize:10,color:t.ok,fontWeight:600}}>stdout</div>
        <pre style={{margin:0,padding:"4px 10px 8px",whiteSpace:"pre-wrap",color:t.dim,maxHeight:200,overflowY:"auto",fontSize:11}}>{d.stdout}</pre>
      </>}
      {d.stderr&&<>
        <div style={{padding:"4px 10px 2px",fontSize:10,color:t.err,fontWeight:600}}>stderr</div>
        <pre style={{margin:0,padding:"4px 10px 8px",whiteSpace:"pre-wrap",color:t.err,maxHeight:120,overflowY:"auto",fontSize:11}}>{d.stderr}</pre>
      </>}
    </div>;
  })}</>;

  const _fileIcon=(fn)=>{
    const ext=(fn||"").split(".").pop().toLowerCase();
    const m={py:"🐍",python:"🐍",js:"📜",mjs:"📜",jsx:"⚛️",ts:"🔷",tsx:"🔷",java:"☕",jar:"☕",rs:"🦀",go:"🐹",c:"⚙️",cpp:"⚙️",h:"⚙️",cs:"🟣",rb:"💎",php:"🐘",swift:"🐦",kt:"🟠",lua:"🌙",r:"📊",sh:"🖥️",bash:"🖥️",zsh:"🖥️",html:"🌐",css:"🎨",scss:"🎨",json:"📋",yaml:"📋",yml:"📋",xml:"📋",toml:"📋",md:"📝",txt:"📄",csv:"📊",sql:"🗃️",dockerfile:"🐳",png:"🖼️",jpg:"🖼️",jpeg:"🖼️",gif:"🖼️",svg:"🖼️",pdf:"📕",zip:"📦",tar:"📦",gz:"📦"};
    return m[ext]||"📁";
  };
  const FileReadyButtons = ()=>{
    // Deduplicate by filename — keep latest URL, count updates
    const fileMap={};
    fileReadyEvts.forEach(e=>{const d=e.data||{};if(!d.filename)return;if(!fileMap[d.filename])fileMap[d.filename]={d,count:1};else{fileMap[d.filename].d=d;fileMap[d.filename].count++;}});
    return <>{Object.values(fileMap).map(({d,count},i)=>{
    const isImg=d.is_image||/\.(png|jpe?g|gif|svg|webp)$/i.test(d.filename);
    // The backend now streams generated-image markdown inline at the point of
    // generation, so the image already renders inside the message body. Skip the
    // duplicate <img> here when the message content already references this URL;
    // keep the download/preview/artifact buttons. Older messages (no inline
    // markdown) and post-clear refinement paths fall back to rendering it here.
    const inlineAlready=isImg&&d.url&&typeof msgContent==="string"&&msgContent.includes(d.url);
    return <div key={i} style={{marginTop:5}}>
      {isImg&&!inlineAlready&&<img src={`${API}${d.url}`} alt={d.filename} style={{maxWidth:"100%",maxHeight:380,borderRadius:8,display:"block",margin:"6px 0",border:`1px solid ${t.brd}22`,cursor:"pointer"}} onClick={()=>onPreview&&onPreview(d.filename,d.url)} onError={e=>e.target.style.display="none"}/>}
      <div style={{display:"inline-flex",alignItems:"center",gap:4,marginTop:isImg?2:0}}>
      <a href={d.artifact_id?`${API}/api/artifacts/${d.artifact_id}/download`:`${API}${d.url}`} download={d.filename}
        style={{display:"inline-flex",alignItems:"center",gap:7,padding:"7px 14px",background:`${t.ok}15`,border:`1px solid ${t.ok}40`,borderRadius:"10px 0 0 10px",color:t.ok,textDecoration:"none",fontSize:12,fontWeight:700,cursor:"pointer"}}>
        <span style={{fontSize:16}}>{_fileIcon(d.filename)}</span><span>{d.filename}</span>{count>1&&<span style={{fontSize:9,background:t.acc,color:t.bg,borderRadius:8,padding:"1px 5px",fontWeight:700,marginLeft:2}}>v{count}</span>}<span style={{fontSize:10,opacity:.6}}>⬇</span>
      </a>
      {onPreview&&<button onClick={()=>onPreview(d.filename,d.url)} title="Preview" style={{padding:"7px 10px",background:`${t.acc}15`,border:`1px solid ${t.acc}40`,borderRadius:"0 10px 10px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:13,display:"flex",alignItems:"center"}}>👁</button>}
      {d.artifact_id&&onOpenArtifact&&<button onClick={()=>onOpenArtifact(d.artifact_id)} title="Open in Artifacts" style={{padding:"7px 10px",background:`${t.pink}12`,border:`1px solid ${t.pink}35`,borderRadius:8,color:t.pink,cursor:"pointer",fontSize:11,fontFamily:"inherit",fontWeight:800,display:"flex",alignItems:"center"}}>Artifact</button>}
      </div>
    </div>;
  })}</>;};

  const CodeAgentNoteCards = ()=><>{codeAgentNoteEvts.map((e,i)=>{
    const d=e.data||{};
    const text=d.status||d.detail||"CodeAgent is starting a direct build.";
    return <div key={i} style={{marginTop:6,border:`1px solid ${t.acc}30`,background:`${t.acc}0F`,borderRadius:8,padding:"7px 10px",display:"flex",alignItems:"flex-start",gap:8,maxWidth:640}}>
      <span style={{fontSize:13,lineHeight:1.35,flexShrink:0}}>🧬</span>
      <div style={{minWidth:0}}>
        <div style={{fontSize:9,color:t.acc,textTransform:"uppercase",letterSpacing:.7,fontWeight:800,marginBottom:2}}>Direct build</div>
        <div style={{fontSize:11,color:t.dim,lineHeight:1.45}}>{text}</div>
      </div>
    </div>;
  })}</>;

  const KbSourceCards = ()=>{
    const latestKb = kbSourceEvts[kbSourceEvts.length-1];
    const sources = latestKb?.data?.sources||[];
    if(!sources.length)return null;
    const names=[...new Set(sources.map(s=>s.filename).filter(Boolean))];
    return <div style={{marginTop:6,border:`1px solid ${t.acc}28`,background:`${t.surface}42`,borderRadius:8,padding:"7px 10px",maxWidth:680}}>
      <div style={{fontSize:9,color:t.acc,textTransform:"uppercase",letterSpacing:.7,fontWeight:800,marginBottom:5,display:"flex",alignItems:"center",gap:6}}>
        <span>📚</span><span>Knowledge sources</span><span style={{color:t.mut,fontWeight:600,textTransform:"none",letterSpacing:0}}>· {sources.length} chunk{sources.length!==1?"s":""}</span>
      </div>
      <div style={{fontSize:11,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginBottom:5}}>
        {names.length?names.slice(0,4).join(", "):"Knowledge base context"}
        {names.length>4?` +${names.length-4} more`:""}
      </div>
      <div style={{display:"flex",flexDirection:"column",gap:4}}>
        {sources.slice(0,3).map((s,si)=><div key={`${s.filename||"source"}-${s.chunk_index||si}-${si}`} style={{display:"flex",gap:7,alignItems:"flex-start",fontSize:10,color:t.dim,lineHeight:1.35}}>
          <span style={{fontWeight:800,color:t.acc,flexShrink:0}}>[{s.n||si+1}]</span>
          <span style={{minWidth:0,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>
            <span style={{color:t.text,fontWeight:700}}>{s.filename||"source"}</span>
            {typeof s.chunk_index==="number"?<span style={{color:t.mut}}> chunk {s.chunk_index}</span>:null}
            {s.snippet?` - ${s.snippet}`:""}
          </span>
        </div>)}
      </div>
    </div>;
  };

  const SearchResultCards = ()=><>{searchResultEvts.map((e,ei)=>{
    const d=e.data||{};
    const results=d.results||[];
    if(!results.length)return null;
    return <div key={ei} style={{marginTop:10}}>
      <div style={{fontSize:9,color:t.mut,textTransform:"uppercase",letterSpacing:.5,marginBottom:6,display:"flex",alignItems:"center",gap:4}}>
        <IC.Search/><span>{d.query||"Search"}</span><span style={{opacity:.5}}>· {results.length} sources</span>
      </div>
      <div style={{display:"flex",gap:8,overflowX:"auto",paddingBottom:8}}>
        {results.map((r,ri)=>{
          const hn=(()=>{try{return new URL(r.url.startsWith("http")?r.url:"https://"+r.url).hostname.replace("www.","");}catch{return "";}})();
          const isYT=r.type==="youtube";
          const isImg=r.type==="image";
          const hasThumbnail=!!r.thumbnail;
          return <a key={ri} href={r.url} target="_blank" rel="noreferrer" style={{textDecoration:"none",display:"flex",flexDirection:"column",background:`${t.surface}CC`,border:`1px solid ${t.brd}44`,borderRadius:10,overflow:"hidden",cursor:"pointer",minWidth:155,maxWidth:190,flexShrink:0,transition:"border-color .15s"}}
            onMouseEnter={e=>e.currentTarget.style.borderColor=`${t.acc}88`} onMouseLeave={e=>e.currentTarget.style.borderColor=`${t.brd}44`}>
            <div style={{width:"100%",height:88,background:hasThumbnail?(isYT?"#0a0a0a":"transparent"):`linear-gradient(135deg,${t.bgDeep}DD,${t.surface}AA,${t.acc}18)`,position:"relative",overflow:"hidden",flexShrink:0,display:"flex",alignItems:"center",justifyContent:"center"}}>
              {hasThumbnail
                ?<img src={r.thumbnail} style={{width:"100%",height:"100%",objectFit:"cover",display:"block"}} alt="" onError={e=>{e.target.style.display="none";}}/>
                :<div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:4}}>
                  <img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=64`} style={{width:28,height:28,borderRadius:4,display:"block"}} alt="" onError={e=>{e.target.style.display="none";}}/>
                  <span style={{fontSize:9,color:t.mut,opacity:.6,letterSpacing:.5}}>{hn}</span>
                </div>}
              {isYT&&<div style={{position:"absolute",inset:0,display:"flex",alignItems:"center",justifyContent:"center",pointerEvents:"none"}}>
                <div style={{width:28,height:28,borderRadius:"50%",background:"rgba(255,0,0,.9)",display:"flex",alignItems:"center",justifyContent:"center",fontSize:11,color:"#fff",boxShadow:"0 2px 8px rgba(0,0,0,.5)"}}>▶</div>
              </div>}
              {hasThumbnail&&hn&&<div style={{position:"absolute",top:5,right:5,background:`${t.bg}CC`,backdropFilter:"blur(6px)",borderRadius:5,padding:3,display:"flex",alignItems:"center",justifyContent:"center",border:`1px solid ${t.brd}33`}}>
                <img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=32`} style={{width:16,height:16,borderRadius:2,display:"block"}} alt="" onError={e=>e.target.parentElement.style.display="none"}/>
              </div>}
            </div>
            <div style={{padding:"6px 8px",flex:1,display:"flex",flexDirection:"column",gap:2}}>
              <div style={{fontSize:10,fontWeight:600,color:t.text,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",lineHeight:1.35}}>{r.title}</div>
              {r.snippet&&<div style={{fontSize:9,color:t.mut,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",lineHeight:1.4}}>{r.snippet}</div>}
              <div style={{fontSize:8,color:isYT?t.err:isImg?t.acc:t.dim,marginTop:"auto",paddingTop:2}}>{isYT?"▶ YouTube":isImg?"🖼 Image":hn||"Web"}</div>
            </div>
          </a>;
        })}
      </div>
    </div>;
  })}</>;

  const SourceLinkCards = ()=>{
    const allLinks=sourceLinkEvts.flatMap(e=>(e.data?.links||[]));
    if(!allLinks.length)return null;
    const [slExpanded,setSlExpanded]=useState(false);
    const shown=slExpanded?allLinks:allLinks.slice(0,8);
    return <div style={{marginTop:10}}>
      <div style={{fontSize:9,color:t.warm,textTransform:"uppercase",letterSpacing:.5,marginBottom:6,display:"flex",alignItems:"center",gap:4}}>
        <span>🔗</span><span>Sources ({allLinks.length})</span>
      </div>
      <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
        {shown.map((lnk,i)=>{
          const hn=(()=>{try{return new URL(lnk.url).hostname.replace("www.","");}catch{return "";}})();
          const isPdf=lnk.url.toLowerCase().endsWith(".pdf");
          const title=(lnk.title||hn||lnk.url).slice(0,60);
          return <div key={i} style={{display:"inline-flex",alignItems:"center",gap:0}}>
            <a href={lnk.url} target="_blank" rel="noopener" style={{display:"inline-flex",alignItems:"center",gap:5,padding:"5px 10px",background:`${t.warm}10`,border:`1px solid ${t.warm}33`,borderRadius:"8px 0 0 8px",color:t.warm,textDecoration:"none",fontSize:10,fontWeight:600,maxWidth:220,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",cursor:"pointer",transition:"all .15s"}}
              onMouseEnter={e=>e.currentTarget.style.background=`${t.warm}20`} onMouseLeave={e=>e.currentTarget.style.background=`${t.warm}10`}>
              {isPdf?<span>📄</span>:<img src={`https://www.google.com/s2/favicons?domain=${hn}&sz=16`} style={{width:12,height:12,borderRadius:2}} alt="" onError={e=>e.target.style.display="none"}/>}
              <span>{title}</span>
            </a>
            {onPreview&&<button onClick={()=>onPreview(title,lnk.url)} title="Preview in panel"
              style={{padding:"5px 8px",background:`${t.acc}12`,border:`1px solid ${t.acc}33`,borderRadius:"0 8px 8px 0",borderLeft:"none",color:t.acc,cursor:"pointer",fontSize:11,display:"flex",alignItems:"center",transition:"all .15s"}}
              onMouseEnter={e=>e.currentTarget.style.background=`${t.acc}25`} onMouseLeave={e=>e.currentTarget.style.background=`${t.acc}12`}>👁</button>}
          </div>;
        })}
      </div>
      {allLinks.length>8&&<button onClick={()=>setSlExpanded(p=>!p)} style={{background:`${t.surface}60`,border:`1px solid ${t.brd}22`,color:t.mut,padding:"2px 8px",borderRadius:10,fontSize:9,cursor:"pointer",fontFamily:"inherit",marginTop:4}}>{slExpanded?`Show less ▴`:`+${allLinks.length-8} more ▾`}</button>}
    </div>;
  };


  if(!showAll){
    const activeThinking = merged.find(e=>e.type==="thinking");
    const activeTool = [...merged].reverse().find(e=>e.type==="tool_start"||e.type==="tool_progress");
    const primaryPill = activeThinking || activeTool || latest;
    return <div style={{padding:"4px 0"}}>
      {Panels}
      <CodeAgentNoteCards/>
      <KbSourceCards/>
      {primaryPill&&<div style={{display:"flex",flexDirection:"column",gap:3}}>
        <Pill ev={primaryPill} t={t} expanded={expandedPill===getPillKey(primaryPill)} onToggle={()=>setExpandedPill(expandedPill===getPillKey(primaryPill)?null:getPillKey(primaryPill))}/>
        {primaryPill!==latest&&latest&&<Pill ev={latest} t={t} expanded={expandedPill===getPillKey(latest)} onToggle={()=>setExpandedPill(expandedPill===getPillKey(latest)?null:getPillKey(latest))}/>}
        {totalSteps>1&&<button onClick={()=>setShowAll(true)} style={{background:`${t.surface}60`,border:`1px solid ${t.brd}22`,color:t.mut,padding:"2px 8px",borderRadius:10,fontSize:10,cursor:"pointer",fontFamily:"inherit",whiteSpace:"nowrap",alignSelf:"flex-start",marginTop:1}}>
          {completedCount} step{completedCount!==1?"s":""} ▾
        </button>}
      </div>}
      <CodeOutputCards/>
      <FileReadyButtons/>
      <SearchResultCards/>
      <SourceLinkCards/>
    </div>;
  }

  return <div style={{padding:"4px 0"}}>
    {Panels}
    <CodeAgentNoteCards/>
    <KbSourceCards/>
    <button onClick={()=>setShowAll(false)} style={{background:`${t.surface}60`,border:`1px solid ${t.brd}22`,color:t.mut,padding:"2px 8px",borderRadius:10,fontSize:10,cursor:"pointer",fontFamily:"inherit",marginBottom:4}}>
      Hide steps ▴
    </button>
    <div style={{display:"flex",flexDirection:"column",gap:2}}>
      {merged.map((ev,i)=><Pill key={getPillKey(ev,i)} ev={ev} t={t} expanded={expandedPill===getPillKey(ev,i)} onToggle={()=>setExpandedPill(expandedPill===getPillKey(ev,i)?null:getPillKey(ev,i))}/>)}
    </div>
    <CodeOutputCards/>
    <FileReadyButtons/>
    <SearchResultCards/>
    <SourceLinkCards/>
  </div>;
};

const Chip=({l,c,bg,onX})=><span style={{display:"inline-flex",alignItems:"center",gap:4,background:bg,color:c,padding:"3px 8px",borderRadius:20,fontSize:10,fontWeight:600}}>{l}{onX&&<span onClick={onX} style={{cursor:"pointer",opacity:.7}}>&times;</span>}</span>;

const TIPS={
  chunk_size:"Characters per text chunk when indexing KB files. Smaller = more precise retrieval, larger = more context per chunk. Default: 500",
  chunk_overlap:"Overlap between adjacent chunks in characters. Preserves context at boundaries. Default: 50",
  max_context:"Maximum total characters injected into system prompt from retrieved KB chunks. Higher = more context but uses more tokens.",
  top_k:"Number of most-similar chunks retrieved per query. Higher = broader but may include less relevant content. Default: 6",
  embed_model:"Ollama model used for vector embeddings. Must be pulled first. nomic-embed-text recommended.",
  temperature:"Controls randomness. 0 = deterministic, 2 = very creative. Default: 0.7",
  top_p:"Nucleus sampling. Lower = more focused output. Default: 0.9",
  num_ctx:"Context window in tokens. Determines how much history + prompt the model sees. Default: 4096",
  repeat_penalty:"Penalizes repeated tokens. Higher = less repetition. 1.0 = no penalty. Default: 1.1",
  research_top_k:"Number of chunks retrieved from research memory per query. Default: 4",
  research_max_chars:"Maximum characters from research memory injected into context. Default: 3000",
};
const Tip=({k,t})=>{const [show,setShow]=React.useState(false);const txt=TIPS[k];if(!txt)return null;return <span style={{position:"relative",display:"inline-flex",alignItems:"center",cursor:"help",marginLeft:3}} onMouseEnter={()=>setShow(true)} onMouseLeave={()=>setShow(false)}><span style={{fontSize:10,color:t.mut,opacity:.6}}>&#9432;</span>{show&&<div style={{position:"absolute",bottom:"calc(100% + 6px)",left:"50%",transform:"translateX(-50%)",background:t.bgDeep,border:`1px solid ${t.brd}55`,borderRadius:8,padding:"8px 12px",fontSize:10,lineHeight:1.5,color:t.dim,boxShadow:"0 4px 16px #0006",zIndex:999,minWidth:200,maxWidth:280,whiteSpace:"normal",pointerEvents:"none"}}>{txt}</div>}</span>;};

// ============================================================
// WORKSPACE DETAIL COMPONENT
// ============================================================
function MemoryProfilePanel({t,API,font,notify,onOpenConv,models,wsModel}){
  const inputS={width:"100%",background:`${t.bgDeep}E6`,border:`1px solid ${t.brd}55`,color:t.text,padding:"9px 12px",borderRadius:7,fontFamily:font,fontSize:13,outline:"none",boxSizing:"border-box"};
  const btnS=(c,bg)=>({background:bg||`${c}18`,border:`1px solid ${c}4D`,color:c,padding:"6px 12px",borderRadius:7,cursor:"pointer",fontFamily:font,fontSize:11,display:"flex",alignItems:"center",gap:5,boxShadow:"none"});
  const emptyDraft={display_name:"",legal_name:"",birthday:"",age:"",bio:"",interestsText:"",linksText:"",notes:""};
  const [profile,setProfile]=React.useState(null);
  const [draft,setDraft]=React.useState(emptyDraft);
  const [memories,setMemories]=React.useState([]);
  const [loading,setLoading]=React.useState(false);
  const [saving,setSaving]=React.useState(false);
  const [scanning,setScanning]=React.useState(false);
  const [newMemText,setNewMemText]=React.useState("");
  const [newMemType,setNewMemType]=React.useState("semantic");
  const [memEdit,setMemEdit]=React.useState(null);
  const [memEditText,setMemEditText]=React.useState("");
  const parseLinks=(text)=>String(text||"").split("\n").map(line=>{
    const parts=line.split("|").map(x=>x.trim()).filter(Boolean);
    if(!parts.length)return null;
    if(parts.length===1)return {label:"",url:parts[0]};
    return {label:parts[0],url:parts[1]||"",...(parts[2]?{note:parts.slice(2).join(" | ")}:{})};
  }).filter(Boolean);
  const linksToText=(links)=>Array.isArray(links)?links.map(l=>[l.label,l.url,l.note].filter(Boolean).join(" | ")).join("\n"):"";
  const hydrateProfile=(p)=>{
    const extra=p?.extra||{};
    setProfile(p||{});
    setDraft({
      display_name:p?.display_name||"",
      legal_name:p?.legal_name||"",
      birthday:p?.birthday||"",
      age:p?.age||"",
      bio:p?.bio||"",
      interestsText:(p?.interests||[]).join("\n"),
      linksText:linksToText(p?.links||[]),
      notes:extra.notes||"",
    });
  };
  const refresh=React.useCallback(async()=>{
    setLoading(true);
    try{
      const [pr,mr]=await Promise.all([
        fetch(`${API}/api/memory/profile`).then(r=>r.json()),
        fetch(`${API}/api/memory/memories?status=all`).then(r=>r.json()),
      ]);
      hydrateProfile(pr);
      setMemories(Array.isArray(mr.memories)?mr.memories:[]);
    }catch(e){notify&&notify({type:"error",text:"Memory failed to load",detail:e.message||String(e)});}
    setLoading(false);
  },[API]);
  React.useEffect(()=>{refresh();},[refresh]);
  const saveProfile=async()=>{
    setSaving(true);
    try{
      const body={
        display_name:draft.display_name,
        legal_name:draft.legal_name,
        birthday:draft.birthday,
        age:draft.age,
        bio:draft.bio,
        interests:draft.interestsText.split(/[\n,]+/).map(x=>x.trim()).filter(Boolean),
        links:parseLinks(draft.linksText),
        extra:{...(profile?.extra||{}),notes:draft.notes},
      };
      const r=await fetch(`${API}/api/memory/profile`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      hydrateProfile(await r.json());
      notify&&notify({type:"success",text:"Profile saved",duration:2200});
    }catch(e){notify&&notify({type:"error",text:"Profile save failed",detail:e.message||String(e)});}
    setSaving(false);
  };
  const scanMemory=async()=>{
    setScanning(true);
    try{
      const r=await fetch(`${API}/api/memory/suggest`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:wsModel||""})});
      const d=await r.json();
      await refresh();
      notify&&notify({type:d.created?"success":"info",text:d.created?`Queued ${d.created} memory suggestion${d.created===1?"":"s"}`:"No new memory suggestions",detail:d.message||"",duration:4200});
    }catch(e){notify&&notify({type:"error",text:"Memory scan failed",detail:e.message||String(e)});}
    setScanning(false);
  };
  const updateMemoryLocal=(m)=>setMemories(p=>p.map(x=>x.id===m.id?m:x));
  const acceptMemory=async(m)=>{
    try{const r=await fetch(`${API}/api/memory/memories/${m.id}/accept`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});updateMemoryLocal(await r.json());notify&&notify({type:"success",text:"Memory accepted",duration:2200});}
    catch(e){notify&&notify({type:"error",text:"Memory accept failed",detail:e.message||String(e)});}
  };
  const rejectMemory=async(m)=>{
    try{const r=await fetch(`${API}/api/memory/memories/${m.id}/reject`,{method:"POST"});updateMemoryLocal(await r.json());}
    catch(e){notify&&notify({type:"error",text:"Memory reject failed",detail:e.message||String(e)});}
  };
  const patchMemory=async(m,patch)=>{
    try{const r=await fetch(`${API}/api/memory/memories/${m.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});updateMemoryLocal(await r.json());}
    catch(e){notify&&notify({type:"error",text:"Memory update failed",detail:e.message||String(e)});}
  };
  const deleteMemory=async(m)=>{
    try{await fetch(`${API}/api/memory/memories/${m.id}`,{method:"DELETE"});setMemories(p=>p.filter(x=>x.id!==m.id));}
    catch(e){notify&&notify({type:"error",text:"Memory delete failed",detail:e.message||String(e)});}
  };
  const startMemEdit=(m)=>{setMemEdit(m.id);setMemEditText(m.content||"");};
  const saveMemEdit=async(m,accept=false)=>{
    const r=await fetch(`${API}/api/memory/memories/${m.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:memEditText})});
    let updated=await r.json();
    if(accept){
      const ar=await fetch(`${API}/api/memory/memories/${m.id}/accept`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
      updated=await ar.json();
    }
    updateMemoryLocal(updated);setMemEdit(null);setMemEditText("");
  };
  const createManualMemory=async()=>{
    try{
      const r=await fetch(`${API}/api/memory/memories`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:newMemText,type:newMemType,status:"accepted",importance:3})});
      const m=await r.json();setMemories(p=>[m,...p]);setNewMemText("");notify&&notify({type:"success",text:"Memory added",duration:2200});
    }catch(e){notify&&notify({type:"error",text:"Memory add failed",detail:e.message||String(e)});}
  };
  const byStatus=s=>memories.filter(m=>m.status===s);
  const accepted=byStatus("accepted").filter(m=>!m.valid_until);
  const suggested=byStatus("suggested");
  const hidden=memories.filter(m=>["rejected","archived"].includes(m.status)||m.valid_until);
  const typeMeta={semantic:["Facts & Preferences",t.acc],episodic:["Events & People",t.warm],procedural:["Procedures",t.pink]};
  const card=(m,mode)=>{const meta=typeMeta[m.type]||typeMeta.semantic;const editing=memEdit===m.id;return <div key={m.id} style={{padding:"10px 12px",borderRadius:8,background:`${t.surface}66`,border:`1px solid ${meta[1]}33`,display:"flex",flexDirection:"column",gap:7}}>
    <div style={{display:"flex",alignItems:"center",gap:6}}>
      <span style={{fontSize:9,fontWeight:800,color:meta[1],textTransform:"uppercase",letterSpacing:.6}}>{meta[0]}</span>
      <span style={{fontSize:9,color:t.mut}}>importance {m.importance||3}</span>
      {m.pinned?<span style={{fontSize:9,color:t.warm}}>pinned</span>:null}
      {m.confidence?<span style={{fontSize:9,color:t.mut}}>{Math.round((m.confidence||0)*100)}%</span>:null}
    </div>
    {editing?<textarea value={memEditText} onChange={e=>setMemEditText(e.target.value)} style={{...inputS,minHeight:72,lineHeight:1.5,resize:"vertical"}}/>:<div style={{fontSize:12,lineHeight:1.55,color:t.text,whiteSpace:"pre-wrap"}}>{m.content}</div>}
    {m.metadata?.reason&&<div style={{fontSize:10,color:t.mut,lineHeight:1.4}}>Why: {m.metadata.reason}</div>}
    <div style={{display:"flex",gap:5,flexWrap:"wrap",alignItems:"center"}}>
      {editing?<><button onClick={()=>saveMemEdit(m,mode==="suggested")} style={btnS(t.ok)}>{mode==="suggested"?"Save + Accept":"Save"}</button><button onClick={()=>{setMemEdit(null);setMemEditText("");}} style={btnS(t.mut)}>Cancel</button></>
      :mode==="suggested"?<><button onClick={()=>acceptMemory(m)} style={btnS(t.ok)}>Accept</button><button onClick={()=>startMemEdit(m)} style={btnS(t.acc)}>Edit</button><button onClick={()=>rejectMemory(m)} style={btnS(t.err)}>Reject</button></>
      :<><button onClick={()=>patchMemory(m,{pinned:m.pinned?0:1})} style={btnS(m.pinned?t.warm:t.mut)}>{m.pinned?"Unpin":"Pin"}</button><button onClick={()=>startMemEdit(m)} style={btnS(t.acc)}>Edit</button><button onClick={()=>patchMemory(m,{status:"archived"})} style={btnS(t.mut)}>Archive</button><button onClick={()=>deleteMemory(m)} style={btnS(t.err)}>Delete</button></>}
      {m.source_conversation_id&&<button onClick={()=>onOpenConv&&onOpenConv(m.source_conversation_id)} style={{...btnS(t.f1),marginLeft:"auto"}}>Source</button>}
    </div>
  </div>;};
  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"16px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",justifyContent:"space-between",alignItems:"center",gap:12}}>
      <div style={{display:"flex",alignItems:"center",gap:8}}><span style={{display:"flex",color:t.acc}}><IC.Brain/></span><span style={{fontSize:14,fontWeight:700,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Memory & Profile</span></div>
      <div style={{display:"flex",gap:8}}><button onClick={scanMemory} disabled={scanning} style={btnS(t.warm)}>{scanning?"Scanning":"Scan Recent"}</button><button onClick={refresh} disabled={loading} style={btnS(t.acc)}>{loading?"Loading":"Refresh"}</button></div>
    </div>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      <div style={{display:"grid",gridTemplateColumns:"minmax(280px,0.62fr) minmax(280px,1fr)",gap:14,alignItems:"start"}}>
        <div style={{background:`${t.surface}F0`,border:`1px solid ${t.brd}55`,borderRadius:8,padding:16}}>
          <div style={{fontSize:10,fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.7,marginBottom:12}}>User Profile</div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10,marginBottom:10}}>
            <div><label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Preferred name</label><input value={draft.display_name} onChange={e=>setDraft(p=>({...p,display_name:e.target.value}))} style={inputS}/></div>
            <div><label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Full name</label><input value={draft.legal_name} onChange={e=>setDraft(p=>({...p,legal_name:e.target.value}))} style={inputS}/></div>
            <div><label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Birthday</label><input value={draft.birthday} onChange={e=>setDraft(p=>({...p,birthday:e.target.value}))} style={inputS}/></div>
            <div><label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Age</label><input value={draft.age} onChange={e=>setDraft(p=>({...p,age:e.target.value}))} style={inputS}/></div>
          </div>
          <label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Bio</label>
          <textarea value={draft.bio} onChange={e=>setDraft(p=>({...p,bio:e.target.value}))} style={{...inputS,minHeight:86,lineHeight:1.5,resize:"vertical",marginBottom:10}}/>
          <label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Interests</label>
          <textarea value={draft.interestsText} onChange={e=>setDraft(p=>({...p,interestsText:e.target.value}))} style={{...inputS,minHeight:74,lineHeight:1.45,resize:"vertical",marginBottom:10}}/>
          <label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Links</label>
          <textarea value={draft.linksText} onChange={e=>setDraft(p=>({...p,linksText:e.target.value}))} placeholder="Portfolio | https://example.com" style={{...inputS,minHeight:74,lineHeight:1.45,resize:"vertical",marginBottom:10}}/>
          <label style={{fontSize:10,color:t.mut,display:"block",marginBottom:4}}>Notes</label>
          <textarea value={draft.notes} onChange={e=>setDraft(p=>({...p,notes:e.target.value}))} style={{...inputS,minHeight:82,lineHeight:1.45,resize:"vertical",marginBottom:12}}/>
          <button onClick={saveProfile} disabled={saving} style={{...btnS(t.ok),justifyContent:"center"}}>{saving?"Saving":"Save Profile"}</button>
        </div>
        <div style={{display:"flex",flexDirection:"column",gap:12}}>
          <div style={{background:`${t.surface}F0`,border:`1px solid ${t.brd}55`,borderRadius:8,padding:16}}>
            <div style={{fontSize:10,fontWeight:800,color:t.pink,textTransform:"uppercase",letterSpacing:.7,marginBottom:10}}>Add Memory</div>
            <div style={{display:"grid",gridTemplateColumns:"160px 1fr",gap:8,alignItems:"start"}}>
              <select value={newMemType} onChange={e=>setNewMemType(e.target.value)} style={inputS}><option value="semantic">Semantic</option><option value="episodic">Episodic</option><option value="procedural">Procedural</option></select>
              <textarea value={newMemText} onChange={e=>setNewMemText(e.target.value)} placeholder="Add an accepted memory manually..." style={{...inputS,minHeight:78,lineHeight:1.5,resize:"vertical"}}/>
            </div>
            <button onClick={createManualMemory} disabled={!newMemText.trim()} style={{...btnS(t.ok),opacity:newMemText.trim()?1:.45,marginTop:8}}>Add Accepted Memory</button>
          </div>
          {suggested.length>0&&<div style={{display:"flex",flexDirection:"column",gap:8}}>
            <div style={{fontSize:10,fontWeight:800,color:t.warm,textTransform:"uppercase",letterSpacing:.7}}>Suggestions ({suggested.length})</div>
            {suggested.map(m=>card(m,"suggested"))}
          </div>}
          {Object.entries(typeMeta).map(([typ,[label,color]])=>{const items=accepted.filter(m=>m.type===typ);return <div key={typ} style={{display:"flex",flexDirection:"column",gap:8}}>
            <div style={{fontSize:10,fontWeight:800,color,textTransform:"uppercase",letterSpacing:.7}}>{label} ({items.length})</div>
            {items.length?items.map(m=>card(m,"accepted")):<div style={{fontSize:11,color:t.mut,padding:"12px 0"}}>No accepted {typ} memories.</div>}
          </div>;})}
          {hidden.length>0&&<details style={{borderTop:`1px solid ${t.brd}22`,paddingTop:10}}>
            <summary style={{fontSize:10,color:t.mut,cursor:"pointer",fontWeight:800,textTransform:"uppercase",letterSpacing:.7}}>Rejected, archived, or superseded ({hidden.length})</summary>
            <div style={{display:"flex",flexDirection:"column",gap:8,marginTop:8}}>{hidden.map(m=>card(m,"hidden"))}</div>
          </details>}
        </div>
      </div>
    </div>
  </div>;
}

function WorkspaceDetail({ws,wsLoading,t,API,workspaces,setWorkspaces,setActiveWs,setWsDetail,convs,onOpenConv,onOpenReport,onRemoveReport,onPreview,models,wsModel,onPersonaCreated,notify}){
  const [tab,setTab]=React.useState("convs");
  const [editMode,setEditMode]=React.useState(false);
  const [wsName,setWsName]=React.useState("");
  const [wsDesc,setWsDesc]=React.useState("");
  const [wsInstructions,setWsInstructions]=React.useState("");
  const [addPanel,setAddPanel]=React.useState(false);
  const [addSearch,setAddSearch]=React.useState("");
  const [analyzing,setAnalyzing]=React.useState(false);
  const [analyzeMsg,setAnalyzeMsg]=React.useState(null);
  const [creatingKb,setCreatingKb]=React.useState(false);
  const [memories,setMemories]=React.useState([]);
  const [memBlocks,setMemBlocks]=React.useState([]);
  const [memLoading,setMemLoading]=React.useState(false);
  const [memEdit,setMemEdit]=React.useState(null);
  const [memEditText,setMemEditText]=React.useState("");
  const [newMemText,setNewMemText]=React.useState("");
  const [newMemType,setNewMemType]=React.useState("semantic");
  const [memScanning,setMemScanning]=React.useState(false);
  React.useEffect(()=>{if(ws){setWsName(ws.name||"");setWsDesc(ws.description||"");setWsInstructions(ws.instructions||"");};},[ws?.id]);
  const refreshMemory=React.useCallback(async()=>{
    if(!ws?.id)return;
    setMemLoading(true);
    try{
      const [mr,br]=await Promise.all([
        fetch(`${API}/api/workspaces/${ws.id}/memories?status=all`).then(r=>r.json()),
        fetch(`${API}/api/workspaces/${ws.id}/memory-blocks`).then(r=>r.json()),
      ]);
      setMemories(Array.isArray(mr.memories)?mr.memories:[]);
      setMemBlocks(Array.isArray(br.blocks)?br.blocks:[]);
    }catch(e){notify&&notify({type:"error",text:"Memory failed to load",detail:e.message||String(e)});}
    setMemLoading(false);
  },[API,ws?.id,notify]);
  React.useEffect(()=>{if(ws?.id&&tab==="memory")refreshMemory();},[ws?.id,tab,refreshMemory]);
  if(wsLoading)return<div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",color:t.mut}}><div style={{textAlign:"center"}}><div style={{fontSize:24,marginBottom:8}}>⏳</div>Loading workspace...</div></div>;
  if(!ws)return<div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",color:t.mut}}><div style={{textAlign:"center"}}><div style={{fontSize:36,marginBottom:8}}>🗂</div>Select a workspace</div></div>;
  const font="'JetBrains Mono',monospace";
  const inputS={width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}40`,color:t.text,padding:"8px 10px",borderRadius:6,fontFamily:font,fontSize:12,outline:"none",boxSizing:"border-box"};
  const btnS=(c,bg)=>({background:bg||`${c}22`,border:`1px solid ${c}44`,color:c,padding:"5px 10px",borderRadius:6,cursor:"pointer",fontFamily:font,fontSize:10,display:"flex",alignItems:"center",gap:5});
  const topics=Array.isArray(ws.topics)?ws.topics:(typeof ws.topics==="string"?JSON.parse(ws.topics||"[]"):[]);
  const convIds=(ws.conversations||[]).map(c=>c.id);
  const availableConvs=convs.filter(c=>!convIds.includes(c.id));
  const filteredAvail=availableConvs.filter(c=>(c.title||"").toLowerCase().includes(addSearch.toLowerCase()));
  const saveEdit=async()=>{
    await fetch(`${API}/api/workspaces/${ws.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:wsName,description:wsDesc})}).catch(()=>{});
    setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,name:wsName,description:wsDesc}:w));
    setWsDetail(d=>({...d,name:wsName,description:wsDesc}));
    setEditMode(false);
  };
  const analyzeTopics=async()=>{
    const analyzableItems=[...(ws.conversations||[]).filter(c=>c.title&&c.title.trim()),...(ws.reports||[]).filter(r=>(r.title||r.query||"").trim())];
    if(!analyzableItems.length){setAnalyzeMsg({ok:false,text:"No chats or reports to analyze. Add workspace content first."});setTimeout(()=>setAnalyzeMsg(null),4000);return;}
    setAnalyzing(true);setAnalyzeMsg(null);
    const useModel=wsModel||models[0]||"llama3.2:3b";
    try{
      const r=await fetch(`${API}/api/workspaces/${ws.id}/analyze`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:useModel})});
      if(!r.ok)throw new Error(`Server error ${r.status}`);
      const d=await r.json();
      const newTopics=d.topics||[];
      setWsDetail(prev=>({...prev,topics:newTopics}));
      setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,topics:newTopics}:w));
      setAnalyzeMsg(newTopics.length?{ok:true,text:`Found ${newTopics.length} topic${newTopics.length>1?"s":""}`}:{ok:false,text:"No topics detected. Try adding more varied conversations."});
    }catch(e){setAnalyzeMsg({ok:false,text:`Analysis failed: ${e.message}. Check that model "${useModel}" is installed.`});}
    setAnalyzing(false);
    setTimeout(()=>setAnalyzeMsg(null),5000);
  };
  const createAgent=async()=>{
    setCreatingKb(true);
    try{
      const r=await fetch(`${API}/api/workspaces/${ws.id}/create-kb`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:ws.name})});
      const kb=await r.json();
      const mcR=await fetch(`${API}/api/model-configs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:`${ws.name} Agent`,base_model:models[0]||"",system_prompt:`You are a specialized assistant with knowledge from: ${ws.name}`,tool_ids:[],kb_ids:[kb.id],parameters:{profile_type:"agent",description:`Workspace agent grounded in ${ws.name}.`}})});
      const mc=await mcR.json();
      onPersonaCreated(mc);
    }catch{}
    setCreatingKb(false);
  };
  const addConv=async(convId)=>{
    await fetch(`${API}/api/workspaces/${ws.id}/conversations`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conversation_id:convId})}).catch(()=>{});
    const conv=convs.find(c=>c.id===convId);
    if(conv){setWsDetail(d=>({...d,conversations:[...(d.conversations||[]),{id:conv.id,title:conv.title,model:conv.model,updated_at:conv.updated_at}]}));setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,conv_count:(w.conv_count||0)+1}:w));}
  };
  const removeConv=async(convId)=>{
    await fetch(`${API}/api/workspaces/${ws.id}/conversations/${convId}`,{method:"DELETE"}).catch(()=>{});
    setWsDetail(d=>({...d,conversations:(d.conversations||[]).filter(c=>c.id!==convId)}));
    setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,conv_count:Math.max(0,(w.conv_count||0)-1)}:w));
  };
  const deleteWs=async()=>{
    await fetch(`${API}/api/workspaces/${ws.id}`,{method:"DELETE"}).catch(()=>{});
    setWorkspaces(p=>p.filter(w=>w.id!==ws.id));
    setActiveWs(null);setWsDetail(null);
  };
  const patchWsMemory=async(fields)=>{
    await fetch(`${API}/api/workspaces/${ws.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)}).catch(()=>{});
    setWsDetail(d=>({...d,...fields}));
    setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,...fields}:w));
  };
  const toggleMemory=()=>patchWsMemory({memory_enabled:(ws.memory_enabled==="1"||ws.memory_enabled===1||ws.memory_enabled===true)?0:1});
  const saveInstructions=async()=>{
    await patchWsMemory({instructions:wsInstructions});
    notify&&notify({type:"success",text:"Workspace instructions saved",duration:2200});
  };
  const scanMemory=async()=>{
    if(!ws?.id)return;
    setMemScanning(true);
    try{
      const r=await fetch(`${API}/api/workspaces/${ws.id}/memories/suggest`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:wsModel||""})});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      await refreshMemory();
      notify&&notify({type:d.created?"success":"info",text:d.created?`Queued ${d.created} memory suggestion${d.created===1?"":"s"}`:"No new memory suggestions",detail:d.message||"",duration:4200});
    }catch(e){notify&&notify({type:"error",text:"Memory scan failed",detail:e.message||String(e)});}
    setMemScanning(false);
  };
  const saveBlocks=async(blocks=memBlocks)=>{
    try{
      const r=await fetch(`${API}/api/workspaces/${ws.id}/memory-blocks`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({blocks})});
      const d=await r.json();
      setMemBlocks(d.blocks||[]);
      notify&&notify({type:"success",text:"Memory blocks saved",duration:2200});
    }catch(e){notify&&notify({type:"error",text:"Memory block save failed",detail:e.message||String(e)});}
  };
  const updateBlock=(id,patch)=>setMemBlocks(p=>p.map(b=>b.id===id?{...b,...patch}:b));
  const updateMemoryLocal=(m)=>setMemories(p=>p.map(x=>x.id===m.id?m:x));
  const acceptMemory=async(m)=>{
    try{const r=await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}/accept`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});updateMemoryLocal(await r.json());notify&&notify({type:"success",text:"Memory accepted",duration:2200});}
    catch(e){notify&&notify({type:"error",text:"Accept failed",detail:e.message||String(e)});}
  };
  const rejectMemory=async(m)=>{
    try{const r=await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}/reject`,{method:"POST"});updateMemoryLocal(await r.json());}
    catch(e){notify&&notify({type:"error",text:"Reject failed",detail:e.message||String(e)});}
  };
  const patchMemory=async(m,patch)=>{
    try{const r=await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});updateMemoryLocal(await r.json());}
    catch(e){notify&&notify({type:"error",text:"Memory update failed",detail:e.message||String(e)});}
  };
  const deleteMemory=async(m)=>{
    try{await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}`,{method:"DELETE"});setMemories(p=>p.filter(x=>x.id!==m.id));}
    catch(e){notify&&notify({type:"error",text:"Delete failed",detail:e.message||String(e)});}
  };
  const startMemEdit=(m)=>{setMemEdit(m.id);setMemEditText(m.content||"");};
  const saveMemEdit=async(m,accept=false)=>{
    const r=await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:memEditText})});
    let updated=await r.json();
    if(accept&&updated.status!=="accepted"){
      const ar=await fetch(`${API}/api/workspaces/${ws.id}/memories/${m.id}/accept`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
      updated=await ar.json();
    }
    updateMemoryLocal(updated);setMemEdit(null);setMemEditText("");
  };
  const createManualMemory=async()=>{
    const content=newMemText.trim();if(!content)return;
    try{
      const r=await fetch(`${API}/api/workspaces/${ws.id}/memories`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content,type:newMemType,status:"accepted",importance:3})});
      const m=await r.json();setMemories(p=>[m,...p]);setNewMemText("");notify&&notify({type:"success",text:"Memory added",duration:2200});
    }catch(e){notify&&notify({type:"error",text:"Memory add failed",detail:e.message||String(e)});}
  };
  const fileIcon=(filename)=>{
    const ext=(filename||"").split(".").pop().toLowerCase();
    if(["py","js","ts","rs","go","java","c","cpp"].includes(ext))return "💻";
    if(["md","txt","pdf","docx"].includes(ext))return "📄";
    if(["csv","json","xml","yaml"].includes(ext))return "📊";
    if(["tar","gz","zip"].includes(ext))return "📦";
    return "📎";
  };
  const patchArtifact=async(id,patch)=>{
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(id)}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const d=await r.json();
      setWsDetail(prev=>prev?{...prev,files:(prev.files||[]).map(f=>f.id===id?d:f)}:prev);
      notify&&notify({type:"success",text:"Artifact updated",duration:1800});
    }catch(e){notify&&notify({type:"error",text:"Artifact update failed",detail:e.message||String(e)});}
  };
  const deleteArtifact=async(id)=>{
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(id)}`,{method:"DELETE"});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      setWsDetail(prev=>prev?{...prev,files:(prev.files||[]).filter(f=>f.id!==id)}:prev);
      setWorkspaces(p=>p.map(w=>w.id===ws.id?{...w,file_count:Math.max(0,(w.file_count||1)-1)}:w));
    }catch(e){notify&&notify({type:"error",text:"Artifact delete failed",detail:e.message||String(e)});}
  };
  return<div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"14px 20px",borderBottom:`1px solid ${t.brd}22`,background:`${t.surface}44`,flexShrink:0}}>
      <div style={{display:"flex",alignItems:"flex-start",gap:10,marginBottom:8}}>
        <span style={{fontSize:24,lineHeight:1}}>🗂</span>
        <div style={{flex:1,minWidth:0}}>
          {editMode?<input value={wsName} onChange={e=>setWsName(e.target.value)} style={{...inputS,fontSize:16,fontWeight:700,background:"transparent",border:`1px solid ${t.acc}44`,marginBottom:6}}/>
            :<div style={{fontSize:16,fontWeight:700,color:t.text,marginBottom:4}}>{ws.name}</div>}
          {editMode?<input value={wsDesc} onChange={e=>setWsDesc(e.target.value)} placeholder="Description..." style={{...inputS,fontSize:11,color:t.mut}}/>
            :<div style={{fontSize:11,color:t.mut}}>{ws.description||"No description"}</div>}
        </div>
        <div style={{display:"flex",gap:4,flexShrink:0}}>
          {editMode
            ?<><button onClick={saveEdit} style={btnS(t.ok)}>💾 Save</button><button onClick={()=>setEditMode(false)} style={btnS(t.mut)}>Cancel</button></>
            :<><button onClick={()=>setEditMode(true)} style={btnS(t.mut)}>✏️ Edit</button>
              <button onClick={analyzeTopics} disabled={analyzing} style={btnS(t.acc)}>{analyzing?"⏳":"🔬"} Analyze</button>
              <button onClick={createAgent} disabled={creatingKb} style={btnS(t.pink)}>{creatingKb?"⏳":"✦"} Create Agent</button>
              <button onClick={deleteWs} style={btnS(t.err)}>🗑</button></>}
        </div>
      </div>
      {analyzeMsg&&<div style={{fontSize:10,color:analyzeMsg.ok?t.ok:t.err,marginTop:4,padding:"4px 8px",background:analyzeMsg.ok?`${t.ok}15`:`${t.err}15`,borderRadius:6,border:`1px solid ${analyzeMsg.ok?t.ok:t.err}33`}}>{analyzeMsg.text}</div>}
      {topics.length>0&&<div style={{display:"flex",flexWrap:"wrap",gap:4,marginTop:analyzeMsg?4:0}}>
        {topics.map((tp,i)=><span key={i} style={{fontSize:10,padding:"3px 8px",borderRadius:12,background:tp.color+"22",color:tp.color,border:`1px solid ${tp.color}44`,fontWeight:600}}>{tp.label}</span>)}
      </div>}
    </div>
    <div style={{display:"flex",gap:0,borderBottom:`1px solid ${t.brd}22`,flexShrink:0}}>
      {[["convs","💬 Conversations"],["memory","🧠 Memory"],["reports","📊 Reports"],["files","📎 Artifacts"]].map(([id,label])=><button key={id} onClick={()=>{setTab(id);setAddPanel(false);}} style={{padding:"10px 16px",background:tab===id?`${t.acc}15`:"transparent",border:"none",borderBottom:tab===id?`2px solid ${t.acc}`:"2px solid transparent",color:tab===id?t.acc:t.mut,cursor:"pointer",fontFamily:font,fontSize:11,fontWeight:tab===id?700:400}}>{label}</button>)}
      {tab==="convs"&&<button onClick={()=>setAddPanel(p=>!p)} style={{marginLeft:"auto",padding:"10px 14px",background:addPanel?`${t.warm}15`:"transparent",border:"none",borderBottom:addPanel?`2px solid ${t.warm}`:"2px solid transparent",color:addPanel?t.warm:t.mut,cursor:"pointer",fontFamily:font,fontSize:11}}>+ Add Chats</button>}
    </div>
    {addPanel&&<div style={{padding:12,borderBottom:`1px solid ${t.brd}22`,background:`${t.warm}07`,animation:"fadeIn .2s",flexShrink:0}}>
      <input value={addSearch} onChange={e=>setAddSearch(e.target.value)} placeholder="Search conversations..." style={{...inputS,marginBottom:8}}/>
      <div style={{maxHeight:180,overflowY:"auto",display:"flex",flexDirection:"column",gap:3}}>
        {filteredAvail.length===0&&<div style={{fontSize:11,color:t.mut,padding:8,textAlign:"center"}}>All conversations are already in this workspace.</div>}
        {filteredAvail.map(c=><div key={c.id} style={{display:"flex",alignItems:"center",gap:8,padding:"6px 10px",background:`${t.surface}44`,borderRadius:8,cursor:"pointer",border:`1px solid ${t.brd}22`}} onClick={()=>addConv(c.id)}>
          <span style={{fontSize:12,flex:1,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{c.title||"New Chat"}</span>
          <span style={{fontSize:10,color:t.mut}}>{c.model||""}</span>
          <span style={{color:t.ok,fontSize:10}}>+ Add</span>
        </div>)}
      </div>
    </div>}
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {tab==="convs"&&<div style={{display:"flex",flexDirection:"column",gap:8}}>
        {!(ws.conversations||[]).length&&<div style={{textAlign:"center",padding:40,color:t.mut,fontSize:12}}>No conversations yet.<br/>Click "+ Add Chats" to add some.</div>}
        {(ws.conversations||[]).map(c=><div key={c.id} style={{padding:"10px 14px",borderRadius:10,background:`${t.surface}66`,border:`1px solid ${t.brd}33`,display:"flex",alignItems:"center",gap:10,animation:"fadeIn .3s"}}>
          <span style={{fontSize:18}}>💬</span>
          <div style={{flex:1,minWidth:0}}>
            <div style={{fontSize:12,fontWeight:600,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{c.title||"New Chat"}</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2}}>{c.model||""} {c.updated_at?new Date(c.updated_at).toLocaleDateString():""}</div>
          </div>
          <button onClick={()=>onOpenConv(c.id)} style={btnS(t.acc)}>Open →</button>
          <button onClick={()=>removeConv(c.id)} style={btnS(t.err)}>✕</button>
        </div>)}
      </div>}
      {tab==="memory"&&(()=>{const enabled=ws.memory_enabled==="1"||ws.memory_enabled===1||ws.memory_enabled===true;const byStatus=s=>memories.filter(m=>m.status===s);const accepted=byStatus("accepted").filter(m=>!m.valid_until);const suggested=byStatus("suggested");const hidden=memories.filter(m=>["rejected","archived"].includes(m.status)||m.valid_until);const typeMeta={semantic:["Facts & Preferences",t.acc],episodic:["Decisions & Events",t.warm],procedural:["Procedures",t.pink]};const card=(m,mode)=>{const meta=typeMeta[m.type]||typeMeta.semantic;const editing=memEdit===m.id;return <div key={m.id} style={{padding:"10px 12px",borderRadius:10,background:`${t.surface}66`,border:`1px solid ${meta[1]}33`,display:"flex",flexDirection:"column",gap:7}}>
          <div style={{display:"flex",alignItems:"center",gap:6}}>
            <span style={{fontSize:9,fontWeight:800,color:meta[1],textTransform:"uppercase",letterSpacing:.6}}>{meta[0]}</span>
            <span style={{fontSize:9,color:t.mut}}>importance {m.importance||3}</span>
            {m.pinned? <span style={{fontSize:9,color:t.warm}}>pinned</span>:null}
            {m.confidence? <span style={{fontSize:9,color:t.mut}}>{Math.round((m.confidence||0)*100)}%</span>:null}
          </div>
          {editing?<textarea value={memEditText} onChange={e=>setMemEditText(e.target.value)} style={{...inputS,minHeight:72,lineHeight:1.5,resize:"vertical"}}/>:<div style={{fontSize:12,lineHeight:1.55,color:t.text,whiteSpace:"pre-wrap"}}>{m.content}</div>}
          {m.metadata?.reason&&<div style={{fontSize:10,color:t.mut,lineHeight:1.4}}>Why: {m.metadata.reason}</div>}
          <div style={{display:"flex",gap:5,flexWrap:"wrap",alignItems:"center"}}>
            {editing?<><button onClick={()=>saveMemEdit(m,mode==="suggested")} style={btnS(t.ok)}>{mode==="suggested"?"Save + Accept":"Save"}</button><button onClick={()=>{setMemEdit(null);setMemEditText("");}} style={btnS(t.mut)}>Cancel</button></>
            :mode==="suggested"?<><button onClick={()=>acceptMemory(m)} style={btnS(t.ok)}>Accept</button><button onClick={()=>startMemEdit(m)} style={btnS(t.acc)}>Edit</button><button onClick={()=>rejectMemory(m)} style={btnS(t.err)}>Reject</button></>
            :<><button onClick={()=>patchMemory(m,{pinned:m.pinned?0:1})} style={btnS(m.pinned?t.warm:t.mut)}>{m.pinned?"Unpin":"Pin"}</button><button onClick={()=>startMemEdit(m)} style={btnS(t.acc)}>Edit</button><button onClick={()=>patchMemory(m,{status:"archived"})} style={btnS(t.mut)}>Archive</button><button onClick={()=>deleteMemory(m)} style={btnS(t.err)}>Delete</button></>}
            {m.source_conversation_id&&<button onClick={()=>onOpenConv&&onOpenConv(m.source_conversation_id)} style={{...btnS(t.f1),marginLeft:"auto"}}>Source →</button>}
          </div>
        </div>;};return <div style={{display:"flex",flexDirection:"column",gap:14}}>
        <div style={{display:"flex",alignItems:"center",gap:10,padding:"10px 12px",borderRadius:10,background:`${enabled?t.ok:t.surface}10`,border:`1px solid ${enabled?t.ok:t.brd}33`}}>
          <button onClick={toggleMemory} style={btnS(enabled?t.ok:t.mut)}>{enabled?"On":"Off"}</button>
          <div style={{flex:1,minWidth:0}}>
            <div style={{fontSize:12,fontWeight:700,color:enabled?t.ok:t.text}}>Workspace memory {enabled?"enabled":"disabled"}</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2}}>Accepted memories and enabled blocks are injected into workspace chats. Suggestions are never injected until accepted.</div>
          </div>
          <button onClick={scanMemory} disabled={memScanning} style={btnS(t.warm)}>{memScanning?"Scanning":"Scan Recent"}</button>
          <button onClick={refreshMemory} disabled={memLoading} style={btnS(t.acc)}>{memLoading?"Loading":"Refresh"}</button>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(260px,.55fr)",gap:12,alignItems:"start"}}>
          <div style={{display:"flex",flexDirection:"column",gap:8}}>
            <div style={{fontSize:10,fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.7}}>Workspace Instructions</div>
            <textarea value={wsInstructions} onChange={e=>setWsInstructions(e.target.value)} placeholder="Stable project instructions, constraints, tone, or goals..." style={{...inputS,minHeight:92,lineHeight:1.5,resize:"vertical"}}/>
            <button onClick={saveInstructions} style={{...btnS(t.ok),alignSelf:"flex-start"}}>Save Instructions</button>
          </div>
          <div style={{display:"flex",flexDirection:"column",gap:8}}>
            <div style={{fontSize:10,fontWeight:800,color:t.pink,textTransform:"uppercase",letterSpacing:.7}}>Add Memory</div>
            <select value={newMemType} onChange={e=>setNewMemType(e.target.value)} style={inputS}><option value="semantic">Semantic</option><option value="episodic">Episodic</option><option value="procedural">Procedural</option></select>
            <textarea value={newMemText} onChange={e=>setNewMemText(e.target.value)} placeholder="Add an accepted memory manually..." style={{...inputS,minHeight:78,lineHeight:1.5,resize:"vertical"}}/>
            <button onClick={createManualMemory} disabled={!newMemText.trim()} style={{...btnS(t.ok),opacity:newMemText.trim()?1:.45,alignSelf:"flex-start"}}>Add Accepted Memory</button>
          </div>
        </div>
        {memBlocks.length>0&&<div style={{display:"flex",flexDirection:"column",gap:8}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><div style={{fontSize:10,fontWeight:800,color:t.acc,textTransform:"uppercase",letterSpacing:.7}}>Pinned Blocks</div><button onClick={()=>saveBlocks()} style={btnS(t.ok)}>Save Blocks</button></div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(240px,1fr))",gap:8}}>
            {memBlocks.map(b=><div key={b.id} style={{padding:10,borderRadius:10,background:`${t.surface}55`,border:`1px solid ${b.enabled?t.acc:t.brd}28`,display:"flex",flexDirection:"column",gap:6}}>
              <div style={{display:"flex",alignItems:"center",gap:8}}><button onClick={()=>updateBlock(b.id,{enabled:b.enabled?0:1})} style={btnS(b.enabled?t.ok:t.mut)}>{b.enabled?"On":"Off"}</button><div style={{fontSize:11,fontWeight:800,color:t.text}}>{b.label}</div></div>
              <div style={{fontSize:10,color:t.mut,lineHeight:1.35}}>{b.description}</div>
              <textarea value={b.value||""} onChange={e=>updateBlock(b.id,{value:e.target.value})} style={{...inputS,minHeight:76,lineHeight:1.45,resize:"vertical"}}/>
            </div>)}
          </div>
        </div>}
        {suggested.length>0&&<div style={{display:"flex",flexDirection:"column",gap:8}}>
          <div style={{fontSize:10,fontWeight:800,color:t.warm,textTransform:"uppercase",letterSpacing:.7}}>Suggestions ({suggested.length})</div>
          {suggested.map(m=>card(m,"suggested"))}
        </div>}
        {Object.entries(typeMeta).map(([typ,[label,color]])=>{const items=accepted.filter(m=>m.type===typ);return <div key={typ} style={{display:"flex",flexDirection:"column",gap:8}}>
          <div style={{fontSize:10,fontWeight:800,color,textTransform:"uppercase",letterSpacing:.7}}>{label} ({items.length})</div>
          {items.length?items.map(m=>card(m,"accepted")):<div style={{fontSize:11,color:t.mut,padding:"12px 0"}}>No accepted {typ} memories.</div>}
        </div>;})}
        {hidden.length>0&&<details style={{borderTop:`1px solid ${t.brd}22`,paddingTop:10}}>
          <summary style={{fontSize:10,color:t.mut,cursor:"pointer",fontWeight:800,textTransform:"uppercase",letterSpacing:.7}}>Rejected, archived, or superseded ({hidden.length})</summary>
          <div style={{display:"flex",flexDirection:"column",gap:8,marginTop:8}}>{hidden.map(m=>card(m,"hidden"))}</div>
        </details>}
      </div>;})()}
      {tab==="reports"&&<div style={{display:"flex",flexDirection:"column",gap:8}}>
        {!(ws.reports||[]).length&&<div style={{textAlign:"center",padding:40,color:t.mut,fontSize:12}}>No reports yet.<br/>Add reports from Deep Research.</div>}
        {(ws.reports||[]).map(r=>{const sm={complete:[t.ok,"Complete"],running:[t.acc,"Running"],queued:[t.warm,"Queued"],failed:[t.err,"Failed"],cancelled:[t.mut,"Cancelled"]}[String(r.status||"queued").toLowerCase()]||[t.warm,"Queued"];return <div key={r.id} style={{padding:"10px 14px",borderRadius:10,background:`${t.surface}66`,border:`1px solid ${t.brd}33`,display:"flex",alignItems:"center",gap:10,animation:"fadeIn .3s"}}>
          <span style={{fontSize:18}}>📊</span>
          <div style={{flex:1,minWidth:0}}>
            <div style={{fontSize:12,fontWeight:600,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{r.title||r.query||"Research Report"}</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{sm[1]} · {r.report_type||"report"} · {r.source_count||0} sources</div>
          </div>
          <button onClick={()=>onOpenReport&&onOpenReport(r.id)} style={btnS(t.acc)}>Open →</button>
          <button onClick={()=>onRemoveReport&&onRemoveReport(ws.id,r.id)} style={btnS(t.err)}>✕</button>
        </div>;})}
      </div>}
      {tab==="files"&&<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(220px,1fr))",gap:10}}>
        {!(ws.files||[]).length&&<div style={{width:"100%",textAlign:"center",padding:40,color:t.mut,fontSize:12}}>No artifacts yet.<br/>Artifacts are tracked automatically when the AI downloads them in workspace chats.</div>}
        {(ws.files||[]).map(f=><ArtifactCard key={f.id||`${f.filename}-${f.created_at}`} artifact={f} t={t} font={font} workspaces={workspaces} onPreview={onPreview} onOpenConv={onOpenConv} onPatch={patchArtifact} onDelete={deleteArtifact} compact/>)}
      </div>}
    </div>
  </div>;
}

// ============================================================
function SandboxSection({t,btnS,labelS,notify,addActivity,updateActivity}){
  const [sbInfo,setSbInfo]=useState(null);
  const [cleanupDays,setCleanupDays]=useState(30);
  const [cleaning,setCleaning]=useState(false);
  const [cleanResult,setCleanResult]=useState(null);
  const [cbCleaning,setCbCleaning]=useState(false);
  const [cbResult,setCbResult]=useState(null);
  const fmtBytes=(b)=>{if(!b)return"0 B";if(b<1024)return`${b} B`;if(b<1048576)return`${(b/1024).toFixed(1)} KB`;return`${(b/1048576).toFixed(1)} MB`;};
  useEffect(()=>{
    fetch(`${API}/api/settings`).then(r=>r.json()).then(d=>{setSbInfo(d);setCleanupDays(d.file_cleanup_days??30);}).catch(()=>{});
  },[]);
  const saveCleanupDays=async(v)=>{
    setCleanupDays(v);
    await fetch(`${API}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({file_cleanup_days:v})})
      .then(r=>notify&&notify({type:r.ok?"success":"error",text:r.ok?"Cleanup schedule saved":"Cleanup schedule failed",duration:2500}))
      .catch(()=>notify&&notify({type:"error",text:"Cleanup schedule failed"}));
  };
  const cleanNow=async()=>{
    setCleaning(true);setCleanResult(null);
    const activityId=addActivity&&addActivity({name:"Sandbox cleanup",kind:"cleanup",message:"Cleaning sandbox outputs...",pct:25});
    try{const r=await fetch(`${API}/api/settings/cleanup-now`,{method:"POST"});const d=await r.json();setCleanResult(d);
      if(d.error)throw new Error(d.error);
      updateActivity&&updateActivity(activityId,{status:"done",message:`Deleted ${d.deleted||0} files`,pct:100});
      notify&&notify({type:"success",text:"Sandbox cleanup complete",detail:`Deleted ${d.deleted||0} files.`});
      const r2=await fetch(`${API}/api/settings`);const d2=await r2.json();setSbInfo(d2);
    }catch(e){setCleanResult({error:e.message||"Failed"});updateActivity&&updateActivity(activityId,{status:"error",error:e.message||"Failed",message:"Cleanup failed"});notify&&notify({type:"error",text:"Sandbox cleanup failed",detail:e.message||"Failed"});}
    setCleaning(false);
  };
  const cleanCodebox=async()=>{
    setCbCleaning(true);setCbResult(null);
    const activityId=addActivity&&addActivity({name:"Codebox cleanup",kind:"cleanup",message:"Cleaning Codebox files...",pct:25});
    try{const r=await fetch(`${API}/api/settings/cleanup-codebox`,{method:"POST"});const d=await r.json();setCbResult(d);
      if(d.error)throw new Error(d.error);
      updateActivity&&updateActivity(activityId,{status:"done",message:`Deleted ${d.deleted||0} files`,pct:100});
      notify&&notify({type:"success",text:"Codebox cleanup complete",detail:`Deleted ${d.deleted||0} files.`});
    }catch(e){setCbResult({error:e.message||"Failed"});updateActivity&&updateActivity(activityId,{status:"error",error:e.message||"Failed",message:"Cleanup failed"});notify&&notify({type:"error",text:"Codebox cleanup failed",detail:e.message||"Failed"});}
    setCbCleaning(false);
  };
  const cleanAll=async()=>{
    setCleaning(true);setCbCleaning(true);setCleanResult(null);setCbResult(null);
    const activityId=addActivity&&addActivity({name:"Cleanup all",kind:"cleanup",message:"Cleaning sandbox and Codebox...",pct:20});
    try{
      const [r1,r2]=await Promise.all([
        fetch(`${API}/api/settings/cleanup-now`,{method:"POST"}).then(r=>r.json()).catch(()=>({error:"Sandbox failed"})),
        fetch(`${API}/api/settings/cleanup-codebox`,{method:"POST"}).then(r=>r.json()).catch(()=>({error:"Codebox failed"})),
      ]);
      setCleanResult(r1);setCbResult(r2);
      const hasErr=r1.error||r2.error;
      updateActivity&&updateActivity(activityId,{status:hasErr?"error":"done",message:hasErr?"Cleanup finished with errors":"Cleanup complete",error:hasErr||null,pct:100});
      notify&&notify({type:hasErr?"warning":"success",text:hasErr?"Cleanup finished with errors":"Cleanup complete",detail:hasErr||"Sandbox and Codebox cleaned."});
      const r3=await fetch(`${API}/api/settings`);const d3=await r3.json();setSbInfo(d3);
    }catch(e){setCleanResult({error:e.message||"Failed"});updateActivity&&updateActivity(activityId,{status:"error",error:e.message||"Failed",message:"Cleanup failed"});notify&&notify({type:"error",text:"Cleanup failed",detail:e.message||"Failed"});}
    setCleaning(false);setCbCleaning(false);
  };
  const INTERVALS=[
    {label:"Every week",days:7},{label:"Every 2 weeks",days:14},
    {label:"Monthly (default)",days:30},{label:"Every 3 months",days:90},
    {label:"Every 6 months",days:180},{label:"Yearly",days:365},
    {label:"Never",days:0},
  ];
  return <div style={{marginBottom:20}}>
    <label style={labelS}>Sandbox & File Cleanup</label>
    <div style={{background:`${t.surface}66`,border:`1px solid ${t.brd}22`,borderRadius:10,padding:"10px 14px",marginBottom:10,fontSize:11}}>
      <div style={{display:"flex",flexWrap:"wrap",gap:"6px 20px",marginBottom:sbInfo?.sandbox_dir?6:0}}>
        {sbInfo?.sandbox_dir&&<div style={{color:t.mut}}>
          <span style={{color:t.acc}}>📁 </span>
          <span style={{fontFamily:"monospace",fontSize:10,color:t.dim,wordBreak:"break-all"}}>{sbInfo.sandbox_dir}</span>
        </div>}
      </div>
      <div style={{display:"flex",gap:20,flexWrap:"wrap"}}>
        <div><span style={{color:t.mut}}>Files: </span><span style={{fontWeight:700,color:t.text}}>{sbInfo?.sandbox_file_count??"—"}</span></div>
        <div><span style={{color:t.mut}}>Size: </span><span style={{fontWeight:700,color:t.text}}>{sbInfo!=null?fmtBytes(sbInfo.sandbox_size_bytes):"—"}</span></div>
        <div><span style={{color:t.mut}}>Venv: </span><span style={{fontWeight:700,color:sbInfo?.sandbox_venv_ready?t.ok:t.mut}}>{sbInfo?.sandbox_venv_ready?"✓ Ready":"building..."}</span></div>
      </div>
    </div>
    <div style={{marginBottom:8}}>
      <div style={{fontSize:10,color:t.mut,marginBottom:5}}>Auto-delete tool output files after:</div>
      <div style={{display:"flex",flexWrap:"wrap",gap:5}}>
        {INTERVALS.map(iv=><button key={iv.days} onClick={()=>saveCleanupDays(iv.days)}
          style={{...btnS(cleanupDays===iv.days?t.acc:t.mut),background:cleanupDays===iv.days?`${t.acc}22`:`${t.surface}55`,fontWeight:cleanupDays===iv.days?700:400,fontSize:10}}>
          {iv.label}
        </button>)}
      </div>
      {cleanupDays===0&&<div style={{fontSize:10,color:t.warm,marginTop:4}}>⚠ Files will never be cleaned automatically. Clean manually when needed.</div>}
    </div>
    <div style={{display:"flex",alignItems:"center",gap:8,flexWrap:"wrap"}}>
      <button onClick={cleanAll} disabled={cleaning||cbCleaning} style={{...btnS(t.err),opacity:(cleaning||cbCleaning)?0.6:1}}>
        {(cleaning||cbCleaning)?<><span style={{display:"inline-block",animation:"spin .8s linear infinite"}}>⟳</span> Cleaning...</>:"🗑 Clean All"}
      </button>
      {cleanResult&&!cleanResult.error&&<span style={{fontSize:11,color:t.ok}}>
        ✓ Sandbox: {cleanResult.deleted} file{cleanResult.deleted!==1?"s":""}{cleanResult.freed_bytes>0?`, freed ${fmtBytes(cleanResult.freed_bytes)}`:""}
      </span>}
      {cbResult&&!cbResult.error&&<span style={{fontSize:11,color:t.ok}}>
        ✓ Codebox: {cbResult.deleted} item{cbResult.deleted!==1?"s":""}{cbResult.freed_bytes>0?`, freed ${fmtBytes(cbResult.freed_bytes)}`:""}
      </span>}
      {cleanResult?.skipped&&<span style={{fontSize:11,color:t.mut}}>↷ {cleanResult.skipped}</span>}
      {cleanResult?.error&&<span style={{fontSize:11,color:t.err}}>✕ {cleanResult.error}</span>}
      {cbResult?.error&&<span style={{fontSize:11,color:t.err}}>✕ Codebox: {cbResult.error}</span>}
    </div>
    <div style={{fontSize:10,color:t.mut,marginTop:6,lineHeight:1.5}}>
      Tool-generated files (code outputs, downloads) are stored in <code style={{background:`${t.surface}`,padding:"1px 5px",borderRadius:3,fontSize:9}}>{sbInfo?.sandbox_outputs_dir||"sandbox/outputs/"}</code> and cleaned automatically. User uploads and knowledge base files are never auto-deleted.
    </div>
  </div>;
}

// ============================================================
// Mermaid diagram block — renders ```mermaid fences as SVG
const _artifactEventsFromDaedalus = (evts)=>{
  const map=new Map();
  for(const e of evts||[]){
    if(e?.type!=="file_ready")continue;
    const d=e.data||{};
    const key=d.artifact_id||d.url||d.filename;
    if(key)map.set(key,d);
  }
  return [...map.values()];
};
const _runEnv = (run)=>run&&typeof run.result_envelope==="object"&&run.result_envelope?run.result_envelope:{};
const _baseRole = (role)=>String(role||"").split(".")[0];
const _basename = (path)=>String(path||"").split("/").filter(Boolean).pop()||"";

function DaedalusSummary({runIds=[],savedEvents=[],liveEvts=[],workflows=[],t,font,md,msgContent="",live=false,onOpenArtifact,onPreview}){
  const [runs,setRuns]=useState([]);
  const [loadErr,setLoadErr]=useState("");
  const [rawPill,setRawPill]=useState(null);
  const runIdsKey=runIds.join("|");
  useEffect(()=>{
    let stop=false;
    const load=async()=>{
      if(!runIds.length){setRuns([]);return;}
      try{
        const rows=await Promise.all(runIds.map(async rid=>{
          try{
            const r=await fetch(`${API}/api/runs/${encodeURIComponent(rid)}`);
            return r.ok?await r.json():null;
          }catch{return null;}
        }));
        if(!stop){setRuns(rows.filter(Boolean));setLoadErr("");}
      }catch(e){if(!stop)setLoadErr(e.message||"run load failed");}
    };
    load();
    const id=live&&runIds.length?setInterval(load,4000):null;
    return()=>{stop=true;if(id)clearInterval(id);};
  },[runIdsKey,live]);

  const events=useMemo(()=>{
    const seen=new Set(),out=[];
    for(const e of [...(savedEvents||[]),...(liveEvts||[])]){
      const key=`${e?.type||""}:${e?.timestamp||""}:${e?.data?.tool||""}:${e?.data?.run_id||e?.run_id||""}:${e?.data?.status||""}`;
      if(!seen.has(key)){seen.add(key);out.push(e);}
    }
    return out;
  },[savedEvents,liveEvts]);
  const tools=new Set(events.map(e=>e?.data?.tool).filter(Boolean));
  const files=_artifactEventsFromDaedalus(events);
  const latestArtifact=files[files.length-1]||null;
  const wf=Array.isArray(workflows)&&workflows.length?workflows[0]:null;
  const wfAccepted=(workflows||[]).some(w=>w.state==="accepted"||w.artifact_status==="delivered"||w.artifact_status==="partial_delivered");
  const activeStates=new Set(["queued","pending","running","planning","reviewing","fixing","accepting"]);
  const active=live||runs.some(r=>activeStates.has(r.status))||(workflows||[]).some(w=>activeStates.has(w.state));
  const delivered=wfAccepted||tools.has("download_project")||tools.has("download_file")||files.length>0;
  const problemRuns=runs.filter(r=>{
    const env=_runEnv(r);
    const status=String(r.status||"").toLowerCase();
    if(["failed","partial","cancelled","canceled"].includes(status))return true;
    if(["reviewer","acceptance"].includes(_baseRole(r.role))&&["issues","error","failed"].includes(String(env.status||"").toLowerCase()))return true;
    return false;
  });
  const needsAttention=!delivered&&!active&&(problemRuns.length||(workflows||[]).some(w=>["blocked","cancelled","canceled"].includes(String(w.state||"").toLowerCase())));
  const colour=active?t.acc:needsAttention?t.err:t.ok;
  const title=active?"Daedalus is working":needsAttention?"Daedalus needs attention":"Project ready";
  const latestProblem=problemRuns[problemRuns.length-1];
  const latestEnv=_runEnv(latestProblem);
  const statusLine=active
    ?"Building, reviewing, or packaging is still in progress."
    :needsAttention
      ?(latestEnv.error||latestEnv.summary||"The latest Daedalus workflow needs a follow-up.")
      :(delivered?"Accepted and packaged.":"Workflow complete.");
  const projectName=wf?.project_id
    || runs.map(r=>r.project_id||_runEnv(r).project_id||_basename(_runEnv(r).project_dir)).find(Boolean)
    || (latestArtifact?.filename||"").replace(/(\.tar\.gz|\.tgz|\.zip|\.gz)$/i,"")
    || "Project";
  const filesTouched=new Set();
  runs.forEach(r=>{
    const env=_runEnv(r);
    for(const f of [...(env.files_written||[]),...(env.files_touched||[])])if(f)filesTouched.add(f);
  });
  const roleCount=(role)=>runs.filter(r=>_baseRole(r.role)===role).length;
  const reviewCount=roleCount("reviewer");
  const fixCount=roleCount("fixer");
  const acceptanceCount=roleCount("acceptance")+(tools.has("run_acceptance_review")&&!roleCount("acceptance")?1:0);
  const phases=[
    ["Plan",tools.has("plan_project")||roleCount("architect")],
    ["Build",tools.has("generate_code")||roleCount("builder")],
    ["Review",tools.has("run_review")||reviewCount],
    [fixCount===1?"Fix":"Fixes",tools.has("run_fixer")||tools.has("run_aider_fix")||fixCount,fixCount],
    ["Acceptance",tools.has("run_acceptance_review")||acceptanceCount],
    ["Package",delivered],
  ].filter(p=>p[1]);
  const runRows=runs.map(r=>{
    const env=_runEnv(r),visual=_roleVisual(r.role);
    const status=String(r.status||"loading");
    const c=status==="succeeded"?t.ok:["failed","partial","cancelled","canceled"].includes(status)?t.err:t.acc;
    return {id:r.id,icon:visual.icon,label:visual.label,role:r.role,status,c,summary:env.summary||env.error||""};
  });

  const artifactHref=latestArtifact?(latestArtifact.artifact_id?`${API}/api/artifacts/${latestArtifact.artifact_id}/download`:`${API}${latestArtifact.url||""}`):"";
  return <div style={{margin:"0 0 12px",maxWidth:"100%"}}>
    {msgContent&&<div style={{margin:"0 0 14px",color:t.dim,lineHeight:1.68}}>
      {md?<MDWrap>{md(msgContent)}</MDWrap>:<pre style={{margin:0,whiteSpace:"pre-wrap",fontFamily:font}}>{msgContent}</pre>}
    </div>}

    <div style={{display:"flex",gap:10,alignItems:"flex-start",padding:"2px 0 12px",borderBottom:`1px solid ${t.brd}24`,maxWidth:"100%"}}>
      <div style={{width:28,height:28,borderRadius:7,background:`${colour}14`,border:`1px solid ${colour}35`,display:"flex",alignItems:"center",justifyContent:"center",color:colour,flexShrink:0,fontSize:15}}>{active?"⏳":needsAttention?"⚠":"✓"}</div>
      <div style={{minWidth:0,flex:1}}>
        <div style={{display:"flex",alignItems:"center",gap:8,flexWrap:"wrap"}}>
          <div style={{fontSize:13,fontWeight:900,color:colour,letterSpacing:.2}}>{title}</div>
          <span style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:280}}>{projectName}</span>
        </div>
        <div style={{fontSize:11,color:t.dim,lineHeight:1.45,marginTop:3,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical"}}>{statusLine}</div>
        <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:8}}>
          {phases.map(([label,done,count])=><span key={label} style={{fontSize:9,fontWeight:800,color:done?colour:t.mut,border:`1px solid ${done?colour:t.brd}35`,background:`${done?colour:t.surface}10`,borderRadius:999,padding:"2px 7px",lineHeight:1.4}}>{label}{count?` ×${count}`:""}</span>)}
          {filesTouched.size>0&&<span style={{fontSize:9,fontWeight:800,color:t.mut,border:`1px solid ${t.brd}35`,background:`${t.surface}55`,borderRadius:999,padding:"2px 7px",lineHeight:1.4}}>{filesTouched.size} file{filesTouched.size===1?"":"s"}</span>}
          {reviewCount>1&&<span style={{fontSize:9,fontWeight:800,color:t.mut,border:`1px solid ${t.brd}35`,background:`${t.surface}55`,borderRadius:999,padding:"2px 7px",lineHeight:1.4}}>{reviewCount} reviews</span>}
          {loadErr&&<span style={{fontSize:9,fontWeight:800,color:t.err,border:`1px solid ${t.err}35`,background:`${t.err}10`,borderRadius:999,padding:"2px 7px",lineHeight:1.4}}>{loadErr}</span>}
        </div>
      </div>
      {latestArtifact&&<div style={{display:"flex",gap:5,alignItems:"center",flexShrink:0,flexWrap:"wrap",justifyContent:"flex-end"}}>
        <a href={artifactHref} download={latestArtifact.filename} style={{display:"inline-flex",alignItems:"center",gap:6,padding:"6px 10px",background:`${t.ok}16`,border:`1px solid ${t.ok}40`,borderRadius:7,color:t.ok,textDecoration:"none",fontSize:11,fontWeight:900,maxWidth:260}}>
          <span>📦</span><span style={{minWidth:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{latestArtifact.filename||"Artifact"}</span>
        </a>
        {latestArtifact.artifact_id&&onOpenArtifact&&<button onClick={()=>onOpenArtifact(latestArtifact.artifact_id)} title="Open artifact details" style={{padding:"6px 8px",borderRadius:7,border:`1px solid ${t.acc}35`,background:`${t.acc}12`,color:t.acc,cursor:"pointer",fontFamily:font,fontSize:11,fontWeight:900}}>details</button>}
      </div>}
    </div>

    <div style={{marginTop:12}}>
      <div style={{fontSize:11,fontWeight:900,color:t.dim,letterSpacing:.35,marginBottom:9}}>Build Timeline</div>
      {runRows.length?<div style={{display:"flex",flexDirection:"column"}}>
        {runRows.map((r,idx)=><div key={r.id} style={{display:"grid",gridTemplateColumns:"28px minmax(0,1fr)",gap:10,position:"relative"}}>
          <div style={{position:"relative",display:"flex",justifyContent:"center"}}>
            {idx<runRows.length-1&&<span style={{position:"absolute",top:24,bottom:-2,width:1,background:`${t.brd}33`}}/>}
            <span style={{width:24,height:24,borderRadius:7,display:"flex",alignItems:"center",justifyContent:"center",fontSize:12,color:r.c,background:`${r.c}12`,border:`1px solid ${r.c}32`,zIndex:1}}>{r.icon}</span>
          </div>
          <div style={{minWidth:0,padding:"1px 0 12px",borderBottom:idx<runRows.length-1?`1px solid ${t.brd}16`:"none"}}>
            <div style={{display:"flex",alignItems:"baseline",gap:8,flexWrap:"wrap"}}>
              <span style={{fontWeight:900,color:r.c,fontSize:11}}>{r.label}</span>
              <span style={{fontSize:10,color:t.mut}}>{r.status}</span>
              <span style={{fontSize:10,color:t.mut,opacity:.72}}>{r.role}</span>
            </div>
            <div style={{fontSize:10,color:t.mut,lineHeight:1.45,marginTop:3,overflow:"hidden",display:"-webkit-box",WebkitLineClamp:2,WebkitBoxOrient:"vertical",wordBreak:"break-word"}}>{r.summary||"No summary recorded for this run."}</div>
          </div>
        </div>)}
      </div>:<div style={{fontSize:11,color:t.mut,padding:"2px 0 4px"}}>Run details will appear here when Daedalus starts writing run records.</div>}
    </div>

    <div style={{marginTop:8,opacity:.86}}>
      <Collapsible summary="Raw Events" theme={t} font={font}>
        <ToolStatus evts={events.filter(e=>(e.data?.tool||"")!=="processing")} savedEvts={[]} msgContent={msgContent} historical={true} t={t} expandedPill={rawPill} setExpandedPill={setRawPill} onPreview={onPreview} onOpenArtifact={onOpenArtifact} md={md}/>
        {(workflows||[]).slice(0,3).map(w=><WorkflowCard key={w.id} workflow={w} t={t} font={font} onOpenArtifact={onOpenArtifact}/>)}
        {runIds.map(rid=><RunCard key={rid} runId={rid} liveEvts={live?liveEvts:[]} t={t} font={font} md={md} onOpenArtifact={onOpenArtifact}/>)}
      </Collapsible>
    </div>
  </div>;
}

// KaTeX post-render wrapper — runs renderMathInElement against mounted children
// Memoized markdown render for FINALIZED messages. Every streamed token
// rebuilds the conversation object, which re-runs md() for every message in
// the list. md(content) is pure, so we skip the re-parse unless content (or
// render opts) actually changed — `md` itself is excluded from the compare
// because it's a fresh-but-pure closure each render.
const MemoMD = React.memo(
  function MemoMD({content, md, opts}){ return <MDWrap>{md(content, opts)}</MDWrap>; },
  (prev, next)=> prev.content===next.content && prev.opts===next.opts
);

// Stable per-message citation opts. MemoMD compares opts by REFERENCE — a fresh
// object each render would re-parse every message on every token. Cache keyed on
// the metadata object (or the msg itself when metadata is a serialized string).
const _citeOptsCache=new WeakMap();
function citeOptsFor(msg, liveQuickSearch=null){
  if(!msg||typeof msg!=="object")return undefined;
  const key=(msg.metadata&&typeof msg.metadata==="object")?msg.metadata:msg;
  const liveQuick=liveQuickSearch?.results?.length?liveQuickSearch:null;
  if(!liveQuick&&_citeOptsCache.has(key))return _citeOptsCache.get(key);
  let meta=msg.metadata;
  if(typeof meta==="string"){try{meta=JSON.parse(meta);}catch{meta=null;}}
  const ev=(meta?.saved_events||[]).filter(e=>e.type==="kb_sources").pop();
  const quick=liveQuick||_quickSearchPayloadFromEvents(meta?.saved_events||[]);
  const kbSources=ev?.data?.sources||[];
  const quickCitations=kbSources.length?[]:_quickSearchCitationSources(quick);
  const v=(ev?.data?.sources?.length||quick?.results?.length)?{
    ...(kbSources.length?{citations:kbSources}:quickCitations.length?{citations:quickCitations}:{}),
    ...(quick?.results?.length?{quickSearch:quick}:{}),
  }:undefined;
  if(!liveQuick)_citeOptsCache.set(key,v);
  return v;
}

// Inline KB citation chip [n] — click opens a small source popover.
function CitationChip({n, src, theme, font}){
  const [open,setOpen]=useState(false);
  const ref=useRef(null);
  useEffect(()=>{
    if(!open)return;
    const close=e=>{if(ref.current&&!ref.current.contains(e.target))setOpen(false);};
    document.addEventListener("mousedown",close);
    return ()=>document.removeEventListener("mousedown",close);
  },[open]);
  if(!src)return <sup style={{fontSize:"0.75em",margin:"0 1px",color:theme.mut}}>[{n}]</sup>;
  const isQuick=src.kind==="quick_search";
  const sourceTitle=isQuick?(src.title||src.domain||src.url||"Source"):(src.filename||"Source");
  const sourceMeta=isQuick
    ? [src.domain,src.engine||src.type,src.published_date||src.freshness,typeof src.score==="number"?Math.round(src.score):null].filter(v=>v!==undefined&&v!==null&&v!=="").join(" · ")
    : `chunk ${src.chunk_index}${typeof src.score==="number"?` · ${Math.round(src.score*100)}%`:""}`;
  const titleText=isQuick?[sourceTitle,src.domain||src.url].filter(Boolean).join(" - "):`${src.filename} (chunk ${src.chunk_index})`;
  return <sup ref={ref} style={{fontSize:"0.72em",lineHeight:0,margin:"0 1px",position:"relative",display:"inline-block"}}>
    <button onClick={()=>setOpen(o=>!o)} title={titleText} style={{background:`${theme.acc}18`,border:"none",color:theme.acc,padding:"0 4px",borderRadius:3,fontWeight:700,cursor:"pointer",fontFamily:font,fontSize:"inherit",lineHeight:1.5}}>{n}</button>
    {open&&<span style={{position:"absolute",bottom:"calc(100% + 6px)",left:"50%",transform:"translateX(-50%)",zIndex:30,width:300,maxWidth:"72vw",background:theme.bgDeep,border:`1px solid ${theme.brd}66`,borderRadius:8,padding:"9px 11px",boxShadow:"0 8px 24px rgba(0,0,0,.45)",textAlign:"left",fontSize:11,lineHeight:1.5,fontWeight:400,display:"block",whiteSpace:"normal"}}>
      <span style={{display:"flex",alignItems:"center",gap:6,marginBottom:4}}>
        {isQuick?<SourceFavicon url={src.url} domain={src.domain} size={16} theme={theme}/>:<span style={{flexShrink:0}}>📄</span>}
        <span style={{fontWeight:700,color:theme.acc,fontSize:11,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{sourceTitle}</span>
        <span style={{marginLeft:"auto",fontSize:9,color:theme.mut,flexShrink:0}}>{sourceMeta}</span>
      </span>
      <span style={{display:"block",color:theme.dim,maxHeight:120,overflow:"auto"}}>{src.snippet||""}{src.snippet&&src.snippet.length>=300?"…":""}</span>
      {isQuick&&src.url&&<a href={src.url} target="_blank" rel="noopener noreferrer" style={{display:"inline-flex",marginTop:7,color:theme.acc,textDecoration:"none",fontSize:10,fontWeight:700,borderBottom:`1px solid ${theme.acc}44`}}>Open source</a>}
    </span>}
  </sup>;
}

function MDWrap({children}){
  const ref=useRef(null);
  useEffect(()=>{
    if(!window.renderMathInElement||!ref.current)return;
    try{
      // Currency $-signs (e.g. "$94 ... $17.50") otherwise pair up as a math
      // span. Swap them for a placeholder before auto-render, restore after.
      const PH="";
      const SKIP={SCRIPT:1,STYLE:1,CODE:1,PRE:1,TEXTAREA:1};
      const walk=(n,fn)=>{
        if(n.nodeType===3){fn(n);return;}
        if(n.nodeType!==1||SKIP[n.tagName])return;
        for(let c=n.firstChild;c;c=c.nextSibling)walk(c,fn);
      };
      walk(ref.current,n=>{
        const v=n.nodeValue;
        if(!v||v.indexOf("$")<0)return;
        const out=v.replace(/(?<![\\$])\$(?=\d)/g,PH);
        if(out!==v)n.nodeValue=out;
      });
      window.renderMathInElement(ref.current,{
        delimiters:[
          {left:"$$",right:"$$",display:true},
          {left:"\\[",right:"\\]",display:true},
          {left:"\\(",right:"\\)",display:false},
          {left:"$",right:"$",display:false}
        ],
        throwOnError:false,
        ignoredTags:["script","noscript","style","textarea","pre","code"]
      });
      walk(ref.current,n=>{
        const v=n.nodeValue;
        if(v&&v.indexOf(PH)>=0)n.nodeValue=v.split(PH).join("$");
      });
    }catch{}
  });
  return <div ref={ref}>{children}</div>;
}



export {
  Pill,
  ReasoningPanel,
  PlanPanel,
  RunCard,
  WorkflowCard,
  _runIdsFromEvents,
  _metaObj,
  _qsHref,
  _qsHost,
  _qsUrlKey,
  _qsFaviconUrl,
  _quickSearchPayloadFromEvents,
  _quickSearchResults,
  _quickSearchCitationSources,
  _quickSourceForUrl,
  SourceFavicon,
  QuickSourceInlineChip,
  QuickSearchSourcesPanel,
  _eventsHaveDaedalus,
  _daedalusRunIdsForMessage,
  _isDaedalusOutput,
  _activityIsTerminal,
  _activityStatusKey,
  _activityGroupKey,
  _fmtElapsed,
  _parseUtcishMs,
  _stripStatusEmoji,
  _phaseFromEvent,
  ResearchLiveStatus,
  ToolStatus,
  Chip,
  Tip,
  MemoryProfilePanel,
  WorkspaceDetail,
  SandboxSection,
  _artifactEventsFromDaedalus,
  _runEnv,
  _baseRole,
  _basename,
  DaedalusSummary,
  MemoMD,
  citeOptsFor,
  CitationChip,
  MDWrap,
};
