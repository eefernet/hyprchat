import React,{useMemo,useState} from 'react';
import {withPreviewCsp,PREVIEW_WIDTHS} from '../htmlPreview.js';
import {IC} from './icons.jsx';
import './htmlPreview.css';

// Runs HTML the model wrote in a sandboxed frame. allow-scripts without
// allow-same-origin gives the page an opaque origin: no cookies, storage, DOM or
// /api access; the injected CSP keeps it off the network. Never open the code as
// a blob URL (that would run it on HyprChat's own origin) — download only.
export default function HtmlLivePreview({html,font}){
  const [widthId,setWidthId]=useState('full'),[mode,setMode]=useState('cdn'),[reloads,setReloads]=useState(0);
  const code=html?.code||'';
  const doc=useMemo(()=>withPreviewCsp(code,mode),[code,mode]);
  const width=PREVIEW_WIDTHS.find(option=>option.id===widthId)?.width||0;
  const download=()=>{
    const name=String(html?.title||'preview').replace(/\.html?$/i,'').replace(/[^\w.-]+/g,'-')||'preview';
    const url=URL.createObjectURL(new Blob([code],{type:'text/html'}));
    const link=document.createElement('a');link.href=url;link.download=`${name}.html`;
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  return <div className="hp-root" style={{fontFamily:font}}>
    <div className="hp-toolbar">
      <span className={`hp-chip hp-chip-ok${html?.streaming?' hp-chip-live':''}`}>{html?.streaming?'updating as it streams':'scripts on'} · sandboxed</span>
      <span className="hp-spacer"/>
      <div className="hp-group" role="group" aria-label="Preview width">
        {PREVIEW_WIDTHS.map(option=><button key={option.id} type="button" aria-pressed={widthId===option.id} onClick={()=>setWidthId(option.id)} title={option.width?`${option.width}px wide`:'Fill the pane'}>{option.label}</button>)}
      </div>
      <button type="button" aria-pressed={mode==='strict'} onClick={()=>setMode(current=>current==='strict'?'cdn':'strict')}
        title={mode==='strict'?'Strict: inline code only. Click to allow libraries from https CDNs.':'Libraries from https CDNs may load. Click for strict (inline code only).'}>{mode==='strict'?'Strict':'CDN ok'}</button>
      <button type="button" onClick={()=>setReloads(n=>n+1)} aria-label="Reload preview" title="Reload"><IC.Refresh/></button>
      <button type="button" onClick={download} aria-label="Download HTML file" title="Download .html"><IC.Download/></button>
    </div>
    <div className="hp-stage">
      <iframe key={reloads} className="hp-frame" sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={doc} title={html?.title||'HTML preview'} style={{width:width?`${width}px`:'100%',maxWidth:'100%'}}/>
    </div>
    <p className="hp-note">Isolated frame: no access to HyprChat, no network requests, no form posts.</p>
  </div>;
}
