import React,{useEffect,useRef,useState} from 'react';
import {API,userScopedUrl} from '../session.js';

const previewJobs=new Map();

export default function OfficePreview({preview,t,font,autoRender=false,fill=false}){
  const [data,setData]=useState(preview),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const [sheet,setSheet]=useState(""),[range,setRange]=useState("A1:H30"),[format,setFormat]=useState("pdf");
  const [exports,setExports]=useState([]),[runId,setRunId]=useState(""),[stopping,setStopping]=useState(false);
  const alive=useRef(true),autoAttempted=useRef(false);
  useEffect(()=>{alive.current=true;setData(preview);return()=>{alive.current=false;};},[preview]);
  const request=async(url,options)=>{
    const r=await fetch(`${API}${url}`,options),d=await r.json();
    if(!r.ok)throw new Error(d.detail||d.error||`HTTP ${r.status}`);return d;
  };
  const run=async(action)=>{
    setBusy(true);setStopping(false);setError("");
    try{
      const start=()=>request("/api/documents/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,artifact_id:preview.id,conversation_id:preview.conversation_id,format,sheet,range})});
      if(action==="preview"&&!previewJobs.has(preview.id))previewJobs.set(preview.id,start().catch(e=>{previewJobs.delete(preview.id);throw e;}));
      const job=await (action==="preview"?previewJobs.get(preview.id):start());
      if(!alive.current)return;
      setRunId(job.run_id);
      while(alive.current){
        const state=await request(`/api/runs/${encodeURIComponent(job.run_id)}`);
        if(alive.current)setStopping(state.status==="cancelling");
        if(["succeeded","failed","cancelled"].includes(state.status)){
          if(action==="preview")previewJobs.delete(preview.id);
          const result=state.result_envelope||{};
          if(state.status!=="succeeded")throw new Error(result.error||result.summary||state.status);
          const updated=await request(`/api/artifacts/${encodeURIComponent(preview.id)}/preview`);
          if(alive.current){
            setData({...updated,inspection:result.report?.inspection||result.report?.items&&result.report||updated.inspection});
            setExports(result.artifacts||[]);
          }
          break;
        }
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
    }catch(e){if(action==="preview")previewJobs.delete(preview.id);if(alive.current)setError(e.message);}finally{if(alive.current){setBusy(false);setRunId("");}}
  };
  useEffect(()=>{if(autoRender&&!preview.render_url&&!autoAttempted.current){autoAttempted.current=true;run("preview");}},[autoRender,preview.id]);
  const inspection=data.inspection||{},isSheet=preview.kind==="spreadsheet";
  const input={background:t.bg,color:t.text,border:`1px solid ${t.brd}55`,borderRadius:6,padding:6,fontFamily:font,fontSize:11};
  return <div style={{display:"grid",gap:10}}>
    <div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
      <button style={input} disabled={busy} onClick={()=>run("preview")}>{busy?(stopping?"Stopping…":"Processing…"):"Render preview"}</button>
      <select aria-label="Export format" value={format} onChange={e=>setFormat(e.target.value)} style={input}>
        {(isSheet?["pdf","csv"]:["pdf","txt","md"]).map(f=><option key={f} value={f}>{f.toUpperCase()}</option>)}
      </select>
      <button style={input} disabled={busy} onClick={()=>run("convert")}>Export</button>
      {busy&&runId&&<button style={input} onClick={()=>request(`/api/runs/${encodeURIComponent(runId)}/cancel`,{method:"POST"}).then(d=>{if(alive.current){setStopping(d.status==="cancelling");setError(d.error||"");}}).catch(e=>{if(alive.current)setError(e.message);})}>{stopping?"Retry Stop":"Stop"}</button>}
    </div>
    {isSheet&&<div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
      <input style={{...input,width:130}} list={`sheets-${preview.id}`} aria-label="Worksheet name" placeholder="Worksheet name" value={sheet} onChange={e=>setSheet(e.target.value)}/>
      <datalist id={`sheets-${preview.id}`}>{(inspection.sheets||[]).map(s=><option key={s.name} value={s.name}/>)}</datalist>
      <input style={{...input,width:90}} aria-label="Cell range" value={range} onChange={e=>setRange(e.target.value)}/>
      <button style={input} disabled={busy} onClick={()=>run("read")}>Read range</button>
    </div>}
    {error&&<div role="alert" style={{fontSize:12,color:t.err}}>{error}</div>}
    {(data.warnings||[]).map((w,i)=><div key={i} style={{fontSize:11,color:t.mut}}>{w}</div>)}
    {exports.map(a=><a key={a.artifact_id} href={userScopedUrl(a.url)} target="_blank" rel="noopener noreferrer" style={{color:t.acc,fontSize:12}}>Download {a.filename}</a>)}
    {data.render_url?<><div style={{fontSize:11,color:t.mut}}>LibreOffice preview</div><iframe title={`${preview.filename} preview`} src={userScopedUrl(data.render_url)} style={{width:"100%",height:fill?"calc(100vh - 270px)":500,minHeight:400,border:`1px solid ${t.brd}44`,background:"white"}}/></>:<div style={{fontSize:12,color:t.mut}}>Render a preview to inspect the layout. The original remains available to download.</div>}
    {isSheet&&inspection.items?.length>0&&<div style={{overflow:"auto",maxHeight:340}}><table style={{fontSize:11,borderCollapse:"collapse",width:"100%"}}><thead><tr>{["Sheet","Cell","Formula","Cached value / text"].map(h=><th key={h} style={{padding:6,textAlign:"left",color:t.mut}}>{h}</th>)}</tr></thead><tbody>{inspection.items.map((item,i)=><tr key={i}>{[item.sheet,item.cell,item.formula,item.value].map((v,j)=><td key={j} style={{padding:6,borderTop:`1px solid ${t.brd}33`,color:t.text,whiteSpace:"pre-wrap"}}>{v??"—"}</td>)}</tr>)}</tbody></table>{inspection.next_offset!=null&&<p style={{color:t.mut}}>More cells exist; narrow the range to inspect them.</p>}</div>}
  </div>;
}
