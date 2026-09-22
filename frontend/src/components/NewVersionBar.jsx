import React,{useEffect,useRef,useState} from 'react';
import {checkForNewVersion} from '../versionCheck.js';

// Shown when the server is serving a newer frontend build than this tab is running.
export default function NewVersionBar(){
  const [stale,setStale]=useState(false),[dismissed,setDismissed]=useState(false),last=useRef(0);
  useEffect(()=>{
    const check=async()=>{
      if(Date.now()-last.current<5*60*1000)return;
      last.current=Date.now();
      if(await checkForNewVersion())setStale(true);
    };
    const onFocus=()=>{if(document.visibilityState!=='hidden')check();};
    window.addEventListener('focus',onFocus);document.addEventListener('visibilitychange',onFocus);
    const timer=setInterval(check,15*60*1000);
    return()=>{window.removeEventListener('focus',onFocus);document.removeEventListener('visibilitychange',onFocus);clearInterval(timer);};
  },[]);
  if(!stale||dismissed)return null;
  const button={background:'transparent',color:'inherit',border:'1px solid currentColor',borderRadius:6,padding:'4px 10px',cursor:'pointer',font:'inherit'};
  return <div role="status" style={{position:'fixed',left:'50%',bottom:'calc(16px + env(safe-area-inset-bottom, 0px))',transform:'translateX(-50%)',zIndex:9999,
    display:'flex',gap:10,alignItems:'center',flexWrap:'wrap',justifyContent:'center',maxWidth:'calc(100vw - 32px)',padding:'8px 14px',borderRadius:10,
    background:'var(--bg, #10222c)',color:'var(--fg, #e6edf3)',border:'1px solid var(--acc, #5ab0ff)',boxShadow:'0 6px 24px rgba(0,0,0,.35)',fontSize:13}}>
    <span>A new version of HyprChat is available.</span>
    <button style={button} onClick={()=>window.location.reload()}>Reload</button>
    <button style={button} onClick={()=>setDismissed(true)} aria-label="Dismiss new version notice">Later</button>
  </div>;
}
