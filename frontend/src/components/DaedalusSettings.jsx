import React, {useEffect, useState} from 'react';
import {API} from '../session.js';

const roles=['chat','architect','builder','reviewer','acceptance','qa','fixer','aider','compaction'];
const fields=[
  ['openhands_num_ctx','Daedalus context window','0 inherits the global context. Applies to every coding stage.'],
  ['generation_num_predict','Completion allowance','Tokens reserved for each model response.'],
  ['context_headroom_percent','Context headroom (%)','Space reserved for request overhead.'],
  ['context_compaction_threshold','Compact at (%)','Percentage of the available input budget.'],
  ['daedalus_job_seconds','Execution allowance (seconds)','Continue a larger job from its checkpoint when this allowance ends.'],
  ['daedalus_model_calls','Model calls per allowance','Includes planning, coding, compaction, and Acceptance.'],
  ['daedalus_attempt_turns','Turns per coding attempt','Return to verification after this many model turns.'],
  ['daedalus_command_seconds','Command timeout (seconds)','Maximum duration of an individual verification command.'],
  ['daedalus_browser_step_seconds','Browser interaction timeout (seconds)','Maximum wait for one browser action or assertion before returning feedback for repair.'],
  ['daedalus_upload_mb','Project archive limit (MB)','Maximum compressed upload size.'],
  ['daedalus_extracted_mb','Extracted project limit (MB)','Maximum expanded source size.'],
  ['daedalus_storage_mb','Worker project storage (MB)','Storage allowance for source workspaces and checkpoints.'],
];

export default function DaedalusSettings({t,font,onSaved}){
  const [values,setValues]=useState(null),[message,setMessage]=useState(''),[saving,setSaving]=useState(false);
  const [exclusions,setExclusions]=useState('');
  useEffect(()=>{let stopped=false;fetch(`${API}/api/settings`).then(async r=>{
    if(!r.ok)throw new Error('Unable to load coding settings');
    const data=await r.json();if(!stopped){setValues({...data,daedalus_role_contexts:{...data.daedalus_role_contexts,aider:data.daedalus_role_contexts?.aider||data.aider_num_ctx||0}});setExclusions((data.daedalus_exclude_dirs||[]).join(', '));}
  }).catch(e=>{if(!stopped)setMessage(e.message);});return()=>{stopped=true;};},[]);
  if(!values)return <div role="status">{message||'Loading coding settings…'}</div>;
  const input={background:t.bgDeep,color:t.text,border:`1px solid ${t.brd}`,borderRadius:6,padding:'7px 9px',fontFamily:font,width:'100%',boxSizing:'border-box'};
  const number=(key,value)=>setValues(v=>({...v,[key]:value}));
  const save=async()=>{
    setSaving(true);setMessage('');
    try{
      const body=Object.fromEntries(fields.map(([key])=>[key,Number(values[key])]));
      body.aider_num_ctx=0;
      body.daedalus_v3_enabled=!!values.daedalus_v3_enabled;
      body.daedalus_compaction=values.daedalus_compaction;
      body.daedalus_role_contexts=Object.fromEntries(roles.map(role=>[role,Number(values.daedalus_role_contexts?.[role]||0)]));
      body.helper_contexts=Object.fromEntries(Object.entries(values.helper_contexts||{}).map(([key,value])=>[key,Number(value)]));
      body.daedalus_exclude_dirs=exclusions.split(',').map(value=>value.trim()).filter(Boolean);
      const response=await fetch(`${API}/api/settings`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Invalid coding settings');
      setValues(data);setExclusions((data.daedalus_exclude_dirs||[]).join(', '));onSaved?.(data);
      setMessage(data.pending_context_jobs?.length?'Saved; waiting for worker settings acknowledgement.':'Saved. Active jobs use these values at the next model request.');
    }catch(error){setMessage(error.message);}finally{setSaving(false);}
  };
  return <div style={{display:'flex',flexDirection:'column',gap:14}}>
    <label style={{display:'flex',alignItems:'center',gap:8,color:t.text,fontSize:12}}>
      <input type="checkbox" checked={!!values.daedalus_v3_enabled} onChange={e=>setValues(v=>({...v,daedalus_v3_enabled:e.target.checked}))}/>
      Use the new persistent Daedalus workflow for new jobs (experimental)
    </label>
    <div style={{fontSize:11,color:t.mut}}>This workflow has not passed its local-model quality evaluation. Review generated changes and test coverage before use. Existing workflows retain their original execution path. Context settings are shared across coding stages.</div>
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(210px,1fr))',gap:12}}>
      {fields.map(([key,label,hint])=><label key={key} style={{fontSize:12,color:t.text}}>{label}
        <input aria-label={label} type="number" min={key==='openhands_num_ctx'?0:key.endsWith('percent')||key.endsWith('threshold')?0.01:1} step={key.endsWith('percent')||key.endsWith('threshold')?'any':1} value={values[key]??''} onChange={e=>number(key,e.target.value)} style={{...input,marginTop:5}}/>
        <span style={{display:'block',fontSize:10,color:t.mut,marginTop:4}}>{hint}</span>
      </label>)}
    </div>
    <label style={{fontSize:12,color:t.text}}>Automatic context compaction
      <select aria-label="Daedalus compaction" style={{...input,marginTop:5}} value={values.daedalus_compaction||'inherit'} onChange={e=>setValues(v=>({...v,daedalus_compaction:e.target.value}))}>
        <option value="inherit">Inherit global compaction setting</option><option value="on">Enabled</option><option value="off">Disabled</option>
      </select>
    </label>
    <details><summary style={{cursor:'pointer',fontSize:12,color:t.dim}}>Stage context overrides and effective values</summary>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(170px,1fr))',gap:10,marginTop:10}}>
        {roles.map(role=><label key={role} style={{fontSize:11,color:t.text,textTransform:'capitalize'}}>{role}
          <input aria-label={`${role} context override`} type="number" min="0" step="1" placeholder="Inherit Daedalus" value={values.daedalus_role_contexts?.[role]||''} onChange={e=>setValues(v=>({...v,daedalus_role_contexts:{...v.daedalus_role_contexts,[role]:e.target.value}}))} style={{...input,marginTop:4}}/>
          <span style={{display:'block',color:t.mut,fontSize:10,marginTop:4}}>{values.resolved_contexts?.[role]?.error||`${values.resolved_contexts?.[role]?.num_ctx?.toLocaleString()||'—'} tokens · ${values.resolved_contexts?.[role]?.source||'inherited'}`}</span>
        </label>)}
      </div>
    </details>
    <details><summary style={{cursor:'pointer',fontSize:12,color:t.dim}}>Helper model contexts</summary>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(160px,1fr))',gap:10,marginTop:10}}>
        {Object.entries(values.helper_contexts||{}).map(([key,value])=><label key={key} style={{fontSize:11,color:t.text}}>{key}
          <input aria-label={`${key} helper context`} type="number" min="1" step="1" value={value} onChange={e=>setValues(v=>({...v,helper_contexts:{...v.helper_contexts,[key]:e.target.value}}))} style={input}/>
        </label>)}
      </div>
    </details>
    <label style={{fontSize:12,color:t.text}}>Excluded directory names
      <input aria-label="Excluded directory names" style={{...input,marginTop:5}} value={exclusions} onChange={e=>setExclusions(e.target.value)}/>
    </label>
    <button disabled={saving} onClick={save} style={{...input,cursor:'pointer',color:t.acc,fontWeight:700}}>{saving?'Saving…':'Save coding settings'}</button>
    {message&&<div role="status" style={{fontSize:11,color:t.dim}}>{message}</div>}
  </div>;
}
