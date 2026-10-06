import React,{useEffect,useState} from 'react';
import {API,userScopedUrl} from '../session.js';
import {nextDocumentRevision} from '../documentFiles.js';
import OfficePreview from './OfficePreview.jsx';
import {ArtifactPreviewBlock} from './artifactComponents.jsx';

export default function DocumentPreviewPanel({artifactId,events,t,font,onRevision}){
  const [data,setData]=useState(null),[error,setError]=useState(''),[retry,setRetry]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();setData(null);setError('');
    (async()=>{try{
      const r=await fetch(`${API}/api/artifacts/${encodeURIComponent(artifactId)}/preview`,{signal:controller.signal});
      if(!r.ok)throw new Error(r.status===404?'This document is no longer available.':`Preview unavailable (HTTP ${r.status}).`);
      const d=await r.json();if(!controller.signal.aborted)setData(d);
    }catch(e){if(!controller.signal.aborted)setError(e.message);}})();
    return()=>controller.abort();
  },[artifactId,retry]);
  useEffect(()=>{const next=nextDocumentRevision(data,events);if(next)onRevision(next);},[data,events,onRevision]);
  if(error)return <div role="alert" style={{color:t.err}}>{error} <button onClick={()=>setRetry(n=>n+1)}>Retry</button></div>;
  if(!data)return <div role="status" style={{color:t.mut}}>Loading document…</div>;
  if(data.preview_type==='missing')return <div role="alert" style={{color:t.err}}>This document file is missing.</div>;
  if(data.preview_type==='office')return <OfficePreview key={data.id} preview={data} t={t} font={font} autoRender fill/>;
  if(data.preview_type==='pdf')return <iframe title={data.filename} src={userScopedUrl(data.download_url||data.url)} style={{width:'100%',height:'100%',minHeight:500,border:0,background:'white'}}/>;
  if(data.preview_type==='image')return <img alt={data.filename} src={userScopedUrl(data.download_url||data.url)} style={{maxWidth:'100%'}}/>;
  return <ArtifactPreviewBlock preview={data} t={t} font={font}/>;
}
