import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { API } from '../session.js';
import { IC } from './icons.jsx';

const artifactKindMeta=(kind,t,metadata={})=>{
  const k=String(kind||"file").toLowerCase();
  const isProject=k==="archive"&&(metadata.project_id||metadata.artifact_status);
  if(isProject)return{label:"Project",icon:IC.Cube,color:t.warm};
  const map={
    image:{label:"Image",icon:IC.Image,color:t.f1},
    html:{label:"HTML",icon:IC.Code,color:t.acc},
    markdown:{label:"Markdown",icon:IC.Paperclip,color:t.pink},
    code:{label:"Code",icon:IC.Code,color:t.acc},
    data:{label:"Data",icon:IC.Database,color:t.ok},
    archive:{label:"Archive",icon:IC.Cube,color:t.warm},
    pdf:{label:"PDF",icon:IC.Paperclip,color:t.err},
    text:{label:"Text",icon:IC.Paperclip,color:t.dim},
  };
  return map[k]||{label:"File",icon:IC.Paperclip,color:t.mut};
};

function ArtifactPreviewBlock({preview,t,font}){
  const [mode,setMode]=useState("rendered");
  const [filter,setFilter]=useState("");
  const [archiveDir,setArchiveDir]=useState("");
  const [archiveQuery,setArchiveQuery]=useState("");
  const [entryPreview,setEntryPreview]=useState(null);
  const [entryLoading,setEntryLoading]=useState(false);
  useEffect(()=>{setMode("rendered");setFilter("");setArchiveDir("");setArchiveQuery("");setEntryPreview(null);},[preview?.id,preview?.updated_at,preview?.preview_type]);
  if(!preview)return <div style={{padding:18,color:t.mut,fontSize:12}}>Loading preview...</div>;
  const type=preview.preview_type||"metadata";
  if(type==="missing")return <div style={{padding:14,border:`1px solid ${t.err}35`,background:`${t.err}08`,borderRadius:8,color:t.err,fontSize:12}}>File metadata is retained, but the physical file is missing.</div>;
  if(type==="image")return <img src={`${API}${preview.download_url||preview.url}`} alt={preview.filename} style={{maxWidth:"100%",borderRadius:8,border:`1px solid ${t.brd}22`,display:"block"}}/>;
  if(type==="pdf")return <iframe src={`${API}${preview.download_url||preview.url}`} title={preview.filename} style={{width:"100%",height:520,border:`1px solid ${t.brd}22`,borderRadius:8,background:"#fff"}}/>;
  if(type==="archive"){
    const raw=(preview.preview?.entries||[]).filter(e=>e.name!=="./"&&e.name!=="."&&e.name!=="/");
    const norm=raw.map(e=>{const path=(e.path||String(e.name||"").replace(/\/$/,"")).replace(/^\.\/+/,"");const parent=e.parent??(path.includes("/")?path.split("/").slice(0,-1).join("/"):"");const base=e.display_name||String(e.name||path).replace(/\/$/,"").split("/").pop()+(e.is_dir?"/":"");return{...e,path,parent,display_name:base};});
    const q=archiveQuery.trim().toLowerCase();
    const shown=norm.filter(e=>q?(e.path||e.name||"").toLowerCase().includes(q):(e.parent||"")===archiveDir);
    const crumbs=archiveDir?archiveDir.split("/").filter(Boolean):[];
    const openEntry=async(entry)=>{
      if(entry.is_dir){setArchiveDir(entry.path||"");setEntryPreview(null);return;}
      if(!entry.previewable){setEntryPreview({error:entry.preview_blocked_reason||"Preview unavailable",entry});return;}
      setEntryLoading(true);setEntryPreview(null);
      try{
        const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(preview.id)}/archive-entry?path=${encodeURIComponent(entry.path)}`);
        const d=await r.json().catch(()=>({}));
        if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
        setEntryPreview(d);
      }catch(e){setEntryPreview({error:e.message||String(e),entry});}
      setEntryLoading(false);
    };
    return <div style={{border:`1px solid ${t.brd}22`,borderRadius:8,overflow:"hidden",display:"grid",gridTemplateRows:"auto auto minmax(240px,1fr)",maxHeight:680}}>
      <div style={{padding:"8px 10px",background:`${t.surface}66`,display:"flex",justifyContent:"space-between",gap:8,fontSize:11,color:t.mut}}>
        <span>{preview.preview?.file_count||0} files · {preview.preview?.folder_count||0} folders</span>
        <span>{preview.preview?.archive_type||"archive"}</span>
      </div>
      <div style={{padding:8,borderTop:`1px solid ${t.brd}12`,display:"flex",gap:6,alignItems:"center",flexWrap:"wrap"}}>
        <button onClick={()=>{setArchiveDir("");setEntryPreview(null);}} style={{background:archiveDir?`${t.surface}66`:`${t.acc}14`,border:`1px solid ${archiveDir?t.brd:t.acc}35`,color:archiveDir?t.mut:t.acc,borderRadius:6,padding:"4px 7px",fontSize:10,fontFamily:font,cursor:"pointer"}}>/</button>
        {crumbs.map((part,i)=>{const next=crumbs.slice(0,i+1).join("/");return <button key={next} onClick={()=>{setArchiveDir(next);setEntryPreview(null);}} style={{background:next===archiveDir?`${t.acc}14`:`${t.surface}66`,border:`1px solid ${next===archiveDir?t.acc:t.brd}35`,color:next===archiveDir?t.acc:t.mut,borderRadius:6,padding:"4px 7px",fontSize:10,fontFamily:font,cursor:"pointer"}}>{part}</button>;})}
        <input value={archiveQuery} onChange={e=>setArchiveQuery(e.target.value)} placeholder="Filter files" style={{marginLeft:"auto",minWidth:120,background:t.bgDeep,border:`1px solid ${t.brd}35`,color:t.text,padding:"5px 8px",borderRadius:6,fontFamily:font,fontSize:10,outline:"none"}}/>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"minmax(130px,42%) minmax(0,1fr)",minHeight:0,borderTop:`1px solid ${t.brd}12`}}>
        <div style={{overflow:"auto",borderRight:`1px solid ${t.brd}18`,minHeight:240}}>
          {!shown.length&&<div style={{padding:14,color:t.mut,fontSize:11}}>No entries.</div>}
          {shown.map((entry,i)=><button key={`${entry.path}-${i}`} onClick={()=>openEntry(entry)} style={{width:"100%",display:"grid",gridTemplateColumns:"18px 1fr auto",gap:6,alignItems:"center",padding:"7px 9px",border:"none",borderTop:i?`1px solid ${t.brd}10`:"none",background:entryPreview?.entry?.path===entry.path?`${t.acc}12`:"transparent",color:entry.is_dir?t.warm:entry.previewable?t.dim:t.mut,cursor:"pointer",fontFamily:font,fontSize:11,textAlign:"left"}}>
            <span>{entry.is_dir?"▸":entry.previewable?"•":"-"}</span>
            <span title={entry.path} style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{entry.display_name||entry.name||entry.path}</span>
            {!entry.is_dir&&<span style={{fontSize:9,color:t.mut,fontFamily:"monospace"}}>{entry.size||0}B</span>}
          </button>)}
        </div>
        <div style={{overflow:"auto",padding:10,minHeight:240}}>
          {entryLoading&&<div style={{fontSize:11,color:t.mut}}>Loading entry...</div>}
          {!entryLoading&&!entryPreview&&<div style={{fontSize:11,color:t.mut,lineHeight:1.6}}>Select a small text-like file to preview it inside the archive.</div>}
          {!entryLoading&&entryPreview?.error&&<div style={{border:`1px solid ${t.warm}30`,background:`${t.warm}08`,borderRadius:7,padding:10,color:t.warm,fontSize:11,lineHeight:1.55}}>
            <div style={{fontWeight:900,marginBottom:4}}>Preview unavailable</div>
            <div>{entryPreview.error}</div>
            {entryPreview.entry&&<div style={{marginTop:6,color:t.mut,wordBreak:"break-word"}}>{entryPreview.entry.path} · {entryPreview.entry.size||0}B</div>}
            <div style={{marginTop:6,color:t.mut}}>Download the archive to inspect this entry.</div>
          </div>}
          {!entryLoading&&entryPreview?.content!=null&&<pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:11,color:t.dim,fontFamily:font,lineHeight:1.55}}>{entryPreview.content}</pre>}
        </div>
      </div>
    </div>;
  }
  if(type==="table"){
    const cols=preview.columns||[];
    const rows=(preview.rows||[]).filter(r=>!filter||r.join(" ").toLowerCase().includes(filter.toLowerCase()));
    return <div style={{display:"flex",flexDirection:"column",gap:8}}>
      <input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Filter rows" style={{background:t.bgDeep,border:`1px solid ${t.brd}35`,color:t.text,padding:"7px 9px",borderRadius:7,fontFamily:font,fontSize:11,outline:"none"}}/>
      <div style={{overflow:"auto",border:`1px solid ${t.brd}22`,borderRadius:8,maxHeight:430}}>
        <table style={{width:"100%",borderCollapse:"collapse",fontSize:11,color:t.dim}}>
          <thead><tr>{cols.map((c,i)=><th key={i} style={{position:"sticky",top:0,background:t.surface,padding:"6px 8px",textAlign:"left",color:t.text,borderBottom:`1px solid ${t.brd}22`}}>{c||`col ${i+1}`}</th>)}</tr></thead>
          <tbody>{rows.map((r,i)=><tr key={i}>{cols.map((_,j)=><td key={j} style={{padding:"5px 8px",borderTop:`1px solid ${t.brd}10`,whiteSpace:"nowrap"}}>{r[j]||""}</td>)}</tr>)}</tbody>
        </table>
      </div>
      {preview.truncated&&<div style={{fontSize:10,color:t.warm}}>Preview truncated.</div>}
    </div>;
  }
  if(type==="json"){
    const data=preview.json;
    if(Array.isArray(data)&&data.length&&typeof data[0]==="object"&&!Array.isArray(data[0])){
      const cols=Array.from(new Set(data.slice(0,100).flatMap(o=>Object.keys(o||{})))).slice(0,20);
      const rows=data.filter(o=>!filter||JSON.stringify(o).toLowerCase().includes(filter.toLowerCase())).slice(0,100);
      return <div style={{display:"flex",flexDirection:"column",gap:8}}>
        <input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Filter JSON rows" style={{background:t.bgDeep,border:`1px solid ${t.brd}35`,color:t.text,padding:"7px 9px",borderRadius:7,fontFamily:font,fontSize:11,outline:"none"}}/>
        <div style={{overflow:"auto",border:`1px solid ${t.brd}22`,borderRadius:8,maxHeight:430}}><table style={{width:"100%",borderCollapse:"collapse",fontSize:11,color:t.dim}}><thead><tr>{cols.map(c=><th key={c} style={{position:"sticky",top:0,background:t.surface,padding:"6px 8px",textAlign:"left",color:t.text,borderBottom:`1px solid ${t.brd}22`}}>{c}</th>)}</tr></thead><tbody>{rows.map((o,i)=><tr key={i}>{cols.map(c=><td key={c} style={{padding:"5px 8px",borderTop:`1px solid ${t.brd}10`,whiteSpace:"nowrap"}}>{typeof o[c]==="object"?JSON.stringify(o[c]):String(o[c]??"")}</td>)}</tr>)}</tbody></table></div>
      </div>;
    }
    return <pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:11,color:t.dim,fontFamily:font,lineHeight:1.55}}>{preview.parse_error?preview.content:JSON.stringify(data??preview.content,null,2)}</pre>;
  }
  if(type==="markdown")return <div style={{display:"flex",flexDirection:"column",gap:8}}>
    <div style={{display:"flex",gap:5}}>{["rendered","raw"].map(m=><button key={m} onClick={()=>setMode(m)} style={{padding:"5px 9px",borderRadius:6,border:`1px solid ${mode===m?t.acc:t.brd}35`,background:mode===m?`${t.acc}14`:"transparent",color:mode===m?t.acc:t.mut,cursor:"pointer",fontFamily:font,fontSize:10,fontWeight:800}}>{m}</button>)}</div>
    {mode==="raw"?<pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:11,color:t.dim,fontFamily:font,lineHeight:1.55}}>{preview.content}</pre>:<div className="md" style={{fontSize:12,color:t.dim,lineHeight:1.65}} dangerouslySetInnerHTML={{__html:preview.html||""}}/>}
  </div>;
  if(type==="html")return <iframe sandbox="" srcDoc={preview.content||""} title={preview.filename} style={{width:"100%",height:520,border:`1px solid ${t.brd}22`,borderRadius:8,background:"#fff"}}/>;
  return <pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:11,color:t.dim,fontFamily:font,lineHeight:1.55}}>{preview.content||preview.message||"No preview available."}</pre>;
}

function ImageStudioPanel({t,font,configured,onPreview,onUseInChat,notify,confirmAction}){
  const [prompt,setPrompt]=useState("");
  const [negPrompt,setNegPrompt]=useState("");
  const [size,setSize]=useState("1024x1024");
  const [customW,setCustomW]=useState(1024);
  const [customH,setCustomH]=useState(1024);
  const [steps,setSteps]=useState(25);
  const [cfg,setCfg]=useState(7);
  const [seed,setSeed]=useState("");
  const [count,setCount]=useState(1);
  const [sampler,setSampler]=useState("euler");
  const [scheduler,setScheduler]=useState("normal");
  const [modelSampling,setModelSampling]=useState("");
  const [checkpoints,setCheckpoints]=useState([]);
  const [checkpoint,setCheckpoint]=useState("");
  const [vaes,setVaes]=useState([]);
  const [vae,setVae]=useState("");
  const [ckptSettings,setCkptSettings]=useState({});
  const [workflows,setWorkflows]=useState([]);
  const [workflow,setWorkflow]=useState("");
  const [job,setJob]=useState(null); // {id, started, status, queuePos}
  const [gallery,setGallery]=useState([]);
  const [galleryLoading,setGalleryLoading]=useState(false);
  const [viewIdx,setViewIdx]=useState(0);     // index into gallery shown in the main viewer (0 = newest)
  const [lightbox,setLightbox]=useState(false);
  const [enhancing,setEnhancing]=useState(false);
  const [restartingComfy,setRestartingComfy]=useState(false);
  const [freeingComfy,setFreeingComfy]=useState(false);
  const [comfyMemory,setComfyMemory]=useState(null);
  const [showAdvanced,setShowAdvanced]=useState(()=>{try{return localStorage.getItem("hc-img-adv")==="1";}catch{return false;}});
  const [error,setError]=useState("");
  const pollRef=useRef(null);
  const wfUploadRef=useRef(null);
  const thumbsRef=useRef(null);
  const toggleAdvanced=()=>setShowAdvanced(p=>{const v=!p;try{localStorage.setItem("hc-img-adv",v?"1":"0");}catch{}return v;});
  const stopPoll=()=>{if(pollRef.current){clearInterval(pollRef.current);pollRef.current=null;}};
  const loadWorkflows=()=>fetch(`${API}/api/images/workflows`).then(r=>r.ok?r.json():{workflows:[]}).then(d=>setWorkflows(d.workflows||[])).catch(()=>{});
  const loadGallery=()=>{
    setGalleryLoading(true);
    fetch(`${API}/api/artifacts?kind=image&source=image_studio&limit=100`)
      .then(r=>r.ok?r.json():[])
      .then(d=>{
        const rows=Array.isArray(d)?d:(d.artifacts||[]);
        setGallery(rows.filter(a=>(a.metadata||{}).source_tool==="image_studio"));
      }).catch(()=>{}).finally(()=>setGalleryLoading(false));
  };
  const loadComfyMemoryStatus=async()=>{
    if(!configured)return null;
    try{
      const r=await fetch(`${API}/api/images/memory-status`);
      const d=await r.json().catch(()=>({}));
      if(!r.ok){
        setComfyMemory({available:false,message:d.detail||d.error||`HTTP ${r.status}`});
        return null;
      }
      const next={available:true,...d};
      setComfyMemory(next);
      return next;
    }catch(e){
      setComfyMemory({available:false,message:String(e.message||e)});
      return null;
    }
  };
  const enhance=async()=>{
    if(!prompt.trim()||enhancing||job)return;
    setEnhancing(true);
    try{
      const r=await fetch(`${API}/api/images/enhance-prompt`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:prompt.trim()})});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      if(d.prompt)setPrompt(d.prompt);
      if(d.negative_prompt)setNegPrompt(d.negative_prompt);
      notify&&notify({type:"success",text:"Prompt enhanced",duration:2000});
    }catch(e){notify&&notify({type:"error",text:"Enhance failed",detail:String(e.message||e)});}
    finally{setEnhancing(false);}
  };
  const reuseParams=(meta)=>{
    if(!meta)return;
    if(meta.prompt)setPrompt(meta.prompt);
    setNegPrompt(meta.negative_prompt||"");
    if(meta.steps)setSteps(meta.steps);
    if(meta.cfg!=null)setCfg(meta.cfg);
    if(meta.sampler)setSampler(meta.sampler);
    if(meta.scheduler)setScheduler(meta.scheduler);
    setModelSampling(meta.model_sampling||"");
    setVae(meta.vae&&vaes.includes(meta.vae)?meta.vae:"");
    if(meta.checkpoint&&checkpoints.includes(meta.checkpoint))setCheckpoint(meta.checkpoint);
    if(meta.workflow&&workflows.some(w=>w.name===meta.workflow))setWorkflow(meta.workflow);else setWorkflow("");
    if(meta.width&&meta.height){
      const preset=`${meta.width}x${meta.height}`;
      if(["1024x1024","1216x832","832x1216"].includes(preset))setSize(preset);
      else{setSize("custom");setCustomW(meta.width);setCustomH(meta.height);}
    }
    if(meta.seed!=null)setSeed(String(meta.seed));
  };
  const deleteOne=async(a)=>{
    const ok=await confirmAction({
      title:"Delete image",
      body:"This removes the file and its record from HyprChat.",
      confirmLabel:"Delete Image",
      tone:"danger",
    });
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/artifacts/${a.id}`,{method:"DELETE"});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      const next=gallery.filter(x=>x.id!==a.id);
      setGallery(next);
      setViewIdx(v=>Math.min(v,Math.max(0,next.length-1)));
      if(!next.length)setLightbox(false);
    }catch(e){notify&&notify({type:"error",text:"Delete failed",detail:String(e.message||e)});}
  };
  const purgeAll=async()=>{
    const ok=await confirmAction({
      title:"Delete all generated images",
      body:"This removes every trace: Image Studio AND chat-generated images, their files and records on HyprChat, references inside chat messages (replies that included a photo are edited to remove it), ComfyUI's job history and file copies, and the server logs. This cannot be undone.",
      confirmLabel:"Delete Everything",
      tone:"danger",
    });
    if(!ok)return;
    try{
      const r=await fetch(`${API}/api/images/purge`,{method:"POST"});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setGallery([]);setViewIdx(0);setLightbox(false);
      const bits=[`${d.deleted_artifacts} image(s)`];
      if(d.scrubbed_messages)bits.push(`${d.scrubbed_messages} chat message(s) scrubbed`);
      if(d.comfyui_files_deleted!=null)bits.push(`${d.comfyui_files_deleted} ComfyUI file(s)`);
      if(d.journal_cleared)bits.push("server logs cleared");
      notify&&notify({type:"success",text:"All image traces deleted",detail:bits.join(" · ")+(d.comfyui_files_deleted==null?" — install the ComfyUI cleanup node for instant remote file removal":""),duration:5000});
    }catch(e){notify&&notify({type:"error",text:"Purge failed",detail:String(e.message||e)});}
  };
  const restartComfyUI=async()=>{
    if(job||restartingComfy||freeingComfy)return;
    setRestartingComfy(true);
    setError("");
    try{
      const r=await fetch(`${API}/api/images/restart-comfyui`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      notify&&notify({
        type:"success",
        text:"ComfyUI restart requested",
        detail:d.message||"The image service will be unavailable briefly while it restarts.",
        duration:4500,
      });
      setTimeout(()=>loadComfyMemoryStatus(),4000);
    }catch(e){
      const msg=String(e.message||e);
      setError(msg);
      notify&&notify({type:"error",text:"Restart failed",detail:msg});
    }
    finally{setRestartingComfy(false);}
  };
  const freeComfyMemory=async()=>{
    if(job||restartingComfy||freeingComfy)return;
    setFreeingComfy(true);
    setError("");
    try{
      const r=await fetch(`${API}/api/images/free-memory`,{method:"POST"});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      notify&&notify({
        type:"success",
        text:"ComfyUI memory unload requested",
        detail:d.message||(d.custom_node?"Resident models unloaded and VRAM free requested.":"Built-in unload accepted; install the HyprChat control node for CPU RAM idle unload."),
        duration:4500,
      });
      await loadComfyMemoryStatus();
    }catch(e){
      const msg=String(e.message||e);
      setError(msg);
      notify&&notify({type:"error",text:"Unload failed",detail:msg});
    }
    finally{setFreeingComfy(false);}
  };
  const applySettings=(s)=>{
    if(!s)return;
    if(s.steps)setSteps(s.steps);
    if(s.cfg!=null)setCfg(s.cfg);
    if(s.sampler)setSampler(s.sampler);
    if(s.scheduler)setScheduler(s.scheduler);
    setModelSampling(s.model_sampling||"");
  };
  const applyCheckpoint=(name,settingsMap)=>{
    setCheckpoint(name);
    applySettings((settingsMap||ckptSettings)[name]);
  };
  useEffect(()=>{
    if(!configured)return;
    fetch(`${API}/api/images/checkpoints`).then(r=>r.ok?r.json():{checkpoints:[]}).then(d=>{
      setCheckpoints(d.checkpoints||[]);
      setVaes(d.vaes||[]);
      setCkptSettings(d.settings||{});
      setCheckpoint(c=>{
        const initial=c||d.default||"";
        // Auto-configure for the initially selected model too
        if(!c&&initial)applySettings((d.settings||{})[initial]);
        return initial;
      });
    }).catch(()=>{});
    loadWorkflows();
    loadGallery();
    loadComfyMemoryStatus();
    return stopPoll;
  },[configured]);
  // Lightbox keyboard nav (mermaid-fullscreen pattern): Escape closes,
  // arrows step through the gallery with functional updates (no stale closures)
  useEffect(()=>{
    if(!lightbox)return;
    const h=(e)=>{
      if(e.key==="Escape")setLightbox(false);
      else if(e.key==="ArrowLeft")setViewIdx(i=>Math.max(0,i-1));
      else if(e.key==="ArrowRight")setViewIdx(i=>Math.min(gallery.length-1,i+1));
    };
    window.addEventListener("keydown",h);
    return ()=>window.removeEventListener("keydown",h);
  },[lightbox,gallery.length]);
  // Keep the selected thumbnail visible in the carousel strip
  useEffect(()=>{
    const el=thumbsRef.current?.querySelector(`[data-idx="${viewIdx}"]`);
    if(el&&el.scrollIntoView)el.scrollIntoView({inline:"nearest",block:"nearest",behavior:"smooth"});
  },[viewIdx,gallery.length]);
  const saveModelDefaults=async()=>{
    if(!checkpoint)return;
    try{
      const r=await fetch(`${API}/api/images/model-settings/${encodeURIComponent(checkpoint)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_sampling:modelSampling,sampler,scheduler,cfg:Number(cfg)||7,steps:Number(steps)||25})});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setCkptSettings(p=>({...p,[checkpoint]:d.settings}));
      notify&&notify({type:"success",text:`Defaults saved for ${checkpoint}`,duration:2200});
    }catch(e){notify&&notify({type:"error",text:"Save failed",detail:String(e.message||e)});}
  };
  const activeWf=workflows.find(w=>w.name===workflow)||null;
  const applyWorkflow=(name)=>{
    setWorkflow(name);
    const meta=workflows.find(w=>w.name===name);
    if(!meta)return;
    // Prefill the controls from the workflow's own base settings
    if(meta.steps)setSteps(meta.steps);
    if(meta.cfg!=null)setCfg(meta.cfg);
    if(meta.sampler)setSampler(meta.sampler);
    if(meta.scheduler)setScheduler(meta.scheduler);
    if(meta.checkpoint&&checkpoints.includes(meta.checkpoint))setCheckpoint(meta.checkpoint);
    if(meta.width&&meta.height){
      const preset=`${meta.width}x${meta.height}`;
      if(["1024x1024","1216x832","832x1216"].includes(preset))setSize(preset);
      else{setSize("custom");setCustomW(meta.width);setCustomH(meta.height);}
    }
    setModelSampling(""); // built-in sampling nodes (if any) come with the workflow itself
  };
  const uploadWorkflow=async(file)=>{
    if(!file)return;
    const isPng=/\.png$/i.test(file.name)||file.type==="image/png";
    const payload={name:file.name.replace(/\.(json|png)$/i,"")};
    if(isPng){
      // ComfyUI embeds the API workflow in PNG metadata — send the image, backend extracts it
      const buf=new Uint8Array(await file.arrayBuffer());
      let bin="";const CH=0x8000;
      for(let i=0;i<buf.length;i+=CH)bin+=String.fromCharCode.apply(null,buf.subarray(i,i+CH));
      payload.png_base64=btoa(bin);
    }else{
      try{payload.workflow=JSON.parse(await file.text());}
      catch{notify&&notify({type:"error",text:"Not valid JSON"});return;}
    }
    try{
      const r=await fetch(`${API}/api/images/workflows`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      await loadWorkflows();
      notify&&notify({type:"success",text:`Workflow "${d.name}" saved${isPng?" (extracted from image)":""}`,duration:2500});
    }catch(e){notify&&notify({type:"error",text:"Workflow rejected",detail:String(e.message||e)});}
  };
  const dims=()=>{
    if(size==="custom")return[parseInt(customW)||1024,parseInt(customH)||1024];
    const [w,h]=size.split("x").map(Number);return[w,h];
  };
  const generate=async()=>{
    if(!prompt.trim()||job)return;
    setError("");
    const [w,h]=dims();
    const body={prompt:prompt.trim(),negative_prompt:negPrompt.trim(),width:w,height:h,steps:Number(steps)||25,cfg:Number(cfg)||7,count:Number(count)||1,checkpoint,sampler,scheduler,model_sampling:modelSampling,vae,workflow};
    if(seed!=="")body.seed=parseInt(seed)||0;
    let d;
    try{
      const r=await fetch(`${API}/api/images/generate`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
    }catch(e){setError(String(e.message||e));return;}
    const jid=d.job_id;
    setJob({id:jid,started:Date.now(),status:"queued",params:d.params});
    pollRef.current=setInterval(async()=>{
      try{
        const r=await fetch(`${API}/api/images/jobs/${jid}`);
        const s=await r.json();
        if(!r.ok)throw new Error(s.detail||`HTTP ${r.status}`);
        if(s.status==="done"){
          stopPoll();
          setJob(null);
          loadGallery();
          setViewIdx(0); // newest lands in the main viewer
          notify&&notify({type:"success",text:`Image ready (seed ${d.params?.seed})`,duration:2500});
        }else if(s.status==="error"){
          stopPoll();setJob(null);setError(s.error||"Generation failed");
        }else{
          setJob(p=>p?{...p,status:s.status,queuePos:s.queue_position}:p);
        }
      }catch(e){stopPoll();setJob(null);setError(String(e.message||e));}
    },1000);
  };
  const cancelJob=async()=>{
    if(!job)return;
    stopPoll();
    try{await fetch(`${API}/api/images/jobs/${job.id}/cancel`,{method:"POST"});}catch{}
    setJob(null);
  };
  const useInChat=async(r)=>{
    if(!r.artifact_id)return;
    try{
      const resp=await fetch(`${API}/api/artifacts/${r.artifact_id}/use-in-chat`,{method:"POST"});
      const d=await resp.json();
      if(d.attachment)onUseInChat&&onUseInChat(d.attachment);
    }catch(e){notify&&notify({type:"error",text:"Use in chat failed",detail:String(e.message||e)});}
  };
  const inputS={width:"100%",background:`${t.bgDeep}E6`,border:`1px solid ${t.brd}55`,color:t.text,padding:"9px 12px",borderRadius:7,fontFamily:font,fontSize:13,outline:"none",boxSizing:"border-box"};
  const smallNum={...inputS,width:"100%",padding:"7px 8px",fontSize:12};
  const labelS={fontSize:10,color:t.mut,fontWeight:800,textTransform:"uppercase",letterSpacing:.55,display:"flex",flexDirection:"column",gap:5,minWidth:0};
  const railSectionS={padding:"0 0 14px",borderBottom:`1px solid ${t.brd}24`,display:"flex",flexDirection:"column",gap:10};
  const sectionHead=(label,Icon,extra=null)=><div style={{display:"flex",alignItems:"center",gap:7,minHeight:22}}>
    {Icon&&<span style={{display:"flex",color:t.acc}}><Icon/></span>}
    <span style={{fontSize:10,fontWeight:900,letterSpacing:.9,textTransform:"uppercase",color:t.acc}}>{label}</span>
    <div style={{flex:1}}/>
    {extra}
  </div>;
  const section=(label,Icon,children,extra=null)=><section style={railSectionS}>{sectionHead(label,Icon,extra)}{children}</section>;
  const grid2={display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(0,1fr)",gap:8,alignItems:"end"};
  const railBtn=(c,opts={})=>({minHeight:34,display:"inline-flex",alignItems:"center",justifyContent:"center",gap:6,padding:"7px 10px",borderRadius:7,background:opts.solid?c:`${c}14`,border:opts.solid?`1px solid ${c}`:`1px solid ${c}3d`,color:opts.solid?t.bgDeep:c,cursor:opts.disabled?"default":"pointer",fontFamily:font,fontWeight:800,fontSize:11,opacity:opts.disabled?0.55:1,boxSizing:"border-box"});
  if(!configured)return <div style={{flex:1,display:"flex",alignItems:"center",justifyContent:"center",flexDirection:"column",gap:10,color:t.mut}}>
    <span style={{fontSize:42,opacity:.5}}>🎨</span>
    <div style={{fontSize:14,fontWeight:700,color:t.dim}}>Image Studio</div>
    <div style={{fontSize:12,maxWidth:380,textAlign:"center"}}>ComfyUI is not configured. Set the ComfyUI URL in Settings → Connections to enable local Stable Diffusion image generation.</div>
  </div>;
  const elapsed=job?Math.round((Date.now()-job.started)/1000):0;
  const smallBtn=(c)=>({fontSize:10,padding:"6px 9px",borderRadius:7,background:`${c}14`,border:`1px solid ${c}35`,color:c,cursor:"pointer",fontFamily:font,fontWeight:800,display:"inline-flex",alignItems:"center",gap:5,textDecoration:"none"});
  const cur=gallery[viewIdx]||null;
  const curMeta=(cur&&cur.metadata)||{};
  const metaLine=(m)=>`seed ${m.seed} · ${m.steps} steps · ${m.width}×${m.height}${m.checkpoint?` · ${String(m.checkpoint).replace(/\.(safetensors|ckpt)$/i,"")}`:""}`;
  const actionChips=(a,m)=>(<div style={{display:"flex",gap:5,flexWrap:"wrap",justifyContent:"center"}}>
    <a href={`${API}/api/artifacts/${a.id}/download`} download={a.filename} style={smallBtn(t.ok)}><IC.Download/>Download</a>
    <button onClick={()=>useInChat({artifact_id:a.id})} style={smallBtn(t.acc)}><IC.Send/>Use in chat</button>
    <button onClick={()=>reuseParams(m)} title="Load this image's prompt and settings back into the form" style={smallBtn(t.acc2)}><IC.Refresh/>Reuse</button>
    {m.seed!=null&&<button onClick={()=>setSeed(String(m.seed))} title="Reuse this seed only" style={smallBtn(t.warm)}><IC.Star/>Seed</button>}
    <button onClick={()=>deleteOne(a)} title="Delete this image" style={smallBtn(t.err)}><IC.Trash/>Delete</button>
  </div>);
  const navBtn=(dir,disabled,onClick,big)=>(<button onClick={onClick} disabled={disabled} style={{width:big?46:30,height:big?46:58,flexShrink:0,borderRadius:big?23:8,border:`1px solid ${t.brd}40`,background:big?"rgba(0,0,0,.45)":`${t.surface}cc`,color:disabled?t.mut:t.text,fontSize:big?20:14,cursor:disabled?"default":"pointer",opacity:disabled?0.35:1,display:"flex",alignItems:"center",justifyContent:"center",fontFamily:font}}>{dir}</button>);
  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"16px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",alignItems:"center",gap:8}}>
      <IC.Image/><span style={{fontSize:14,fontWeight:700,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Image Studio</span>
      <span style={{fontSize:10,color:t.mut,marginLeft:8}}>Local Stable Diffusion via ComfyUI</span>
      <div style={{flex:1}}/>
      {gallery.length>0&&<span style={{fontSize:10,color:t.mut,marginRight:6}}>{gallery.length} image{gallery.length===1?"":"s"}</span>}
      {gallery.length>0&&<button onClick={purgeAll} title="Delete every trace of generated images everywhere: Image Studio + chat images, files, records, chat-message references, ComfyUI history/copies, and server logs." style={{fontSize:10,padding:"6px 11px",borderRadius:6,background:`${t.err}10`,border:`1px solid ${t.err}30`,color:t.err,cursor:"pointer",fontFamily:font,fontWeight:600}}>Delete all</button>}
    </div>
    <div style={{flex:1,display:"flex",overflow:"hidden"}}>
      {/* LEFT RAIL — prompt + all generation inputs */}
      <div style={{width:"clamp(330px,30vw,420px)",flexShrink:0,borderRight:`1px solid ${t.brd}28`,overflowY:"auto",padding:16,display:"flex",flexDirection:"column",gap:12}}>
        {section("Prompt",IC.Pencil,<>
          <textarea value={prompt} onChange={e=>setPrompt(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&(e.metaKey||e.ctrlKey)){e.preventDefault();generate();}}} placeholder="Subject, style, lighting, mood" rows={5} style={{...inputS,resize:"vertical",lineHeight:1.55,fontSize:13.5,padding:"11px 13px"}}/>
          <textarea value={negPrompt} onChange={e=>setNegPrompt(e.target.value)} placeholder="Negative prompt" rows={1} style={{...inputS,resize:"vertical",fontSize:12,minHeight:38}}/>
          <div style={grid2}>
            <button onClick={enhance} disabled={!prompt.trim()||enhancing||!!job} title="Enhance prompt" style={railBtn(t.acc2,{disabled:!prompt.trim()||enhancing||!!job})}>
              {enhancing?<div style={{width:11,height:11,border:`2px solid ${t.acc2}44`,borderTopColor:t.acc2,borderRadius:"50%",animation:"spin 1s linear infinite"}}/>:<IC.Star/>}
              {enhancing?"Enhancing":"Enhance"}
            </button>
            {!job
              ?<button onClick={generate} disabled={!prompt.trim()} style={railBtn(t.acc,{solid:true,disabled:!prompt.trim()})}><IC.Image/>Generate</button>
              :<button onClick={cancelJob} style={railBtn(t.err)}><IC.Stop/>Stop</button>}
          </div>
        </>)}
        {section("Model",IC.Layers,<>
          {checkpoints.length>0?<div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) auto auto",gap:8,alignItems:"end"}}>
            <label style={labelS}>Model{ckptSettings[checkpoint]?.user_override&&<span title="Using saved defaults" style={{color:t.acc,marginLeft:4}}>★</span>}
              <select value={checkpoint} onChange={e=>applyCheckpoint(e.target.value)} title="Selecting a model auto-applies its saved settings" style={{...inputS,padding:"8px 10px",fontSize:12}}>
                {checkpoints.map(c=><option key={c} value={c}>{c.replace(/\.(safetensors|ckpt)$/i,"")}</option>)}
              </select>
            </label>
            <button onClick={freeComfyMemory} disabled={!!job||restartingComfy||freeingComfy} title="Unload resident ComfyUI models and free VRAM" style={railBtn(t.f1,{disabled:!!job||restartingComfy||freeingComfy})}>
              {freeingComfy?<div style={{width:11,height:11,border:`2px solid ${t.f1}44`,borderTopColor:t.f1,borderRadius:"50%",animation:"spin 1s linear infinite"}}/>:<IC.Activity/>}
              {freeingComfy?"Unloading":"Unload"}
            </button>
            <button onClick={restartComfyUI} disabled={!!job||restartingComfy||freeingComfy} title="Restart ComfyUI to release system RAM held by the image service" style={railBtn(t.warm,{disabled:!!job||restartingComfy||freeingComfy})}>
              {restartingComfy?<div style={{width:11,height:11,border:`2px solid ${t.warm}44`,borderTopColor:t.warm,borderRadius:"50%",animation:"spin 1s linear infinite"}}/>:<IC.Refresh/>}
              {restartingComfy?"Restarting":"Restart"}
            </button>
          </div>:<div style={{fontSize:11,color:t.mut}}>No checkpoints reported.</div>}
        </>)}
        {section("Canvas",IC.Image,<>
          <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) 74px",gap:8,alignItems:"end"}}>
            <label style={labelS}>Aspect ratio
              <select value={size} onChange={e=>setSize(e.target.value)} style={{...inputS,padding:"8px 10px",fontSize:12}}>
                <option value="1024x1024">Square · 1024 x 1024</option>
                <option value="1216x832">Landscape · 1216 x 832</option>
                <option value="832x1216">Portrait · 832 x 1216</option>
                <option value="custom">Custom</option>
              </select>
            </label>
            <label style={labelS}>Count
              <select value={count} onChange={e=>setCount(e.target.value)} style={{...inputS,padding:"8px 10px",fontSize:12}}>{[1,2,3,4].map(n=><option key={n} value={n}>{n}</option>)}</select>
            </label>
          </div>
          {size==="custom"&&<div style={grid2}>
            <label style={labelS}>Width<input type="number" min={256} max={2048} step={8} value={customW} onChange={e=>setCustomW(e.target.value)} style={smallNum}/></label>
            <label style={labelS}>Height<input type="number" min={256} max={2048} step={8} value={customH} onChange={e=>setCustomH(e.target.value)} style={smallNum}/></label>
          </div>}
        </>)}
        {section("Status",IC.Activity,<>
          {job?<div style={{display:"flex",alignItems:"center",gap:10,padding:"9px 11px",background:`${t.acc}0d`,border:`1px solid ${t.acc}28`,borderRadius:8,fontSize:12,color:t.dim}}>
            <div style={{width:14,height:14,border:`2px solid ${t.acc}44`,borderTopColor:t.acc,borderRadius:"50%",animation:"spin 1s linear infinite"}}/>
            {job.status==="queued"&&job.queuePos>0?`Queued behind ${job.queuePos}`:elapsed<8?"Generating":elapsed<60?`Generating ${elapsed}s`:`Generating ${elapsed}s`}
          </div>:<div style={{display:"grid",gridTemplateColumns:"1fr auto",gap:8,alignItems:"center",fontSize:11,color:t.mut}}>
            <span>Ready</span><span>{gallery.length} image{gallery.length===1?"":"s"}</span>
          </div>}
          {comfyMemory&&<div style={{padding:"8px 10px",background:`${comfyMemory.available?t.surface:t.warm}22`,border:`1px solid ${comfyMemory.available?t.brd:t.warm}33`,borderRadius:8,fontSize:10,color:comfyMemory.available?t.mut:t.warm,lineHeight:1.45}}>
            {comfyMemory.available
              ?`Queue ${comfyMemory.running||0}/${comfyMemory.pending||0} · idle ${comfyMemory.idle_seconds||0}s · loaded ${comfyMemory.loaded_models??"?"}`
              :`Control node unavailable: ${comfyMemory.message}`}
          </div>}
          {error&&<div style={{padding:"9px 11px",background:`${t.err}10`,border:`1px solid ${t.err}33`,borderRadius:8,fontSize:12,color:t.err}}>{error}</div>}
        </>)}
        <section style={railSectionS}>
          <button onClick={toggleAdvanced} style={{display:"flex",alignItems:"center",gap:7,width:"100%",padding:0,background:"none",border:"none",color:t.acc,cursor:"pointer",fontFamily:font}}>
            <IC.Settings/><span style={{fontSize:10,fontWeight:900,letterSpacing:.9,textTransform:"uppercase"}}>Advanced Sampling</span><div style={{flex:1}}/><span style={{display:"flex",transform:showAdvanced?"rotate(0deg)":"rotate(-90deg)",transition:"transform .15s"}}><IC.ChevDown/></span>
          </button>
          {showAdvanced&&<div style={{display:"flex",flexDirection:"column",gap:10}}>
            <div style={{display:"grid",gridTemplateColumns:"repeat(3,minmax(0,1fr))",gap:8,alignItems:"end"}}>
              <label style={labelS}>Steps<input type="number" min={1} max={60} value={steps} onChange={e=>setSteps(e.target.value)} style={smallNum}/></label>
              <label style={labelS}>CFG<input type="number" min={1} max={20} step={0.5} value={cfg} onChange={e=>setCfg(e.target.value)} style={smallNum}/></label>
              <label style={labelS}>Seed<input type="text" placeholder="random" value={seed} onChange={e=>setSeed(e.target.value.replace(/[^\d]/g,""))} style={smallNum}/></label>
            </div>
            <div style={grid2}>
              <label style={labelS}>Sampler
                <select value={sampler} onChange={e=>setSampler(e.target.value)} style={{...inputS,padding:"7px 8px",fontSize:12}}>
                  {["euler","euler_ancestral","dpmpp_2m","dpmpp_2m_sde","dpmpp_3m_sde","dpmpp_sde","heun","ddim","uni_pc","lcm"].map(s2=><option key={s2} value={s2}>{s2}</option>)}
                </select>
              </label>
              <label style={labelS}>Scheduler
                <select value={scheduler} onChange={e=>setScheduler(e.target.value)} style={{...inputS,padding:"7px 8px",fontSize:12}}>
                  {["normal","karras","sgm_uniform","exponential","simple","beta"].map(s2=><option key={s2} value={s2}>{s2}</option>)}
                </select>
              </label>
            </div>
            <div style={grid2}>
              <label title="Checkpoint training type" style={{...labelS,color:modelSampling?t.acc:t.mut}}>Model type
                <select value={modelSampling} onChange={e=>setModelSampling(e.target.value)} style={{...inputS,padding:"7px 8px",fontSize:12}}>
                  <option value="">Standard</option>
                  <option value="flow">Flow matching</option>
                  <option value="vpred">v-prediction</option>
                </select>
              </label>
              {vaes.length>0?<label title="Override the checkpoint VAE" style={{...labelS,color:vae?t.acc:t.mut}}>VAE
                <select value={vae} onChange={e=>setVae(e.target.value)} style={{...inputS,padding:"7px 8px",fontSize:12}}>
                  <option value="">Baked</option>
                  {vaes.map(v=><option key={v} value={v}>{v.replace(/\.(safetensors|pt|ckpt)$/i,"")}</option>)}
                </select>
              </label>:<span/>}
            </div>
            {checkpoint&&<button onClick={saveModelDefaults} title="Save current sampling defaults for the selected model" style={{...railBtn(t.warm),alignSelf:"flex-start"}}><IC.Star/>Save defaults</button>}
          </div>}
        </section>
        {section("Workflow",IC.Cube,<>
          <label style={labelS}>Workflow
            <select value={workflow} onChange={e=>applyWorkflow(e.target.value)} style={{...inputS,padding:"8px 10px",fontSize:12}}>
              <option value="">Default built-in graph</option>
              {workflows.map(w=><option key={w.name} value={w.name}>{w.name}{w.has_lora?" *":""}</option>)}
            </select>
          </label>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",alignItems:"center"}}>
            <button onClick={()=>wfUploadRef.current?.click()} title="Upload API JSON or workflow-bearing PNG" style={railBtn(t.acc)}><IC.Upload/>Upload</button>
            <input ref={wfUploadRef} type="file" accept=".json,.png,application/json,image/png" style={{display:"none"}} onChange={e=>{uploadWorkflow(e.target.files?.[0]);e.target.value="";}}/>
            {workflow&&<button onClick={async()=>{const ok=await confirmAction({title:"Delete workflow",body:`Delete workflow "${workflow}"?`,confirmLabel:"Delete Workflow",tone:"danger"});if(!ok)return;await fetch(`${API}/api/images/workflows/${encodeURIComponent(workflow)}`,{method:"DELETE"}).catch(()=>{});setWorkflow("");loadWorkflows();}} title="Delete this saved workflow" style={railBtn(t.err)}><IC.Trash/>Delete</button>}
            {activeWf?.model_sampling_builtin&&<span style={{fontSize:9,color:t.warm}}>{activeWf.model_sampling_builtin==="flow"?"flow-matching":activeWf.model_sampling_builtin==="flux-graph"?"Flux architecture":"v-pred"} sampling built in</span>}
          </div>
        </>)}
        </div>
      {/* RIGHT VIEWER — newest/selected image large, carousel of the rest below */}
      <div style={{flex:1,minWidth:0,display:"flex",flexDirection:"column",padding:16,gap:10,overflow:"hidden"}}>
        {gallery.length===0
          ?<div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",gap:10,color:t.mut}}>
            <span style={{fontSize:40,opacity:.4}}>🖼️</span>
            <div style={{fontSize:12}}>{galleryLoading?"Loading gallery…":"No generations yet — describe an image on the left and hit Generate."}</div>
          </div>
          :<>
            <div onClick={()=>cur&&cur.exists_status!=="missing"&&setLightbox(true)} title="Click to open the full-screen viewer" style={{flex:1,minHeight:0,position:"relative",display:"flex",alignItems:"center",justifyContent:"center",background:`${t.bgDeep}99`,border:`1px solid ${t.brd}30`,borderRadius:12,overflow:"hidden",cursor:cur&&cur.exists_status!=="missing"?"zoom-in":"default"}}>
              {cur&&(cur.exists_status==="missing"
                ?<div style={{fontSize:12,color:t.mut}}>file missing on disk</div>
                :<img src={`${API}${cur.url}`} alt={cur.filename} style={{maxWidth:"100%",maxHeight:"100%",objectFit:"contain",display:"block"}}/>)}
              {job&&<div style={{position:"absolute",top:10,left:10,display:"flex",alignItems:"center",gap:8,padding:"6px 12px",background:"rgba(0,0,0,.55)",border:`1px solid ${t.acc}40`,borderRadius:20,fontSize:11,color:t.text,backdropFilter:"blur(4px)"}}>
                <div style={{width:11,height:11,border:`2px solid ${t.acc}44`,borderTopColor:t.acc,borderRadius:"50%",animation:"spin 1s linear infinite"}}/>Generating…
              </div>}
            </div>
            {cur&&<div style={{textAlign:"center",fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}} title={curMeta.prompt}>{metaLine(curMeta)}</div>}
            {cur&&actionChips(cur,curMeta)}
            {gallery.length>1&&<div style={{display:"flex",alignItems:"center",gap:6,flexShrink:0}}>
              {navBtn("◀",viewIdx<=0,()=>setViewIdx(i=>Math.max(0,i-1)))}
              <div ref={thumbsRef} style={{display:"flex",gap:8,overflowX:"auto",flex:1,padding:"4px 2px"}}>
                {gallery.map((a,i)=><div key={a.id} data-idx={i} onClick={()=>setViewIdx(i)} title={(a.metadata||{}).prompt} style={{width:74,height:74,flexShrink:0,borderRadius:8,overflow:"hidden",cursor:"pointer",border:`2px solid ${i===viewIdx?t.acc:`${t.brd}40`}`,opacity:i===viewIdx?1:.6,transition:"opacity .15s, border-color .15s",background:t.bgDeep}}>
                  {a.exists_status==="missing"
                    ?<div style={{width:"100%",height:"100%",display:"flex",alignItems:"center",justifyContent:"center",fontSize:8,color:t.mut}}>missing</div>
                    :<img src={`${API}${a.url}`} loading="lazy" alt="" style={{width:"100%",height:"100%",objectFit:"cover",display:"block"}}/>}
                </div>)}
              </div>
              {navBtn("▶",viewIdx>=gallery.length-1,()=>setViewIdx(i=>Math.min(gallery.length-1,i+1)))}
            </div>}
          </>}
      </div>
    </div>
    {/* LIGHTBOX — front-and-center full preview with arrow navigation */}
    {lightbox&&cur&&createPortal(
      <div onClick={e=>{if(e.target===e.currentTarget)setLightbox(false);}} style={{position:"fixed",inset:0,zIndex:200,background:"rgba(0,0,0,.85)",backdropFilter:"blur(8px)",display:"flex",alignItems:"center",justifyContent:"center",gap:14,animation:"fadeIn .2s"}}>
        <button onClick={()=>setLightbox(false)} title="Close (Esc)" style={{position:"absolute",top:16,right:18,width:36,height:36,borderRadius:18,border:`1px solid ${t.brd}55`,background:"rgba(0,0,0,.5)",color:t.text,fontSize:15,cursor:"pointer",fontFamily:font}}>✕</button>
        {navBtn("◀",viewIdx<=0,()=>setViewIdx(i=>Math.max(0,i-1)),true)}
        <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:10,maxWidth:"86vw",minWidth:0}}>
          {cur.exists_status==="missing"
            ?<div style={{padding:60,fontSize:13,color:t.mut}}>file missing on disk</div>
            :<img src={`${API}${cur.url}`} alt={cur.filename} style={{maxWidth:"86vw",maxHeight:"78vh",objectFit:"contain",borderRadius:10,boxShadow:"0 8px 48px #000a"}}/>}
          <div style={{fontSize:11,color:t.dim,maxWidth:"80vw",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}} title={curMeta.prompt}>{cur.filename} · {metaLine(curMeta)} · {viewIdx+1}/{gallery.length}</div>
          {actionChips(cur,curMeta)}
        </div>
        {navBtn("▶",viewIdx>=gallery.length-1,()=>setViewIdx(i=>Math.min(gallery.length-1,i+1)),true)}
      </div>,
      document.body
    )}
  </div>;
}

function ArtifactDetailPanel({artifact,t,font,workspaces,kbs,onClose,onPatch,onDelete,onUseInChat,notify,onRefreshList,onOpenArtifact}){
  const [preview,setPreview]=useState(null);
  const [timeline,setTimeline]=useState([]);
  const [tagDraft,setTagDraft]=useState("");
  const [kbId,setKbId]=useState(kbs?.[0]?.id||"");
  const a=artifact||{};
  const btnS=(c,bg)=>({background:bg||`${c}14`,border:`1px solid ${c}35`,color:c,padding:"6px 9px",borderRadius:7,cursor:"pointer",fontFamily:font,fontSize:10,fontWeight:800,display:"inline-flex",alignItems:"center",gap:5,textDecoration:"none"});
  const gridBtn=(c)=>({...btnS(c),justifyContent:"center",padding:"7px 6px",minWidth:0,whiteSpace:"nowrap"});
  useEffect(()=>{if(!kbId&&kbs?.[0]?.id)setKbId(kbs[0].id);},[kbs,kbId]);
  useEffect(()=>{let stop=false;setPreview(null);if(!a.id)return;fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/preview`).then(r=>r.json()).then(d=>{if(!stop)setPreview(d);}).catch(e=>{if(!stop)setPreview({preview_type:"metadata",message:e.message||String(e)});});return()=>{stop=true;};},[a.id,a.updated_at,a.exists_status]);
  useEffect(()=>{let stop=false;setTimeline([]);if(!a.id)return;fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/timeline`).then(r=>r.ok?r.json():{events:[]}).then(d=>{if(!stop)setTimeline(d.events||[]);}).catch(()=>{if(!stop)setTimeline([]);});return()=>{stop=true;};},[a.id,a.updated_at,a.status,a.exists_status]);
  if(!a.id)return null;
  const meta=a.metadata||{};
  const km=artifactKindMeta(a.kind,t,meta);
  const DetailIcon=km.icon;
  const patch=async(p)=>{const d=await onPatch?.(a.id,p);return d;};
  const action=async(name,body={})=>{
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/${name}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/timeline`).then(r=>r.ok?r.json():{events:[]}).then(x=>setTimeline(x.events||[])).catch(()=>{});
      return d;
    }catch(e){notify&&notify({type:"error",text:"Artifact action failed",detail:e.message||String(e)});throw e;}
  };
  const refreshDetail=()=>{if(onOpenArtifact&&a.id)onOpenArtifact(a.id);};
  const mergeDuplicate=async(duplicateId)=>{
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(a.id)}/merge-duplicate`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({duplicate_id:duplicateId})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      notify&&notify({type:"success",text:"Duplicate metadata merged",duration:2200});
      onRefreshList?.();
      refreshDetail();
    }catch(e){notify&&notify({type:"error",text:"Duplicate merge failed",detail:e.message||String(e)});}
  };
  const archiveDuplicate=async(duplicateId)=>{await onPatch?.(duplicateId,{status:"archived"});onRefreshList?.();refreshDetail();};
  const addTag=async()=>{const tag=tagDraft.trim();if(!tag)return;await patch({tags:[...(a.tags||[]),tag]});setTagDraft("");};
  const duplicateRows=a.duplicates||[];
  const timelineRows=timeline||[];
  return <div style={{width:"clamp(320px,42vw,430px)",maxWidth:"100vw",borderLeft:`1px solid ${t.brd}28`,background:t.bgDeep,display:"flex",flexDirection:"column",overflow:"hidden",flexShrink:0}}>
    <div style={{padding:"12px 14px",borderBottom:`1px solid ${t.brd}22`,display:"flex",alignItems:"center",gap:8}}>
      <span style={{color:km.color,display:"flex"}}>{DetailIcon?<DetailIcon/>:null}</span>
      <div style={{minWidth:0,flex:1}}><div style={{fontSize:13,fontWeight:900,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{a.title||a.filename}</div><div style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{a.filename}</div></div>
      <button onClick={onClose} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:18,lineHeight:1}}>×</button>
    </div>
    <div style={{flex:1,overflow:"auto",padding:14,display:"flex",flexDirection:"column",gap:14}}>
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Preview</div><ArtifactPreviewBlock preview={preview} t={t} font={font}/></section>
      <section style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,fontSize:11,color:t.dim}}>
        <div><span style={{color:t.mut}}>Status</span><select value={a.status||"draft"} onChange={e=>patch({status:e.target.value})} style={{width:"100%",marginTop:4,background:t.bg,border:`1px solid ${t.brd}35`,color:t.text,padding:"6px 8px",borderRadius:6,fontFamily:font,fontSize:11}}>{["draft","accepted","archived"].map(s=><option key={s} value={s}>{s}</option>)}</select></div>
        <div><span style={{color:t.mut}}>Health</span><div style={{marginTop:4,color:a.exists_status==="missing"?t.err:a.exists_status==="present"?t.ok:t.warm,fontWeight:800}}>{a.exists_status||"unknown"}</div></div>
        <div><span style={{color:t.mut}}>Size</span><div style={{marginTop:4}}>{a.size_bytes?`${(a.size_bytes/1024).toFixed(a.size_bytes>1048576?1:0)} KB`:"unknown"}</div></div>
        <div><span style={{color:t.mut}}>Created</span><div style={{marginTop:4}}>{a.created_at?new Date(a.created_at).toLocaleString():""}</div></div>
      </section>
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Actions</div>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:6}}>
          <button onClick={async()=>{const d=await action("use-in-chat");onUseInChat?.(d.attachment);}} style={gridBtn(t.acc)}>Use in chat</button>
          <button onClick={async()=>{const d=await action("send-to-research");notify&&notify({type:"success",text:"Research started",detail:d.report_id||d.id||"",duration:2500});}} style={gridBtn(t.pink)}>Research</button>
          <button onClick={async()=>{const d=await action("fork-to-codebox");notify&&notify({type:"success",text:"Copied to Codebox",detail:d.path,duration:3000});}} style={gridBtn(t.warm)}>Codebox</button>
          <button onClick={async()=>{await action("revise",{title:`${a.title||a.filename} revision`});onRefreshList?.();}} style={gridBtn(t.f1)}>Revise</button>
          <button onClick={async()=>{const d=await action("check-file");await onPatch?.(a.id,{});notify&&notify({type:"success",text:"File health checked",detail:d.exists_status||"",duration:1800});}} style={gridBtn(t.mut)}>Check file</button>
        </div>
        {kbs?.length>0&&<div style={{display:"flex",gap:6,marginTop:6}}>
          <select value={kbId} onChange={e=>setKbId(e.target.value)} style={{flex:1,minWidth:0,background:t.bg,border:`1px solid ${t.brd}35`,color:t.text,padding:"6px 8px",borderRadius:6,fontFamily:font,fontSize:10,outline:"none",cursor:"pointer"}}>{kbs.map(k=><option key={k.id} value={k.id}>{k.name}</option>)}</select>
          <button onClick={async()=>{await action("add-to-kb",{kb_id:kbId});notify&&notify({type:"success",text:"Artifact imported to KB",duration:1800});}} style={{...btnS(t.ok),flexShrink:0}}>Add to KB</button>
        </div>}
      </section>
      {duplicateRows.length>0&&<section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Duplicates</div>
        <div style={{display:"flex",flexDirection:"column",gap:7}}>
          {duplicateRows.map(d=><div key={d.id} style={{border:`1px solid ${t.brd}24`,background:`${t.surface}44`,borderRadius:8,padding:8,display:"flex",flexDirection:"column",gap:6}}>
            <div style={{display:"flex",gap:8,alignItems:"center",minWidth:0}}>
              <div style={{minWidth:0,flex:1}}><div style={{fontSize:11,color:t.text,fontWeight:800,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{d.title||d.filename}</div><div style={{fontSize:9,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{d.filename}</div></div>
              <span style={{fontSize:9,color:d.status==="archived"?t.mut:t.warm,border:`1px solid ${t.brd}24`,borderRadius:6,padding:"2px 5px"}}>{d.status}</span>
            </div>
            <div style={{fontSize:10,color:t.mut,display:"flex",gap:7,flexWrap:"wrap"}}><span>{d.file_health?.size_bytes?`${(d.file_health.size_bytes/1024).toFixed(0)} KB`:"unknown size"}</span>{d.pinned? <span style={{color:t.warm}}>pinned</span>:null}{(d.workspaces||[]).slice(0,2).map(w=><span key={w.id}>{w.name}</span>)}</div>
            <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
              <button onClick={()=>onOpenArtifact?.(d.id)} style={btnS(t.f1)}>Open</button>
              <button onClick={()=>archiveDuplicate(d.id)} style={btnS(t.warm)}>Archive duplicate</button>
              <button onClick={()=>mergeDuplicate(d.id)} style={btnS(t.ok)}>Merge metadata</button>
            </div>
          </div>)}
        </div>
      </section>}
      {timelineRows.length>0&&<section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Timeline</div>
        <div style={{display:"flex",flexDirection:"column",gap:7}}>
          {timelineRows.slice(0,40).map(ev=>{const md=ev.metadata||{};const rel=md.related_artifact_id||(Array.isArray(md.related_artifact_ids)?md.related_artifact_ids[0]:null);return <div key={ev.id} style={{display:"grid",gridTemplateColumns:"82px 1fr",gap:8,fontSize:11,color:t.dim,lineHeight:1.45}}>
            <div style={{color:t.mut,fontSize:9}}>{ev.created_at?new Date(ev.created_at).toLocaleString([],{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):""}</div>
            <div style={{borderLeft:`2px solid ${ev.source==="event"?t.acc:t.brd}55`,paddingLeft:8,minWidth:0}}>
              <div style={{color:t.text,fontWeight:800,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{String(ev.event_type||"event").replace(/_/g," ")}</div>
              <div style={{wordBreak:"break-word"}}>{ev.summary||""}</div>
              {rel&&<button onClick={()=>onOpenArtifact?.(rel)} style={{marginTop:4,background:"transparent",border:"none",padding:0,color:t.acc,cursor:"pointer",fontFamily:font,fontSize:10}}>Open related artifact</button>}
            </div>
          </div>;})}
        </div>
      </section>}
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Tags</div>
        <div style={{display:"flex",gap:5,flexWrap:"wrap",marginBottom:7}}>{(a.tags||[]).map(tag=><button key={tag} onClick={()=>patch({tags:(a.tags||[]).filter(x=>x!==tag)})} title="Remove tag" style={{background:`${t.acc}12`,border:`1px solid ${t.acc}30`,color:t.acc,padding:"3px 7px",borderRadius:6,fontSize:10,cursor:"pointer",fontFamily:font}}>#{tag} ×</button>)}</div>
        <div style={{display:"flex",gap:6}}><input value={tagDraft} onChange={e=>setTagDraft(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")addTag();}} placeholder="Add tag" style={{flex:1,background:t.bg,border:`1px solid ${t.brd}35`,color:t.text,padding:"6px 8px",borderRadius:6,fontFamily:font,fontSize:11,outline:"none"}}/><button onClick={addTag} style={btnS(t.acc)}>Add</button></div>
      </section>
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Workspaces</div>
        <div style={{display:"flex",flexDirection:"column",gap:5}}>{workspaces.map(w=>{const checked=(a.workspace_ids||[]).includes(w.id);return <label key={w.id} style={{display:"flex",alignItems:"center",gap:7,fontSize:11,color:checked?t.text:t.mut}}><input type="checkbox" checked={checked} onChange={e=>{const cur=new Set(a.workspace_ids||[]);if(e.target.checked)cur.add(w.id);else cur.delete(w.id);patch({workspace_ids:Array.from(cur)});}}/> {w.name}</label>;})}</div>
      </section>
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Lineage</div>
        <div style={{display:"flex",flexDirection:"column",gap:5}}>{(a.versions||[]).map(v=><div key={v.id} style={{fontSize:11,color:v.id===a.id?t.acc:t.dim,display:"flex",justifyContent:"space-between",alignItems:"center",gap:6}}><span style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{v.title||v.filename}{v.latest_for_project&&!v.stale&&<span style={{color:t.ok,fontWeight:800,marginLeft:5}}>latest</span>}{v.stale&&<span title="Project changed after this was packaged" style={{color:t.warm,fontWeight:800,marginLeft:5}}>⚠ stale</span>}</span><span style={{display:"inline-flex",alignItems:"center",gap:6,flexShrink:0}}>{v.status}<a href={`${API}/api/artifacts/${v.id}/download`} download={v.filename} title="Download this version" style={{color:t.ok,textDecoration:"none"}}>⬇</a></span></div>)}</div>
      </section>
      <section><div style={{fontSize:10,color:t.acc,fontWeight:900,textTransform:"uppercase",marginBottom:7}}>Provenance</div>
        <div style={{fontSize:11,color:t.dim,lineHeight:1.7,wordBreak:"break-word"}}>
          {a.conversation_title&&<div>Chat: {a.conversation_title}</div>}
          {a.run_id&&<div>Run: {a.run_id}</div>}
          {a.workflow_id&&<div>Workflow: {a.workflow_id}</div>}
          {meta.source_tool&&<div>Source: {meta.source_tool}</div>}
          {a.sha256&&<div style={{fontFamily:"monospace",fontSize:10}}>sha256: {a.sha256.slice(0,24)}...</div>}
        </div>
      </section>
      {onDelete&&<button onClick={()=>onDelete(a.id)} style={{...btnS(t.err),justifyContent:"center"}}>Delete metadata</button>}
    </div>
  </div>;
}

const ArtifactCard=({artifact,t,font,workspaces,onPreview,onOpenConv,onPatch,onDelete,onDetails,onSelect,selected,compact})=>{
  const a=artifact||{};
  const meta=a.metadata||{};
  const km=artifactKindMeta(a.kind,t,meta);
  const KindIcon=km.icon;
  const title=a.title||a.filename||"Artifact";
  const created=a.created_at?new Date(a.created_at).toLocaleString([],{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"";
  const rename=async()=>{
    const next=window.prompt("Artifact name",title);
    if(next===null)return;
    const clean=String(next||"").trim();
    if(clean&&clean!==title)await onPatch?.(a.id,{title:clean});
  };
  const assign=async(e)=>{
    const wsId=e.target.value||null;
    await onPatch?.(a.id,{workspace_id:wsId});
  };
  const primaryBtn=(c)=>({display:"flex",alignItems:"center",justifyContent:"center",background:`${c}14`,border:`1px solid ${c}38`,color:c,cursor:"pointer",padding:"7px 0",borderRadius:6,fontFamily:font,fontSize:11,fontWeight:800,textDecoration:"none",minWidth:0});
  const utilBtn=(c,active)=>({flex:1,display:"flex",alignItems:"center",justifyContent:"center",background:"transparent",border:`1px solid ${t.brd}33`,color:active?c:t.mut,cursor:"pointer",padding:"5px 0",borderRadius:6,fontFamily:font,fontSize:10,fontWeight:700,minWidth:0,whiteSpace:"nowrap"});
  const selS={width:"100%",background:t.bgDeep,border:`1px solid ${t.brd}36`,color:t.dim,padding:"6px 8px",borderRadius:6,fontFamily:font,fontSize:10,outline:"none",cursor:"pointer"};
  const primaryCount=1+(onDetails?1:0)+1;
  return <div style={{border:`1px solid ${km.color}30`,background:`${t.surface}66`,borderRadius:10,padding:compact?11:13,display:"flex",flexDirection:"column",gap:10,minWidth:0,animation:"fadeIn .18s"}}>
    <div style={{display:"flex",alignItems:"center",gap:9,minWidth:0}}>
      {!compact&&onSelect&&<input type="checkbox" checked={!!selected} onChange={e=>onSelect(a.id,e.target.checked)} style={{flexShrink:0,cursor:"pointer"}}/>}
      <div style={{width:34,height:34,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",background:`${km.color}14`,border:`1px solid ${km.color}38`,color:km.color,fontSize:17,flexShrink:0}}>{KindIcon?<KindIcon/>:null}</div>
      <div style={{minWidth:0,flex:1}}>
        <div title={title} style={{fontSize:12.5,fontWeight:800,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{title}</div>
        <div title={a.filename} style={{fontSize:10,color:t.mut,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",marginTop:1}}>{a.filename}</div>
      </div>
      <span style={{fontSize:9,color:km.color,background:`${km.color}14`,border:`1px solid ${km.color}34`,borderRadius:6,padding:"2px 7px",fontWeight:800,flexShrink:0,letterSpacing:.3}}>{km.label}</span>
    </div>
    <div style={{display:"flex",flexDirection:"column",gap:5,fontSize:10,color:t.mut,minWidth:0,paddingBottom:2,borderBottom:`1px solid ${t.brd}1c`}}>
      {a.conversation_title&&<button onClick={()=>a.conversation_id&&onOpenConv?.(a.conversation_id)} title={a.conversation_title} style={{background:"none",border:"none",padding:0,color:t.acc,cursor:a.conversation_id?"pointer":"default",fontFamily:font,fontSize:10,textAlign:"left",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{a.conversation_title}</button>}
      <div style={{display:"flex",alignItems:"center",gap:7,flexWrap:"wrap"}}>
        {created&&<span>{created}</span>}
        {(a.workspace_name||meta.project_id)&&<span style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:"100%"}}>· {a.workspace_name||meta.project_id}</span>}
      </div>
      {((compact&&a.status)||a.pinned||a.exists_status==="missing"||a.stale||a.latest_for_project)&&<div style={{display:"flex",gap:7,flexWrap:"wrap",alignItems:"center"}}>
        {compact&&a.status&&<span style={{textTransform:"capitalize"}}>{a.status}</span>}
        {a.pinned&&<span style={{color:t.warm,fontWeight:700}}>pinned</span>}
        {a.exists_status==="missing"&&<span style={{color:t.err,fontWeight:700}}>missing</span>}
        {a.stale&&<span title="The project changed after this artifact was packaged — repackage for the latest" style={{color:t.warm,fontWeight:800}}>⚠ stale</span>}
        {a.latest_for_project&&!a.stale&&<span title="Newest delivery for this project" style={{color:t.ok,fontWeight:800}}>latest</span>}
      </div>}
    </div>
    <div style={{display:"grid",gridTemplateColumns:`repeat(${primaryCount},1fr)`,gap:6}}>
      {onPreview&&<button onClick={()=>onPreview(a.filename,a.url)} style={primaryBtn(t.acc)}>Preview</button>}
      {onDetails&&<button onClick={()=>onDetails(a.id)} style={primaryBtn(t.f1)}>Details</button>}
      <a href={a.id?`${API}/api/artifacts/${a.id}/download`:`${API}${a.url}`} download={a.filename} style={primaryBtn(t.ok)}>Download</a>
    </div>
    <div style={{display:"flex",gap:6}}>
      {a.conversation_id?<button onClick={()=>onOpenConv?.(a.conversation_id)} style={utilBtn(t.acc,false)}>Chat</button>:<div style={{flex:1}}/>}
      <button onClick={()=>onPatch?.(a.id,{pinned:!a.pinned})} style={utilBtn(t.warm,a.pinned)}>{a.pinned?"Unpin":"Pin"}</button>
      <button onClick={rename} style={utilBtn(t.acc,false)}>Rename</button>
      {onDelete&&<button onClick={()=>onDelete(a.id)} style={utilBtn(t.err,false)}>Delete</button>}
    </div>
    {!compact&&<div style={{display:"flex",flexDirection:"column",gap:6,marginTop:2}}>
      <select value={a.status||"draft"} onChange={e=>onPatch?.(a.id,{status:e.target.value})} style={selS}>{["draft","accepted","archived"].map(s=><option key={s} value={s}>{s}</option>)}</select>
      {workspaces?.length>0&&<select value={a.workspace_id||""} onChange={assign} style={selS}>
        <option value="">No workspace</option>
        {workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}
      </select>}
    </div>}
  </div>;
};

function ArtifactStudioPanel({t,font,workspaces,kbs,onPreview,onOpenConv,onUseInChat,focusId,onFocusConsumed,notify}){
  const [items,setItems]=useState([]);
  const [view,setView]=useState("all");
  const [q,setQ]=useState("");
  const [loading,setLoading]=useState(false);
  const [detail,setDetail]=useState(null);
  const [selected,setSelected]=useState(()=>new Set());
  const filters=[
    ["all","All"],["pinned","Pinned"],["recent","Recent"],["project","Projects"],
    ["image","Images"],["data","Data"],["code","Code"],["archived","Archived"]
  ];
  const viewParams=(v)=>{
    if(v==="pinned")return{pinned:"true"};
    if(v==="archived")return{status:"archived"};
    if(v==="project")return{kind:"project"};
    if(["image","data","code"].includes(v))return{kind:v};
    return{};
  };
  const load=useCallback(async()=>{
    setLoading(true);
    try{
      const params=new URLSearchParams({limit:"120"});
      Object.entries(viewParams(view)).forEach(([k,v])=>params.set(k,v));
      if(q.trim())params.set("q",q.trim());
      const list=await fetch(`${API}/api/artifacts?${params.toString()}`).then(r=>r.ok?r.json():[]);
      let merged=Array.isArray(list)?list:[];
      if(focusId){
        const one=await fetch(`${API}/api/artifacts/${encodeURIComponent(focusId)}`).then(r=>r.ok?r.json():null).catch(()=>null);
        if(one)merged=[one,...merged.filter(x=>x.id!==one.id)];
        if(one)setDetail(one);
        onFocusConsumed&&onFocusConsumed();
      }
      setItems(merged);
    }catch(e){notify&&notify({type:"error",text:"Artifacts failed to load",detail:e.message||String(e)});}
    setLoading(false);
  },[view,q,focusId,onFocusConsumed,notify]);
  useEffect(()=>{load();},[load]);
  const openDetail=async(id)=>{
    try{
      const d=await fetch(`${API}/api/artifacts/${encodeURIComponent(id)}`).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();});
      setDetail(d);
    }catch(e){notify&&notify({type:"error",text:"Artifact detail failed",detail:e.message||String(e)});}
  };
  const patchArtifact=async(id,patch)=>{
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(id)}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const d=await r.json();
      setItems(p=>p.map(x=>x.id===id?d:x));
      setDetail(p=>p?.id===id?d:p);
      notify&&notify({type:"success",text:"Artifact updated",duration:1800});
      return d;
    }catch(e){notify&&notify({type:"error",text:"Artifact update failed",detail:e.message||String(e)});}
  };
  const deleteArtifact=async(id)=>{
    if(!id)return;
    try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(id)}`,{method:"DELETE"});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      setItems(p=>p.filter(x=>x.id!==id));
      setSelected(p=>{const n=new Set(p);n.delete(id);return n;});
      setDetail(p=>p?.id===id?null:p);
    }catch(e){notify&&notify({type:"error",text:"Artifact delete failed",detail:e.message||String(e)});}
  };
  const bundleSelected=async()=>{
    const ids=Array.from(selected);
    if(!ids.length)return;
    try{
      const r=await fetch(`${API}/api/artifacts/bundle`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({artifact_ids:ids,title:`Artifact bundle (${ids.length})`})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);
      setItems(p=>[d,...p]);
      setSelected(new Set());
      setDetail(d);
      notify&&notify({type:"success",text:"Bundle created",detail:d.filename,duration:2500});
    }catch(e){notify&&notify({type:"error",text:"Bundle failed",detail:e.message||String(e)});}
  };
  const toggleSelected=(id,on)=>setSelected(p=>{const n=new Set(p);if(on)n.add(id);else n.delete(id);return n;});
  return <div style={{flex:1,display:"flex",overflow:"hidden"}}>
  <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div style={{padding:"16px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",alignItems:"center",gap:10,flexWrap:"wrap"}}>
      <div style={{display:"flex",alignItems:"center",gap:8,minWidth:160}}><IC.Layers/><span style={{fontSize:14,fontWeight:800,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Artifacts</span></div>
      <div style={{display:"flex",gap:4,flexWrap:"wrap"}}>
        {filters.map(([id,label])=><button key={id} onClick={()=>setView(id)} style={{padding:"6px 10px",borderRadius:7,border:`1px solid ${view===id?t.acc:t.brd}35`,background:view===id?`${t.acc}18`:"transparent",color:view===id?t.acc:t.mut,cursor:"pointer",fontFamily:font,fontSize:10,fontWeight:view===id?800:600}}>{label}</button>)}
      </div>
      <input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search artifacts..." style={{marginLeft:"auto",minWidth:180,background:t.bgDeep,border:`1px solid ${t.brd}44`,color:t.text,padding:"7px 10px",borderRadius:7,fontFamily:font,fontSize:11,outline:"none"}}/>
      {selected.size>0&&<button onClick={bundleSelected} style={{background:`${t.warm}16`,border:`1px solid ${t.warm}35`,color:t.warm,cursor:"pointer",padding:"7px 10px",borderRadius:7,fontFamily:font,fontSize:10,fontWeight:800}}>Bundle {selected.size}</button>}
      <button onClick={load} style={{background:`${t.surface}66`,border:`1px solid ${t.brd}35`,color:t.mut,cursor:"pointer",padding:"7px 10px",borderRadius:7,fontFamily:font,fontSize:10,fontWeight:800}}>Refresh</button>
    </div>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {loading&&<div style={{fontSize:12,color:t.mut,padding:18}}>Loading artifacts...</div>}
      {!loading&&!items.length&&<div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:8,textAlign:"center",padding:"36px 20px",border:`1px dashed ${t.brd}44`,borderRadius:10,background:`${t.surface}30`,color:t.mut,fontFamily:font}}>
        <div style={{fontSize:30,lineHeight:1,opacity:.9,animation:"float 4s ease-in-out infinite",display:"flex",justifyContent:"center"}}><IC.Layers/></div>
        <div style={{fontSize:13,fontWeight:800,color:t.dim}}>No artifacts yet</div>
        <div style={{fontSize:11,lineHeight:1.55,maxWidth:420}}>Files the assistant delivers with download_file or download_project are collected here.</div>
      </div>}
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(250px,1fr))",gap:12}}>
        {items.map(a=><ArtifactCard key={a.id} artifact={a} t={t} font={font} workspaces={workspaces} onPreview={onPreview} onOpenConv={onOpenConv} onPatch={patchArtifact} onDelete={deleteArtifact} onDetails={openDetail} onSelect={toggleSelected} selected={selected.has(a.id)}/>)}
      </div>
    </div>
  </div>
  {detail&&<ArtifactDetailPanel artifact={detail} t={t} font={font} workspaces={workspaces} kbs={kbs} onClose={()=>setDetail(null)} onPatch={patchArtifact} onDelete={deleteArtifact} onUseInChat={onUseInChat} notify={notify} onRefreshList={load} onOpenArtifact={openDetail}/>}
  </div>;
}



export {
  artifactKindMeta,
  ArtifactPreviewBlock,
  ImageStudioPanel,
  ArtifactDetailPanel,
  ArtifactCard,
  ArtifactStudioPanel,
};
