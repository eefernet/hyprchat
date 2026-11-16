import React,{useMemo,useState} from 'react';
import {withPreviewCsp,PREVIEW_WIDTHS} from '../htmlPreview.js';
import {IC} from './icons.jsx';
import './htmlPreview.css';

const ISOLATION='Sandboxed: no access to HyprChat, no network requests, no form posts.';

// Runs HTML the model wrote in a sandboxed frame. allow-scripts without
// allow-same-origin gives the page an opaque origin: no cookies, storage, DOM or
// /api access; the injected CSP keeps it off the network. Never open the code as
// a blob URL (that would run it on HyprChat's own origin) — download only.
//
// It draws the whole pane: one header row (status, title, width, libraries, reload,
// download, close) and the page edge to edge under it. The controls used to sit in a
// second row with a note line under the page, inside a padded body.
export default function HtmlLivePreview({html,font,onClose}){
  const [widthId,setWidthId]=useState('full'),[mode,setMode]=useState('cdn'),[reloads,setReloads]=useState(0);
  const code=html?.code||'';
  const doc=useMemo(()=>withPreviewCsp(code,mode),[code,mode]);
  const width=PREVIEW_WIDTHS.find(option=>option.id===widthId)?.width||0;
  const title=html?.title||'Preview';
  const live=!!html?.streaming;
  const status=`${live?'Updating as the reply streams.':'Scripts on.'} ${ISOLATION}`;
  const download=()=>{
    const name=String(html?.title||'preview').replace(/\.html?$/i,'').replace(/[^\w.-]+/g,'-')||'preview';
    const url=URL.createObjectURL(new Blob([code],{type:'text/html'}));
    const link=document.createElement('a');link.href=url;link.download=`${name}.html`;
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  return <div className="hp-root" style={{fontFamily:font}}>
    <div className="hp-bar">
      <span className={`hp-dot${live?' hp-dot-live':''}`} role="img" aria-label={status} title={status}/>
      <span className="hp-title" title={title}>{title}</span>
      <div className="hp-tools">
        <div className="hp-group" role="group" aria-label="Preview width">
          {PREVIEW_WIDTHS.map(option=><button key={option.id} type="button" aria-pressed={widthId===option.id} onClick={()=>setWidthId(option.id)} title={option.width?`${option.width}px wide`:'Fill the pane'}>{option.label}</button>)}
        </div>
        <button type="button" aria-pressed={mode==='strict'} onClick={()=>setMode(current=>current==='strict'?'cdn':'strict')}
          title={mode==='strict'?'Strict: inline code only. Click to allow libraries from https CDNs.':'Libraries from https CDNs may load. Click for strict (inline code only).'}>{mode==='strict'?'Strict':'CDN'}</button>
        <button type="button" className="hp-icon" onClick={()=>setReloads(n=>n+1)} aria-label="Reload preview" title="Reload"><IC.Refresh/></button>
        <button type="button" className="hp-icon" onClick={download} aria-label="Download HTML file" title="Download .html"><IC.Download/></button>
      </div>
      {onClose&&<button type="button" className="hp-icon hp-close" onClick={onClose} aria-label="Close preview" title="Close preview"><IC.X/></button>}
    </div>
    <div className="hp-stage">
      <iframe key={reloads} className="hp-frame" sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={doc} title={title} style={{width:width?`${width}px`:'100%',maxWidth:'100%'}}/>
    </div>
  </div>;
}
