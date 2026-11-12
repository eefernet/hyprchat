import React,{useCallback,useEffect,useRef,useState} from 'react';
import {API} from '../session.js';
import {useDaedalusJob} from '../daedalusJobStore.js';
import {finishedJobStates} from '../daedalusJobs.js';

// A delivery or an Accept moves a project's working/accepted pointers on the server; these job fields move with them.
function jobSignature(job){
  return [job?.state,job?.revision_id,job?.accepted_by,job?.artifact?.id,job?.candidate_artifact?.id].map(value=>value||'').join(':');
}

// Completed jobs never change. The cap bounds polling: a watched parked job whose card is off screen polls every 4 s.
// An Accept past the cap still reaches the composer through the store's 'daedalus-project-changed' event.
function watchedJobs(jobs){
  return (jobs||[]).filter(job=>job?.id&&job.workflow_version===3&&!finishedJobStates.has(job.state)).slice(0,3);
}

// Only a project whose accepted and latest versions differ can start from the accepted one.
function versionsDiffer(projects,id){
  const project=(projects||[]).find(p=>p.id===id);
  return !!project?.accepted_revision_id&&!!project?.working_revision_id&&project.accepted_revision_id!==project.working_revision_id;
}

// A conversation or upload change resets the choices; a job change only refetches the list, so it keeps what the user picked.
function useRequestOptions({conversationId,currentProjectId,refreshKey,jobIds,jobChanges,onChange}){
  const [projects,setProjects]=useState([]),[activeProjectId,setActiveProjectId]=useState(''),[target,setTarget]=useState(''),[visual,setVisual]=useState('inherit'),[source,setSource]=useState('working');
  const [enabled,setEnabled]=useState(false),[error,setError]=useState('');
  const request=useRef(0);
  // Only the newest request applies, so a slow earlier response never overwrites a later one.
  const load=useCallback(()=>{
    const id=++request.current,latest=()=>id===request.current;
    return fetch(`${API}/api/coder/projects?conversation_id=${encodeURIComponent(conversationId||'')}`)
      .then(r=>{if(!r.ok)throw new Error('Unable to load projects');return r.json();})
      .then(data=>{if(latest()){setProjects(data.projects||[]);setActiveProjectId(data.active_project_id||'');setError('');}})
      .catch(e=>{if(latest())setError(e.message);});
  },[conversationId]);
  useEffect(()=>{
    let stopped=false;request.current++;
    setTarget('');setVisual('inherit');setSource('working');onChange({});
    setEnabled(false);setProjects([]);setActiveProjectId('');
    fetch(`${API}/api/settings`).then(r=>r.json()).then(settings=>{
      if(stopped||!settings.daedalus_v3_enabled)return;
      setEnabled(true);return load();
    })
      .catch(e=>{if(!stopped)setError(e.message);});
    return()=>{stopped=true;};
  },[conversationId,refreshKey]);
  // A new job, a delivery or an Accept: the project row and its pointers may have moved.
  useEffect(()=>{
    if(!enabled)return;
    const timer=setTimeout(load,250);return()=>clearTimeout(timer);
  },[jobIds,jobChanges,currentProjectId]);
  // An Accept from any card, watched or not (e.g. an older parked candidate past the watch cap), moves the accepted pointer.
  useEffect(()=>{
    if(!enabled)return;
    const changed=()=>load();window.addEventListener('daedalus-project-changed',changed);
    return()=>window.removeEventListener('daedalus-project-changed',changed);
  },[enabled,load]);
  // The server's active project is fresher than the conversation row loaded with the chat list.
  const current=activeProjectId||currentProjectId||'';
  const differs=id=>versionsDiffer(projects,id||current);
  const update=(nextTarget,nextVisual,nextSource=source)=>{
    setTarget(nextTarget);setVisual(nextVisual);setSource(nextSource);
    onChange({daedalus_new_project:nextTarget==='new',daedalus_project_id:nextTarget==='new'?'':nextTarget,
      daedalus_visual_review:nextVisual==='inherit'?null:nextVisual==='on',
      daedalus_source_pointer:nextTarget!=='new'&&nextSource==='accepted'&&differs(nextTarget)?'accepted':''});
  };
  // A refetch can make the two versions equal again: a hidden choice must not keep sending 'accepted'.
  useEffect(()=>{if(source==='accepted'&&(target==='new'||!differs(target)))update(target,visual,'working');},[projects,current]);
  return {enabled,error,projects,current,target,visual,source,update,versions:target!=='new'&&differs(target)};
}

// Shares the job card's live store entry, so a delivery or an Accept is seen without a conversation refresh.
function JobWatch({workflow,onChange}){
  const signature=jobSignature(useDaedalusJob(workflow).job),last=useRef(signature);
  useEffect(()=>{if(last.current!==signature){last.current=signature;onChange();}},[signature,onChange]);
  return null;
}

export default function DaedalusRequestOptions({conversationId,currentProjectId,refreshKey,jobs,onChange,t,font}){
  const [jobChanges,setJobChanges]=useState(0),jobChanged=useCallback(()=>setJobChanges(n=>n+1),[]);
  const jobIds=(jobs||[]).filter(job=>job?.workflow_version===3).map(job=>job.id).join('|');
  const {enabled,error,projects,current,target,visual,source,update,versions}=useRequestOptions({conversationId,currentProjectId,refreshKey,jobIds,jobChanges,onChange});
  if(!enabled)return null;
  const style={background:t.bgDeep,color:t.text,border:`1px solid ${t.brd}`,borderRadius:"var(--hc-radius)",padding:6,fontFamily:font,fontSize:"var(--hc-text-secondary)"};
  return <div style={{display:'flex',gap:8,flexWrap:'wrap',margin:'6px 0',alignItems:'center'}}>
    {watchedJobs(jobs).map(job=><JobWatch key={job.id} workflow={job} onChange={jobChanged}/>)}
    <label style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>Project <select aria-label="Daedalus project" style={style} value={target} onChange={e=>update(e.target.value,visual,'working')}>
      <option value="">{current?'Continue current project':'Start a new project'}</option>{current&&<option value="new">Start a new project</option>}
      {projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
    </select></label>
    {/* Edits start from the latest build; offer the accepted version only when the two differ. */}
    {versions&&<label style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>Start from <select aria-label="Version to edit" style={style} value={source} onChange={e=>update(target,visual,e.target.value)}>
      <option value="working">Latest build</option><option value="accepted">Accepted version</option>
    </select></label>}
    <label style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>AI visual review <select aria-label="AI visual review for this request" style={style} value={visual} onChange={e=>update(target,e.target.value)}>
      <option value="inherit">Use Settings</option><option value="off">Off</option><option value="on">On if supported</option>
    </select></label>
    <span style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>Browser behavior checks apply to web UI tasks. AI visual review requires a local vision model.</span>
    {error&&<span role="alert">{error}</span>}
  </div>;
}
