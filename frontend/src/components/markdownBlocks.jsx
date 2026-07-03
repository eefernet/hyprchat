import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

export function sanitizeMermaidCode(src){
  let out=String(src||"").replace(/\r\n/g,"\n");
  out=out
    .replace(/<\s*[—–-]+\s*>/g,"<-->")
    .replace(/[—–]+>/g,"-->")
    .replace(/<\s*[—–]+/g,"<--")
    .replace(/\s+↔\s+/g," <--> ")
    .replace(/\s+→\s+/g," --> ")
    .replace(/\s+←\s+/g," <-- ")
    .replace(/^\s*graph\s+(TD|TB|BT|LR|RL)\b/i,(m,dir)=>m.replace(/graph/i,"flowchart"));
  out=out.replace(/(^|[^\w])([A-Za-z][\w-]*)\[([^\]\n"]+)\]/g,(m,prefix,id,label)=>{
    const clean=label.replace(/\\"/g,'"').replace(/"/g,'\\"').trim();
    return `${prefix}${id}["${clean}"]`;
  });
  out=out.split("\n").map(line=>{
    const m=line.match(/^(\s*)([A-Za-z][\w-]*(?:\s*&\s*[A-Za-z][\w-]*)+)\s*(-->|<-->|---|--)\s*([A-Za-z][\w-]*)\s*$/);
    if(!m)return line;
    const [,indent,left,arrow,right]=m;
    return left.split(/\s*&\s*/).map(node=>`${indent}${node} ${arrow} ${right}`).join("\n");
  }).join("\n");
  return out;
}

export function initMermaidTheme(theme,font){
  if(!window.mermaid)return;
  const t=theme||{};
  const bg=t.bgDeep||"#ffffff";
  const surface=t.surface||"#f8fafc";
  const brd=t.brd||"#d7dde8";
  const acc=t.acc||"#175cd3";
  const text=t.text||"#161b22";
  const dim=t.dim||"#374151";
  const mut=t.mut||"#667085";
  const warm=t.warm||"#b7791f";
  const err=t.err||"#b42318";
  const sfHov=t.sfHov||"#eef4ff";
  window.mermaid.initialize({
    startOnLoad:false,
    securityLevel:"strict",
    theme:"base",
    fontFamily:font,
    // SVG <text> labels everywhere. The default htmlLabels mode puts flowchart/class/
    // state labels in <foreignObject> HTML, which browsers (notably WebKit with a
    // CSS-scaled svg) can fail to paint — boxes render but text is invisible — and
    // html2canvas can't rasterize for PDF export. Sequence diagrams already use SVG
    // text, which is why they were unaffected.
    htmlLabels:false,
    flowchart:{htmlLabels:false},
    class:{htmlLabels:false},
    themeVariables:{
      background:bg,
      primaryColor:surface,
      primaryTextColor:text,
      primaryBorderColor:brd,
      secondaryColor:bg,
      secondaryTextColor:dim,
      secondaryBorderColor:brd,
      tertiaryColor:sfHov,
      tertiaryTextColor:text,
      tertiaryBorderColor:brd,
      lineColor:acc,
      textColor:text,
      mainBkg:surface,
      nodeBorder:acc,
      nodeTextColor:text,
      clusterBkg:bg,
      clusterBorder:brd,
      titleColor:text,
      // Edge labels (e.g. flowchart link text) sit on a solid panel-colored chip so
      // they stay legible over any theme background.
      edgeLabelBackground:surface,
      // Class-diagram text — not covered by the default base-theme vars, which is why
      // class diagrams previously rendered with empty/invisible boxes.
      classText:text,
      relationLabelColor:text,
      relationLabelBackground:surface,
      // State / pie diagram text
      stateLabelColor:text,
      pieTitleTextColor:text,
      pieSectionTextColor:text,
      noteBkgColor:`${warm}22`,
      noteTextColor:text,
      noteBorderColor:warm,
      errorBkgColor:`${err}22`,
      errorTextColor:err,
      actorBkg:surface,
      actorBorder:acc,
      actorTextColor:text,
      actorLineColor:mut,
      signalColor:dim,
      signalTextColor:text,
      labelBoxBkgColor:surface,
      labelBoxBorderColor:brd,
      labelTextColor:text,
      loopTextColor:text,
      activationBkgColor:sfHov,
      activationBorderColor:acc
    }
  });
}

export function MermaidBlock({code,theme,font,epoch,printMode=false,streaming=false}){
  const t=theme;
  const containerRef=useRef(null);
  const fsRef=useRef(null);
  const panZoomRef=useRef(null);
  const [err,setErr]=useState(null);
  const [pending,setPending]=useState(false);
  const [repaired,setRepaired]=useState(false);
  const [cpd,setCpd]=useState(false);
  const [showSrc,setShowSrc]=useState(false);
  const [fs,setFs]=useState(false);
  const [tick,setTick]=useState(0); // bumped when the lazy mermaid chunk loads
  const id=useMemo(()=>`mmd-${Math.random().toString(36).slice(2,10)}`,[]);
  useEffect(()=>{
    if(!window.mermaid){window.ensureMermaid&&window.ensureMermaid().then(()=>setTick(x=>x+1));return;}
    if(!containerRef.current||showSrc)return;
    let cancelled=false;
    setErr(null);
    setRepaired(false);
    setPending(true);
    try{
      // Apply the current theme before rendering. Mermaid is lazy-loaded now, so the
      // app-level init effect may have run (and bailed) before mermaid existed — this
      // guarantees the user's theme colors are set for the block that triggered the load
      // and on every theme change (effect deps include t/font).
      initMermaidTheme(t,font);
      const repairedCode=sanitizeMermaidCode(code);
      const renderOne=(src,suffix)=>window.mermaid.render(`${id}-${suffix}`,src);
      renderOne(code,"raw").then(({svg})=>{
        if(!cancelled&&containerRef.current){containerRef.current.innerHTML=svg;setPending(false);}
      }).catch(firstErr=>{
        if(repairedCode===code){
          if(!cancelled){
            if(streaming){setErr(null);setPending(true);}
            else{setPending(false);setErr(String(firstErr?.message||firstErr));}
          }
          return;
        }
        renderOne(repairedCode,"fixed").then(({svg})=>{
          if(!cancelled&&containerRef.current){containerRef.current.innerHTML=svg;setRepaired(true);setPending(false);}
        }).catch(e=>{
          if(!cancelled){
            if(streaming){setErr(null);setPending(true);}
            else{setPending(false);setErr(String(e?.message||firstErr?.message||e||firstErr));}
          }
        });
      });
    }catch(e){
      if(streaming){setErr(null);setPending(true);}
      else{setPending(false);setErr(String(e?.message||e));}
    }
    return()=>{cancelled=true;};
  },[code,epoch,showSrc,id,printMode,t,font,streaming,tick]);
  useEffect(()=>{
    if(!fs||!fsRef.current||!containerRef.current)return;
    const srcSvg=containerRef.current.querySelector("svg");
    if(!srcSvg)return;
    const clone=srcSvg.cloneNode(true);
    clone.removeAttribute("style");
    clone.setAttribute("width","100%");
    clone.setAttribute("height","100%");
    clone.style.maxWidth="none";
    clone.style.display="block";
    fsRef.current.innerHTML="";
    fsRef.current.appendChild(clone);
    try{
      panZoomRef.current=window.svgPanZoom(clone,{
        controlIconsEnabled:true,
        fit:true,
        center:true,
        minZoom:0.3,
        maxZoom:12,
        zoomScaleSensitivity:0.35,
        contain:false
      });
    }catch(e){console.warn("svg-pan-zoom failed",e);}
    return()=>{
      try{panZoomRef.current?.destroy();}catch{}
      panZoomRef.current=null;
    };
  },[fs]);
  useEffect(()=>{
    if(!fs)return;
    const onKey=e=>{if(e.key==="Escape")setFs(false);};
    window.addEventListener("keydown",onKey);
    return()=>window.removeEventListener("keydown",onKey);
  },[fs]);
  const copyCode=()=>{try{navigator.clipboard.writeText(code);setCpd(true);setTimeout(()=>setCpd(false),1500);}catch{}};
  const headerBtn={background:"none",border:"none",cursor:"pointer",padding:"3px 6px",fontSize:10,fontFamily:font,borderRadius:5,display:"flex",alignItems:"center",gap:3};
  const wrapStyle=printMode
    ?{margin:"12pt 0",borderRadius:6,overflow:"hidden",border:`1px solid ${t.brd}`,background:"#ffffff",breakInside:"avoid",pageBreakInside:"avoid"}
    :{margin:"10px 0",borderRadius:10,overflow:"hidden",border:`1px solid ${t.brd}55`,background:t.bgDeep};
  const headStyle=printMode
    ?{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5pt 8pt",background:"#f8fafc",borderBottom:`1px solid ${t.brd}`}
    :{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5px 12px",background:`${t.surface}BB`,borderBottom:`1px solid ${t.brd}33`};
  return <div className={printMode?"mermaid-print-block":undefined} style={wrapStyle}>
    <div style={headStyle}>
      <span style={{display:"flex",alignItems:"center",gap:5,fontSize:10,color:t.acc,textTransform:"uppercase",letterSpacing:1,fontWeight:700}}>◈ mermaid{repaired&&<span style={{color:t.warm,letterSpacing:0,textTransform:"none",fontWeight:600}}>repaired</span>}</span>
      {!printMode&&<div style={{display:"flex",gap:4}}>
        {!err&&!showSrc&&<button onClick={()=>setFs(true)} style={{...headerBtn,color:t.dim}} title="fullscreen / zoom">⛶ expand</button>}
        <button onClick={()=>setShowSrc(s=>!s)} style={{...headerBtn,color:showSrc?t.acc:t.dim}} title={showSrc?"show diagram":"show source"}>{showSrc?"◱ diagram":"{ } source"}</button>
        <button onClick={copyCode} style={{...headerBtn,color:cpd?t.ok:t.dim}} title="copy source">{cpd?"✓ copied":"⧉ copy"}</button>
      </div>}
    </div>
    {err?<div>
      <div style={{padding:"8px 12px",background:`${t.err}12`,color:t.err,fontSize:11,fontFamily:font,borderBottom:`1px solid ${t.err}33`}}>⚠ Mermaid render error: {err}</div>
      <pre style={{margin:0,padding:12,fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,color:t.dim}}>{code}</pre>
    </div>
    :showSrc?<pre style={{margin:0,padding:12,fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,color:t.dim}}>{code}</pre>
    :<div style={{position:"relative",background:printMode?"#ffffff":t.bgDeep}}>
      <div ref={containerRef} className="mermaid-container" style={{background:printMode?"#ffffff":t.bgDeep,minHeight:streaming&&pending?120:undefined,opacity:streaming&&pending?0:1,transition:"opacity .18s ease"}}/>
      {streaming&&pending&&<div style={{position:"absolute",inset:0,display:"flex",alignItems:"center",justifyContent:"center",gap:8,color:t.mut,fontSize:11,fontFamily:font,background:t.bgDeep}}>
        <span style={{width:5,height:5,borderRadius:"50%",background:t.acc,animation:"pulse 1.4s infinite"}}/>
        rendering diagram...
      </div>}
    </div>}
    {!printMode&&fs&&createPortal(
      <div style={{position:"fixed",inset:0,zIndex:200,background:"rgba(0,0,0,.82)",display:"flex",alignItems:"center",justifyContent:"center",backdropFilter:"blur(6px)"}} onClick={e=>{if(e.target===e.currentTarget)setFs(false);}}>
        <div style={{background:t.bgDeep,border:`1px solid ${t.brd}44`,borderRadius:16,width:"min(1600px,95vw)",height:"90vh",display:"flex",flexDirection:"column",boxShadow:`0 8px 48px #0008`,overflow:"hidden"}}>
          <div style={{padding:"10px 16px",borderBottom:`1px solid ${t.brd}22`,display:"flex",alignItems:"center",gap:10,flexShrink:0,background:`${t.surface}88`}}>
            <span style={{fontSize:11,color:t.acc,textTransform:"uppercase",letterSpacing:1,fontWeight:700}}>◈ mermaid — fullscreen</span>
            <span style={{fontSize:10,color:t.mut,flex:1}}>drag to pan · scroll to zoom · ESC to close</span>
            <button onClick={()=>panZoomRef.current?.reset()} style={{...headerBtn,color:t.dim}} title="reset view">⟲ reset</button>
            <button onClick={()=>setFs(false)} style={{background:"none",border:"none",color:t.mut,cursor:"pointer",fontSize:18,padding:"0 4px",lineHeight:1}} title="close">✕</button>
          </div>
          <div ref={fsRef} style={{flex:1,overflow:"hidden",background:t.bgDeep}}/>
        </div>
      </div>,
      document.body
    )}
  </div>;
}

export function parseChartConfig(code){
  let cfg=typeof code==="string"?JSON.parse(code):code;
  if(!cfg||typeof cfg!=="object"||Array.isArray(cfg))throw new Error("Chart config must be a JSON object");
  const nativeData=(cfg.data&&typeof cfg.data==="object"&&!Array.isArray(cfg.data))?cfg.data:null;
  if(nativeData&&(Array.isArray(nativeData.datasets)||Array.isArray(nativeData.labels))){
    return {...cfg,labels:nativeData.labels||cfg.labels||[],datasets:nativeData.datasets||cfg.datasets||[]};
  }
  if(Array.isArray(cfg.data)&&!cfg.datasets)return {...cfg,datasets:[{label:cfg.label||"Value",data:cfg.data}]};
  return {...cfg,datasets:cfg.datasets||[]};
}
export function looksLikeChartConfig(code){
  try{
    const cfg=parseChartConfig(code);
    const type=cfg.type||"bar";
    const allowed=["bar","line","pie","doughnut","scatter","radar","polarArea","bubble"];
    return allowed.includes(type)&&(
      Array.isArray(cfg.datasets)||
      Array.isArray(cfg.labels)||
      (cfg.data&&typeof cfg.data==="object")
    );
  }catch{return false;}
}

export function normalizeRenderableFences(text){
  if(!text)return text;
  // Recover model output like `intro:```chart\n{...}`. CommonMark requires
  // fences to start a line, but models sometimes attach visual fences to prose.
  return text.replace(/([^\n])```(chart|pygraph|mermaid)(?=\n)/g,"$1\n```$2");
}

// Chart.js block — renders ```chart / ```pygraph JSON fences as inline charts
export function ChartBlock({code,theme,font,epoch,kind="chart",printMode=false,streaming=false}){
  const t=theme;
  const canvasRef=useRef(null);
  const chartRef=useRef(null);
  const [err,setErr]=useState(null);
  const [pending,setPending]=useState(false);
  const [cpd,setCpd]=useState(false);
  const [showSrc,setShowSrc]=useState(false);
  const [tick,setTick]=useState(0); // bumped when the lazy chart.js chunk loads
  useEffect(()=>{
    if(!window.Chart){window.ensureChart&&window.ensureChart().then(()=>setTick(x=>x+1));if(streaming)setPending(true);return;}
    if(!canvasRef.current||showSrc){if(streaming&&!showSrc)setPending(true);return;}
    let cfg;
    try{cfg=parseChartConfig(code);}
    catch(e){
      if(streaming){setErr(null);setPending(true);}
      else{setPending(false);setErr("Invalid JSON: "+String(e.message));}
      return;
    }
    setPending(false);
    const palette=printMode
      ?["#175cd3","#0f766e","#b7791f","#b42318","#7c3aed","#0891b2","#c2410c","#2563eb"]
      :[t.acc,t.ok,t.warm,t.err||"#e06c75","#b39ddb","#80cbc4","#ffab91","#90caf9"];
    const chartText=printMode?"#263238":t.text;
    const chartDim=printMode?"#475467":t.dim;
    const chartMut=printMode?"#667085":t.mut;
    const chartGrid=printMode?"#d7dde8":t.brd+"33";
    const tooltipBg=printMode?"#ffffff":t.surface+"EE";
    const tooltipBorder=printMode?"#98a2b3":t.brd;
    const type=cfg.type||"bar";
    const isCircular=["pie","doughnut","polarArea"].includes(type);
    const datasets=(cfg.datasets||[]).map((ds,i)=>{
      const color=ds.color||ds.borderColor||palette[i%palette.length];
      const out={label:ds.label||`Dataset ${i+1}`,data:ds.data||[],borderWidth:ds.borderWidth??2,tension:ds.tension??0.25,...ds};
      if(isCircular){
        out.backgroundColor=ds.backgroundColor||(ds.data||[]).map((_,k)=>palette[k%palette.length]+"CC");
        out.borderColor=ds.borderColor||(printMode?"#ffffff":t.bgDeep);
        out.borderWidth=ds.borderWidth??2;
      }else{
        out.backgroundColor=ds.backgroundColor||(type==="line"?color+"22":color+"AA");
        out.borderColor=ds.borderColor||color;
      }
      return out;
    });
    if(chartRef.current){try{chartRef.current.destroy();}catch{}chartRef.current=null;}
    try{
      chartRef.current=new window.Chart(canvasRef.current,{
        type,
        data:{labels:cfg.labels||[],datasets},
        options:{
          responsive:true,
          maintainAspectRatio:false,
          plugins:{
            legend:{labels:{color:chartDim,font:{family:font,size:11}}},
            title:cfg.title?{display:true,text:cfg.title,color:chartText,font:{family:font,size:13,weight:"bold"},padding:{bottom:10}}:{display:false},
            tooltip:{bodyFont:{family:font},titleFont:{family:font},backgroundColor:tooltipBg,borderColor:tooltipBorder,borderWidth:1,titleColor:chartText,bodyColor:chartDim}
          },
          scales:isCircular?{}:{
            x:{ticks:{color:chartMut,font:{family:font,size:10}},grid:{color:chartGrid},title:cfg.xLabel?{display:true,text:cfg.xLabel,color:chartDim,font:{family:font,size:11}}:{display:false}},
            y:{ticks:{color:chartMut,font:{family:font,size:10}},grid:{color:chartGrid},title:cfg.yLabel?{display:true,text:cfg.yLabel,color:chartDim,font:{family:font,size:11}}:{display:false}}
          },
          ...(cfg.options||{}),
          animation:printMode?false:(cfg.options||{}).animation
        }
      });
      setErr(null);
    }catch(e){
      if(streaming){setErr(null);setPending(true);}
      else{setPending(false);setErr("Chart error: "+String(e.message));}
    }
    return()=>{if(chartRef.current){try{chartRef.current.destroy();}catch{}chartRef.current=null;}};
  },[code,epoch,showSrc,t,font,printMode,streaming,tick]);
  const copyCode=()=>{try{navigator.clipboard.writeText(code);setCpd(true);setTimeout(()=>setCpd(false),1500);}catch{}};
  const headerBtn={background:"none",border:"none",cursor:"pointer",padding:"3px 6px",fontSize:10,fontFamily:font,borderRadius:5,display:"flex",alignItems:"center",gap:3};
  const wrapStyle=printMode
    ?{margin:"12pt 0",borderRadius:6,overflow:"hidden",border:`1px solid ${t.brd}`,background:"#ffffff",breakInside:"avoid",pageBreakInside:"avoid"}
    :{margin:"10px 0",borderRadius:10,overflow:"hidden",border:`1px solid ${t.brd}55`,background:t.bgDeep};
  const headStyle=printMode
    ?{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5pt 8pt",background:"#f8fafc",borderBottom:`1px solid ${t.brd}`}
    :{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5px 12px",background:`${t.surface}BB`,borderBottom:`1px solid ${t.brd}33`};
  return <div className={printMode?"chart-print-block":undefined} style={wrapStyle}>
    <div style={headStyle}>
      <span style={{display:"flex",alignItems:"center",gap:5,fontSize:10,color:t.acc,textTransform:"uppercase",letterSpacing:1,fontWeight:700}}>◈ {kind}</span>
      {!printMode&&<div style={{display:"flex",gap:4}}>
        <button onClick={()=>setShowSrc(s=>!s)} style={{...headerBtn,color:showSrc?t.acc:t.dim}} title={showSrc?"show chart":"show source"}>{showSrc?"◱ chart":"{ } source"}</button>
        <button onClick={copyCode} style={{...headerBtn,color:cpd?t.ok:t.dim}} title="copy source">{cpd?"✓ copied":"⧉ copy"}</button>
      </div>}
    </div>
    {err?<div>
      <div style={{padding:"8px 12px",background:`${t.err}12`,color:t.err,fontSize:11,fontFamily:font,borderBottom:`1px solid ${t.err}33`}}>⚠ Chart render error: {err}</div>
      <pre style={{margin:0,padding:12,fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,color:t.dim}}>{code}</pre>
    </div>
    :showSrc?<pre style={{margin:0,padding:12,fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,color:t.dim}}>{code}</pre>
    :<div style={{padding:printMode?"10pt":"14px",height:printMode?280:320,position:"relative",background:printMode?"#ffffff":t.bgDeep}}>
      <canvas ref={canvasRef} style={{opacity:streaming&&pending?0:1,transition:"opacity .18s ease"}}/>
      {streaming&&pending&&<div style={{position:"absolute",inset:0,display:"flex",alignItems:"center",justifyContent:"center",gap:8,color:t.mut,fontSize:11,fontFamily:font,background:t.bgDeep}}>
        <span style={{width:5,height:5,borderRadius:"50%",background:t.acc,animation:"pulse 1.4s infinite"}}/>
        rendering chart...
      </div>}
    </div>}
  </div>;
}

// Syntax-highlighted code block (Prism)
export function CodeBlock({code,lang,t,font}){
  const ref=useRef(null);
  useEffect(()=>{
    if(!ref.current||!window.Prism)return;
    try{window.Prism.highlightElement(ref.current);}catch{}
  },[code,lang]);
  const cls=lang?`language-${lang}`:"language-none";
  return <pre style={{margin:0,padding:12,fontSize:12,lineHeight:1.6,overflowX:"auto",fontFamily:font,background:"transparent"}}><code ref={ref} className={cls} style={{fontFamily:font,background:"transparent",color:t.dim,padding:0,textShadow:"none"}}>{code}</code></pre>;
}

export function InlineMath({code,raw,theme,font,printMode=false}){
  const html=useMemo(()=>{
    if(!window.katex)return null;
    try{
      return window.katex.renderToString(code,{displayMode:false,throwOnError:false,strict:"ignore",trust:false});
    }catch{return null;}
  },[code]);
  if(!html)return <span>{raw||`$${code}$`}</span>;
  return <span style={{color:"inherit",fontFamily:printMode?undefined:font}} dangerouslySetInnerHTML={{__html:html}}/>;
}

export function InlineColorSwatch({value,theme,font,printMode=false}){
  const t=theme||{};
  const bg=printMode?"#f8fafc":`${t.surface||"#0f172a"}BB`;
  const brd=printMode?"#d7dde8":`${t.brd||"#334155"}55`;
  const label=printMode?"#334155":(t.warm||"#facc15");
  return <span style={{display:"inline-flex",alignItems:"center",gap:4,padding:"1px 6px 1px 4px",background:bg,border:`1px solid ${brd}`,borderRadius:4,verticalAlign:"middle",margin:"0 1px"}}>
    <span style={{width:10,height:10,borderRadius:2,background:value,border:`1px solid ${brd}`,flexShrink:0,boxShadow:"inset 0 0 0 1px rgba(255,255,255,.08)"}}/>
    <code style={{fontSize:"0.85em",color:label,background:"none",padding:0,fontFamily:font}}>{value}</code>
  </span>;
}

// Collapsible <details>/<summary> block
export function Collapsible({summary,children,theme,font,defaultOpen,variant=""}){
  const [open,setOpen]=useState(!!defaultOpen);
  const isReleaseSummary=typeof summary==="string"&&/^Alpha v/i.test(summary.trim());
  const isChangelogRelease=variant==="changelog"&&isReleaseSummary;
  return <div style={{margin:isChangelogRelease?"0 0 24px":"8px 0",border:isChangelogRelease?"none":`1px solid ${theme.brd}44`,borderRadius:isChangelogRelease?0:8,background:isChangelogRelease?"transparent":`${theme.surface}44`,overflow:"hidden"}}>
    <div onClick={()=>setOpen(o=>!o)} style={{padding:isReleaseSummary?(isChangelogRelease?"4px 0 14px":"13px 16px"):"6px 10px",cursor:"pointer",display:"flex",alignItems:"center",gap:isReleaseSummary?10:6,fontSize:isReleaseSummary?20:12,fontWeight:isReleaseSummary?900:600,color:isReleaseSummary?theme.acc:theme.dim,userSelect:"none",background:isChangelogRelease?"transparent":`${theme.surface}66`,letterSpacing:isReleaseSummary?0.2:0,lineHeight:1.25,borderBottom:isChangelogRelease&&open?`1px solid ${theme.brd}28`:"none"}}>
      <span style={{display:"inline-block",transition:"transform .15s",transform:open?"rotate(90deg)":"rotate(0)",color:theme.acc,fontSize:isReleaseSummary?13:10}}>▶</span>
      <span style={{fontWeight:isReleaseSummary?900:600}}>{summary||"Details"}</span>
    </div>
    {open&&<div style={{padding:isChangelogRelease?"22px 0 0":"8px 12px",fontSize:13,lineHeight:1.6,color:theme.dim,borderTop:isChangelogRelease?"none":`1px solid ${theme.brd}28`}}>{children}</div>}
  </div>;
}
