import React,{useState} from 'react';
import {API} from '../session.js';

export default function DocumentSettings({enabled,onChange,t,font}){
  const [busy,setBusy]=useState(false),[message,setMessage]=useState("");
  const toggle=async()=>{
    setBusy(true);setMessage("");
    try{
      if(!enabled){
        const health=await fetch(`${API}/api/documents/health`).then(r=>r.json());
        if(!health.ready)throw new Error(health.error||`Document worker needs setup${health.missing?.length?`: ${health.missing.join(", ")}`:""}.`);
      }
      const r=await fetch(`${API}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({document_tools_enabled:!enabled})});
      const d=await r.json();if(!r.ok)throw new Error(d.detail||"Setting could not be saved");
      onChange(d.document_tools_enabled===true);
    }catch(e){setMessage(e.message);}finally{setBusy(false);}
  };
  return <div style={{padding:16,border:`1px solid ${t.brd}44`,borderRadius:12,marginBottom:16,fontFamily:font}}>
    <label style={{display:"flex",gap:10,alignItems:"center",color:t.text,fontWeight:700}}><input type="checkbox" checked={enabled} disabled={busy} onChange={toggle}/> Documents</label>
    <p style={{fontSize:12,color:t.mut}}>Read, compose, edit and export uploaded Word, PowerPoint and Excel files. Originals are retained; edits create revisions.</p>
    {message&&<div role="alert" style={{fontSize:12,color:t.err}}>{message}</div>}
  </div>;
}
