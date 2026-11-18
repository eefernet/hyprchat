import React,{useEffect,useRef,useState} from 'react';
import {IC} from './icons.jsx';
import {Spinner} from './Spinner.jsx';
import {canSaveTo,savePreview} from '../previewSave.js';
import './htmlPreview.css';

// The one header row every preview in the side pane uses: status dot, title, the preview's own
// controls (children), Save, Download, Close. Under 400px of pane width the controls wrap to a second row.
export default function PreviewBar({title,status,live=false,children,onClose,closeLabel='Close preview'}){
  return <div className="hp-bar">
    <span className={`hp-dot${live?' hp-dot-live':''}`} role="img" aria-label={status} title={status}/>
    <span className="hp-title" title={title}>{title}</span>
    <div className="hp-tools">{children}</div>
    {onClose&&<button type="button" className="hp-icon hp-close" onClick={onClose} aria-label={closeLabel} title={closeLabel}><IC.X/></button>}
  </div>;
}

// Stores what is shown as a file of the chat. `content` is read when the button is pressed, so a page
// still being written is saved as it is at that moment. After saving, the button opens the file.
export function SaveButton({conversationId,filename,kind,content,disabled=false,onOpenArtifact}){
  const [state,setState]=useState({phase:'idle'});
  const alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  // Different content is a different file: forget the last result.
  useEffect(()=>{setState({phase:'idle'});},[content,filename,conversationId]);
  const allowed=canSaveTo(conversationId);
  const save=async()=>{
    if(state.phase==='saved'&&state.artifact?.id){onOpenArtifact?.(state.artifact.id);return;}
    if(state.phase==='saving')return;
    setState({phase:'saving'});
    try{
      const result=await savePreview({conversationId,filename,content,kind});
      if(alive.current)setState({phase:'saved',artifact:result.artifact,already:!!result.already_saved});
    }catch(error){if(alive.current)setState({phase:'failed',message:error.message||String(error)});}
  };
  const tip=!allowed?'A temporary chat has no files to save to'
    :state.phase==='saved'?`${state.already?'Already saved':'Saved'} to this chat's files${onOpenArtifact?'. Click to open it.':''}`
    :state.phase==='failed'?`Not saved: ${state.message}. Click to try again.`
    :"Save as a file of this chat";
  return <button type="button" className={`hp-save${state.phase==='saved'?' is-saved':''}${state.phase==='failed'?' is-failed':''}`} onClick={save}
    disabled={!allowed||disabled||!String(content||'').trim()} aria-label={tip} title={tip}>
    {state.phase==='saving'?<Spinner size={11}/>:state.phase==='saved'?<span className="hp-check"><IC.Check/></span>:<IC.HardDrive/>}
    <span>{state.phase==='saved'?'Saved':state.phase==='failed'?'Retry':'Save'}</span>
    <span className="hc-sr-only" role="status">{state.phase==='saved'?'Saved':state.phase==='failed'?'Not saved':''}</span>
  </button>;
}
