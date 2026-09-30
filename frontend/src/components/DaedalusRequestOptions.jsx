import React,{useEffect,useState} from 'react';
import {API} from '../session.js';

export default function DaedalusRequestOptions({conversationId,currentProjectId,refreshKey,onChange,t,font}){
  const [projects,setProjects]=useState([]),[target,setTarget]=useState(''),[visual,setVisual]=useState('inherit');
  const [enabled,setEnabled]=useState(false),[error,setError]=useState('');
  useEffect(()=>{
    let stopped=false;
    setTarget('');setVisual('inherit');onChange({});
    setEnabled(false);setProjects([]);
    fetch(`${API}/api/settings`).then(r=>r.json()).then(async settings=>{
      if(stopped||!settings.daedalus_v3_enabled)return;
      setEnabled(true);
      const r=await fetch(`${API}/api/coder/projects?conversation_id=${encodeURIComponent(conversationId||'')}`);
      if(!r.ok)throw new Error('Unable to load projects');
      const data=await r.json();if(!stopped){setProjects(data.projects||[]);setError('');}
    })
      .catch(e=>{if(!stopped)setError(e.message);});
    return()=>{stopped=true;};
  },[conversationId,refreshKey]);
  const update=(nextTarget,nextVisual)=>{
    setTarget(nextTarget);setVisual(nextVisual);
    onChange({daedalus_new_project:nextTarget==='new',daedalus_project_id:nextTarget==='new'?'':nextTarget,
      daedalus_visual_review:nextVisual==='inherit'?null:nextVisual==='on'});
  };
  if(!enabled)return null;
  const style={background:t.bgDeep,color:t.text,border:`1px solid ${t.brd}`,borderRadius:6,padding:6,fontFamily:font,fontSize:11};
  return <div style={{display:'flex',gap:8,flexWrap:'wrap',margin:'6px 0',alignItems:'center'}}>
    <label style={{fontSize:11,color:t.mut}}>Project <select aria-label="Daedalus project" style={style} value={target} onChange={e=>update(e.target.value,visual)}>
      <option value="">{currentProjectId?'Continue current project':'Start a new project'}</option>{currentProjectId&&<option value="new">Start a new project</option>}
      {projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
    </select></label>
    <label style={{fontSize:11,color:t.mut}}>AI visual review <select aria-label="AI visual review for this request" style={style} value={visual} onChange={e=>update(target,e.target.value)}>
      <option value="inherit">Use Settings</option><option value="off">Off</option><option value="on">On if supported</option>
    </select></label>
    <span style={{fontSize:10,color:t.mut}}>Browser behavior checks apply to web UI tasks. AI visual review requires a local vision model.</span>
    {error&&<span role="alert">{error}</span>}
  </div>;
}
