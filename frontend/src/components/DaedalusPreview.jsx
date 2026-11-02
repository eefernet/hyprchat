import React,{useEffect,useRef,useState} from 'react';
import {API,userScopedUrl} from '../session.js';

export default function DaedalusPreview({job,button}){
  const profiles=job.execution_profile?.profiles||job.verification?.profiles||[];
  const launchProfile=profiles.find(p=>p.launch);
  const quote=value=>"'"+value.replaceAll("'","'\\''")+"'";
  const suggested=launchProfile?(launchProfile.cwd&&launchProfile.cwd!=='.'?`cd ${quote(launchProfile.cwd)} && `:'')+launchProfile.launch.replaceAll('{port}','8000'):'';
  const desktop=job.verification_summary?.unverified_interfaces?.includes('desktop');
  const [kind,setKind]=useState(desktop?'desktop':'web'),[command,setCommand]=useState(suggested),[toolkit,setToolkit]=useState('');
  const [session,setSession]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [url,setUrl]=useState('http://127.0.0.1:8000');
  const canvas=useRef(null),connection=useRef(null),current=useRef(null);
  const base=`/api/coder/workflows/${job.id}/previews`;
  async function close(){
    connection.current?.disconnect(); connection.current=null;
    const previous=current.current; current.current=null; setSession(null);
    if(previous)await fetch(API+`${base}/${previous.id}`,{method:'DELETE'}).catch(()=>{});
  }
  useEffect(()=>{
    const changed=()=>{connection.current?.disconnect();connection.current=null;current.current=null;setSession(null);};
    window.addEventListener('hyprchat-profile-changing',changed);
    return()=>{window.removeEventListener('hyprchat-profile-changing',changed);connection.current?.disconnect();
      if(current.current)fetch(API+`${base}/${current.current.id}`,{method:'DELETE',keepalive:true}).catch(()=>{});
      current.current=null;connection.current=null;setSession(null);};
  },[base]);
  useEffect(()=>{
    if(!session||session.status!=='starting')return;
    let cancelled=false;
    const timer=setInterval(async()=>{
      try{const response=await fetch(API+`${base}/${session.id}`);const next=await response.json();
        if(!response.ok)throw Error(next.detail||'Preview unavailable');
        if(!cancelled){current.current=next;setSession(next);if(next.error)setError(next.error);}
      }catch(e){if(!cancelled){setError(e.message);setSession(null);}}
    },1500);
    return()=>{cancelled=true;clearInterval(timer);};
  },[base,session?.id,session?.status]);
  useEffect(()=>{
    if(session?.status!=='running'||!canvas.current)return;
    let cancelled=false,rfb;
    import('@novnc/novnc').then(({default:RFB})=>{
      if(cancelled)return;
      const address=new URL(userScopedUrl(`${base}/${session.id}/stream`),window.location.href);
      address.protocol=address.protocol==='https:'?'wss:':'ws:';
      rfb=new RFB(canvas.current,address.href);rfb.scaleViewport=true;rfb.resizeSession=false;connection.current=rfb;
      rfb.addEventListener('disconnect',()=>{if(!cancelled)setError('Preview disconnected. Relaunch it to reconnect.');});
    }).catch(e=>setError(e.message));
    return()=>{cancelled=true;rfb?.disconnect();};
  },[base,session?.id,session?.status]);
  async function launch(){
    setBusy(true);setError('');
    try{await close();const response=await fetch(API+base,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({revision:job.revision_id,kind,command,url,toolkit})});
      const next=await response.json();if(!response.ok)throw Error(next.detail||'Could not launch preview');
      current.current=next;setSession(next);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <section><h2>Application preview</h2>
    <p>Run this delivered revision in a private session. Preview changes are temporary. Sessions close after 15 minutes.</p>
    <label>Application <select aria-label="Application type" value={kind} onChange={e=>setKind(e.target.value)} style={button}>
      <option value="web">Web application</option><option value="desktop">Linux desktop</option></select></label>
    <label style={{display:'block',marginTop:12}}>Launch command <input value={command} onChange={e=>setCommand(e.target.value)} style={{...button,width:'100%',boxSizing:'border-box'}} placeholder={kind==='web'?'npm run dev -- --host 127.0.0.1 --port 8000':'python3 app.py'}/></label>
    {kind==='web'?<label>Application URL <input value={url} onChange={e=>setUrl(e.target.value)} style={button}/></label>:
      <label>Desktop toolkit <select value={toolkit} onChange={e=>setToolkit(e.target.value)} style={button}>
        <option value="">Qt / GTK</option><option value="swing">Java Swing</option></select></label>}
    <p><button style={button} disabled={busy||!command.trim()} onClick={launch}>{busy?'Launching…':'Launch preview'}</button>
      {session&&<button style={button} onClick={close}>Close preview</button>}</p>
    {session&&<p role="status">{session.status==='starting'?'Preparing application…':session.status==='running'?'Preview running':`Preview ${session.status}`}</p>}
    {error&&<p role="alert">{error}</p>}
    <div ref={canvas} aria-label="Interactive application preview" style={{width:'100%',height:session?.status==='running'?'min(70vh, 800px)':0,overflow:'hidden',background:'#111'}}/>
  </section>;
}
