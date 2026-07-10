import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { API } from '../session.js';

// Artifact Canvas — full-screen CodeMirror editor overlay for text-like
// artifacts. Select text → "AI Edit" → instruction → diff preview →
// apply/reject; Save creates a revision through the existing /revise
// endpoint (never overwrites the original file). CodeMirror itself is
// lazy-loaded via canvasEditorSetup.js so it stays out of the main bundle.
export default function ArtifactCanvas({artifact,t,font,onClose,notify,confirmAction,onRevised}){
  const a=artifact||{};
  const hostRef=useRef(null);
  const edRef=useRef(null);           // {view,getDoc,getSelection,replaceRange,destroy}
  const [loadState,setLoadState]=useState("loading"); // loading | ready | error
  const [loadError,setLoadError]=useState("");
  const [dirty,setDirty]=useState(false);
  const [saving,setSaving]=useState(false);
  const [aiBusy,setAiBusy]=useState(false);
  const [instruction,setInstruction]=useState("");
  const [pendingEdit,setPendingEdit]=useState(null); // {from,to,original,replacement}
  const [selInfo,setSelInfo]=useState({from:0,to:0,text:""});

  // Load raw artifact text + mount CodeMirror
  useEffect(()=>{
    let stop=false;
    (async()=>{
      try{
        const [resp,mod]=await Promise.all([
          fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/download`),
          import("../canvasEditorSetup.js"),
        ]);
        if(!resp.ok)throw new Error(`HTTP ${resp.status} loading file`);
        const text=await resp.text();
        if(stop||!hostRef.current)return;
        edRef.current=mod.createEditor({
          parent:hostRef.current,
          doc:text,
          filename:a.filename||"",
          t,
          onDocChanged:()=>setDirty(true),
        });
        setLoadState("ready");
      }catch(e){
        if(!stop){setLoadError(e.message||String(e));setLoadState("error");}
      }
    })();
    return()=>{stop=true;edRef.current?.destroy();edRef.current=null;};
  },[a.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshSelection=()=>{
    if(!edRef.current)return;
    setSelInfo(edRef.current.getSelection());
  };

  const runAiEdit=async()=>{
    const ed=edRef.current;
    const instr=instruction.trim();
    if(!ed||!instr||aiBusy)return;
    const sel=ed.getSelection();
    const doc=ed.getDoc();
    const from=sel.from===sel.to?0:sel.from;
    const to=sel.from===sel.to?doc.length:sel.to;
    setAiBusy(true);
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/ai-edit`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({content:doc,selection_start:from,selection_end:to,instruction:instr}),
      });
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      setPendingEdit({from,to,original:doc.slice(from,to),replacement:d.replacement??""});
    }catch(e){
      notify&&notify({type:"error",text:"AI edit failed",detail:e.message||String(e)});
    }finally{
      setAiBusy(false);
    }
  };

  const applyEdit=()=>{
    if(!pendingEdit||!edRef.current)return;
    edRef.current.replaceRange(pendingEdit.from,pendingEdit.to,pendingEdit.replacement);
    setPendingEdit(null);
    setInstruction("");
    setDirty(true);
  };

  const save=async()=>{
    if(!edRef.current||saving)return;
    setSaving(true);
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/revise`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({content:edRef.current.getDoc(),filename:a.filename,title:`${a.title||a.filename} (edited)`}),
      });
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      setDirty(false);
      notify&&notify({type:"success",text:"Saved as revision",detail:d.filename||"",duration:2500});
      onRevised?.(d);
    }catch(e){
      notify&&notify({type:"error",text:"Save failed",detail:e.message||String(e)});
    }finally{
      setSaving(false);
    }
  };

  const close=async()=>{
    if(dirty){
      const ok=confirmAction?await confirmAction({title:"Discard changes",body:"You have unsaved changes. Discard them?",confirmLabel:"Discard",tone:"warning"})
        :window.confirm("Discard unsaved changes?");
      if(!ok)return;
    }
    onClose?.();
  };

  const btn=(c,solid)=>({background:solid?c:`${c}16`,border:`1px solid ${c}45`,color:solid?t.bg:c,padding:"7px 12px",borderRadius:7,cursor:"pointer",fontFamily:font,fontSize:11,fontWeight:800,display:"inline-flex",alignItems:"center",gap:6});
  const hasSelection=selInfo.from!==selInfo.to;

  return createPortal(
    <div style={{position:"fixed",inset:0,zIndex:9000,background:`${t.bgDeep}F2`,backdropFilter:"blur(4px)",display:"flex",flexDirection:"column",fontFamily:font}}>
      {/* Header */}
      <div style={{display:"flex",alignItems:"center",gap:10,padding:"10px 16px",borderBottom:`1px solid ${t.brd}30`,background:t.bgDeep}}>
        <span style={{fontSize:13,fontWeight:900,color:t.text}}>✏️ Canvas</span>
        <span style={{fontSize:11,color:t.dim,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1,minWidth:0}}>
          {a.filename}{dirty&&<span style={{color:t.warm,fontWeight:800}}> • unsaved</span>}
        </span>
        <button onClick={save} disabled={!dirty||saving||loadState!=="ready"} style={{...btn(t.ok,true),opacity:(!dirty||saving||loadState!=="ready")?0.5:1}}>{saving?"Saving...":"Save as revision"}</button>
        <button onClick={close} style={btn(t.mut)}>Close</button>
      </div>

      {/* AI edit bar */}
      <div style={{display:"flex",alignItems:"center",gap:8,padding:"8px 16px",borderBottom:`1px solid ${t.brd}22`,background:`${t.surface}66`}}>
        <span style={{fontSize:10,color:hasSelection?t.acc:t.mut,fontWeight:800,whiteSpace:"nowrap"}}>
          {hasSelection?`✨ ${selInfo.to-selInfo.from} chars selected`:"✨ whole document"}
        </span>
        <input
          value={instruction}
          onChange={e=>setInstruction(e.target.value)}
          onFocus={refreshSelection}
          onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();runAiEdit();}}}
          placeholder={hasSelection?"How should the selection change? (Enter to run)":"Select text in the editor, or describe a whole-document edit"}
          disabled={aiBusy||loadState!=="ready"}
          style={{flex:1,minWidth:0,background:t.bg,border:`1px solid ${t.brd}40`,color:t.text,padding:"7px 10px",borderRadius:7,fontFamily:font,fontSize:11,outline:"none"}}
        />
        <button onClick={runAiEdit} disabled={aiBusy||!instruction.trim()||loadState!=="ready"} style={{...btn(t.acc,true),opacity:(aiBusy||!instruction.trim())?0.55:1}}>
          {aiBusy?"Editing...":"AI Edit"}
        </button>
      </div>

      {/* Editor */}
      <div style={{flex:1,minHeight:0,display:"flex",flexDirection:"column",padding:"10px 16px 16px"}}>
        {loadState==="loading"&&<div style={{color:t.mut,fontSize:12,padding:20}}>Loading editor...</div>}
        {loadState==="error"&&<div style={{color:t.err,fontSize:12,padding:20}}>Failed to load: {loadError}</div>}
        <div
          ref={hostRef}
          onMouseUp={refreshSelection}
          onKeyUp={refreshSelection}
          style={{flex:1,minHeight:0,overflow:"hidden",border:`1px solid ${t.brd}30`,borderRadius:10,background:t.bg,display:loadState==="ready"?"block":"none"}}
        />
      </div>

      {/* Diff preview */}
      {pendingEdit&&<div style={{position:"absolute",inset:"auto 16px 16px 16px",maxHeight:"55%",display:"flex",flexDirection:"column",gap:8,background:t.bgDeep,border:`1px solid ${t.acc}55`,borderRadius:12,padding:14,boxShadow:"0 12px 40px rgba(0,0,0,.5)"}}>
        <div style={{display:"flex",alignItems:"center",gap:8}}>
          <span style={{fontSize:11,fontWeight:900,color:t.acc}}>✨ Proposed edit</span>
          <span style={{fontSize:10,color:t.mut,flex:1}}>{pendingEdit.original.length} → {pendingEdit.replacement.length} chars</span>
          <button onClick={applyEdit} style={btn(t.ok,true)}>Apply</button>
          <button onClick={()=>setPendingEdit(null)} style={btn(t.err)}>Reject</button>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,minHeight:0,overflow:"hidden",flex:1}}>
          <div style={{minWidth:0,display:"flex",flexDirection:"column",gap:4,minHeight:0}}>
            <span style={{fontSize:9,color:t.err,fontWeight:800,textTransform:"uppercase"}}>Before</span>
            <pre style={{margin:0,flex:1,overflow:"auto",whiteSpace:"pre-wrap",wordBreak:"break-word",fontSize:10.5,lineHeight:1.55,color:t.dim,background:`${t.err}0C`,border:`1px solid ${t.err}25`,borderRadius:8,padding:10,fontFamily:font}}>{pendingEdit.original||"(empty)"}</pre>
          </div>
          <div style={{minWidth:0,display:"flex",flexDirection:"column",gap:4,minHeight:0}}>
            <span style={{fontSize:9,color:t.ok,fontWeight:800,textTransform:"uppercase"}}>After</span>
            <pre style={{margin:0,flex:1,overflow:"auto",whiteSpace:"pre-wrap",wordBreak:"break-word",fontSize:10.5,lineHeight:1.55,color:t.text,background:`${t.ok}0C`,border:`1px solid ${t.ok}25`,borderRadius:8,padding:10,fontFamily:font}}>{pendingEdit.replacement||"(empty)"}</pre>
          </div>
        </div>
      </div>}
    </div>,
    document.body,
  );
}
