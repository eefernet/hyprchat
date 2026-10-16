import useFocusScope from '../useFocusScope.js';
import React,{useEffect,useState} from 'react';
import {createPortal} from 'react-dom';
import {apiJson} from '../api.js';
import OfficePreview from './OfficePreview.jsx';

export default function DocumentRevisionReview({artifact,t,font,onOpen}){
  const [open,setOpen]=useState(false),[revisions,setRevisions]=useState([]),[earlier,setEarlier]=useState(''),[comparison,setComparison]=useState(null),[previews,setPreviews]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const focus=useFocusScope(open,()=>setOpen(false));
  const button={padding:'7px 10px',border:`1px solid ${t.brd}`,borderRadius:6,color:t.text,background:t.surface,cursor:'pointer'};
  useEffect(()=>{const c=new AbortController();setComparison(null);setEarlier('');apiJson(`/api/artifacts/${artifact.id}/revisions`,{signal:c.signal}).then(rows=>{setRevisions(rows);setEarlier(rows.find(r=>r.id!==artifact.id)?.id||'');}).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[artifact.id]);
  useEffect(()=>{if(!open||!earlier)return;const c=new AbortController();setBusy(true);setError('');Promise.all([apiJson(`/api/artifacts/${artifact.id}/compare?earlier_id=${encodeURIComponent(earlier)}`,{signal:c.signal}),... [earlier,artifact.id].map(id=>apiJson(`/api/artifacts/${id}/preview`,{signal:c.signal}))]).then(([d,...p])=>{setComparison(d);setPreviews(p);}).catch(e=>{if(e.name!=='AbortError')setError(e.message);}).finally(()=>{if(!c.signal.aborted)setBusy(false);});return()=>c.abort();},[open,earlier,artifact.id]);
  const restore=async()=>{setBusy(true);try{const d=await apiJson(`/api/artifacts/${artifact.id}/restore`,{method:'POST',body:{earlier_id:earlier,expected_sha256:artifact.sha256}});setOpen(false);onOpen?.(d.id);}catch(e){if(e.name!=='AbortError')setError(e.message);}finally{setBusy(false);}};
  return <><button style={button} onClick={()=>setOpen(true)}>Compare / restore revisions</button>{open&&createPortal(<div ref={focus} role="dialog" aria-modal="true" aria-label="Document revisions" style={{position:'fixed',inset:0,zIndex:250,background:t.bgDeep,color:t.text,padding:16,overflow:'auto'}}>
    <div style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center'}}><strong>Document revisions</strong><select value={earlier} onChange={e=>setEarlier(e.target.value)} style={button}><option value="">Choose an earlier revision</option>{revisions.filter(r=>r.id!==artifact.id).map(r=><option key={r.id} value={r.id}>{r.filename} · {r.created_at}</option>)}</select><button style={button} onClick={()=>setOpen(false)}>Close</button></div>
    {revisions.length<2&&<p>No earlier revisions are recorded.</p>}{error&&<p role="alert" style={{color:t.err}}>{error}</p>}{busy&&<p>Working…</p>}
    {comparison&&<><p>{comparison.note}</p><div style={{display:'flex',gap:16,flexWrap:'wrap'}}>{previews.map((preview,i)=><section key={preview.id} style={{flex:'1 1 300px',minWidth:0}}><h3>{i?'Current':'Earlier'}: {preview.filename}</h3><OfficePreview preview={preview} t={t} font={font}/></section>)}</div><details open><summary>Text changes</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{comparison.diff||'No extracted text changes.'}</pre></details><details><summary>Structure comparison</summary><div style={{display:'flex',flexWrap:'wrap',gap:16}}>{['earlier','current'].map(side=><pre key={side} style={{flex:'1 1 300px',whiteSpace:'pre-wrap',minWidth:0}}>{JSON.stringify(comparison[side].structure,null,2)}</pre>)}</div></details><p>Restoring creates a new revision. Existing files remain available.</p><button disabled={busy} style={button} onClick={restore}>Restore earlier content as a new revision</button></>}
  </div>,document.body)}</>;
}
