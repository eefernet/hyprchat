import {useCallback,useSyncExternalStore} from 'react';
import {userScopedUrl} from './session.js';
import {applyJobEvent,consumeSseFrames,terminalJobStates,finishedJobStates,resumeJobBody} from './daedalusJobs.js';

const entries=new Map();
const delay=(ms,signal)=>new Promise(resolve=>{const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',done);resolve();};const timer=setTimeout(done,ms);signal.addEventListener('abort',done,{once:true});});
function entryFor(workflow){
  const key=userScopedUrl(`/api/coder/workflows/${workflow.id}`);
  if(!entries.has(key))entries.set(key,{key,job:workflow,snapshot:{job:workflow,connection:'connecting',error:'',busy:false},listeners:new Set(),sequence:workflow.event_sequence||0});
  return entries.get(key);
}
function publish(entry,changes){entry.snapshot={...entry.snapshot,...changes};entry.listeners.forEach(fn=>fn());}
async function watch(entry,signal){
  while(!signal.aborted){
    try{
      const response=await fetch(entry.key,{signal});
      if(!response.ok)throw new Error('Progress connection unavailable');
      const job=await response.json();
      if(signal.aborted)return;
      if((job.event_sequence||0)>=entry.sequence){entry.job=job;entry.sequence=job.event_sequence||0;}
      publish(entry,{job:entry.job,connection:'connected'});
      if(finishedJobStates.has(entry.job.state))return; // nothing can change: a card per finished job polled forever
      if(terminalJobStates.has(entry.job.state)){
        await delay(4000,signal);continue;
      }
      const events=await fetch(userScopedUrl(`/api/coder/workflows/${job.id}/events?after=${entry.sequence}`),{signal});
      if(!events.ok||!events.body)throw new Error('Progress disconnected');
      const reader=events.body.getReader(),decoder=new TextDecoder();let buffer='';
      try{
        while(!signal.aborted){
          const {value,done}=await reader.read();if(done)break;
          buffer+=decoder.decode(value,{stream:true});
          const parsed=consumeSseFrames(buffer);buffer=parsed.remainder;
          for(const event of parsed.events){
            const next=applyJobEvent(entry.job,event,entry.sequence);
            entry.job=next.job;entry.sequence=next.lastSequence;
          }
          publish(entry,{job:entry.job,connection:'connected'});
        }
      }finally{reader.releaseLock();}
    }catch(error){if(signal.aborted)return;publish(entry,{connection:'reconnecting'});}
    await delay(1500,signal);
  }
}
export function useDaedalusJob(workflow){
  const entry=entryFor(workflow);
  const subscribe=useCallback(listener=>{
    entry.listeners.add(listener);
    if(!entry.controller){entry.controller=new AbortController();watch(entry,entry.controller.signal);}
    return ()=>{entry.listeners.delete(listener);if(!entry.listeners.size){entry.controller?.abort();entry.controller=null;}};
  },[entry]);
  const snapshot=useSyncExternalStore(subscribe,()=>entry.snapshot);
  const action=async(name,visual='unchanged',clarification='')=>{
    if(entry.snapshot.busy)return;
    publish(entry,{busy:true,error:''});
    try{
      const response=await fetch(userScopedUrl(`/api/coder/workflows/${workflow.id}/${name}`),{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(name==='resume'?resumeJobBody(entry.job,visual,clarification):{})});
      const job=await response.json();if(!response.ok)throw new Error(job.detail||'Unable to update this job');
      entry.job=job;entry.sequence=Math.max(entry.sequence,job.event_sequence||0);
      publish(entry,{job,connection:'connected'});
      entry.controller?.abort();entry.controller=null;
      // Only while a card is mounted: restarting with no listener left an unstoppable poller behind.
      if(entry.listeners.size){entry.controller=new AbortController();watch(entry,entry.controller.signal);}
    }catch(error){publish(entry,{error:error.message});}finally{publish(entry,{busy:false});}
  };
  return {...snapshot,action};
}
