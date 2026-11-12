// Module-level store for the right-hand workbench pane: Daedalus job tabs or a
// live HTML preview. A store rather than props on purpose: CodeCard renders deep
// inside the memoized markdown tree (MemoMD compares opts by reference), so what
// it needs must not travel through md() opts. Same pattern as daedalusJobStore.
import {useEffect,useRef,useSyncExternalStore} from 'react';
import {terminalJobStates} from './daedalusJobs.js';
import {isWebApp} from './daedalusActivity.js';

export const DEBOUNCE_MS=300;
export const DAEDALUS_TABS=['Activity','Plan','Checks','Files','Preview'];
const initial=()=>({open:false,kind:'',job:null,tab:'Activity',html:null,autoOpen:true});
let state=initial();
const listeners=new Set(),autoOpened=new Set();
let opener=null,timer=0,queued=null;

function emit(changes){state={...state,...changes};listeners.forEach(fn=>fn());}
function subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);}
const snapshot=()=>state;
export function useWorkbench(){return useSyncExternalStore(subscribe,snapshot,snapshot);}
export function workbenchState(){return state;}

// kind 'daedalus' needs {job:{id,...}}; kind 'html' needs {html:{key,code,title,streaming}}.
export function openWorkbench({kind,job=null,tab,html=null}={}){
  if(kind!=='daedalus'&&kind!=='html')return;
  if(!state.open&&typeof document!=='undefined')opener=document.activeElement;
  clearTimeout(timer);timer=0;queued=null;
  const sameJob=kind==='daedalus'&&state.kind==='daedalus'&&state.job?.id===job?.id;
  emit({open:true,kind,job:kind==='daedalus'?job:null,html:kind==='html'?html:null,
    tab:tab||(kind==='html'?'Preview':sameJob?state.tab:'Activity')});
}
export function closeWorkbench(){
  clearTimeout(timer);timer=0;queued=null;
  if(!state.open)return;
  emit({open:false,kind:'',job:null,html:null});
  const target=opener;opener=null;
  if(target&&typeof target.focus==='function'&&(typeof document==='undefined'||document.contains(target)))target.focus();
}
export function setWorkbenchTab(tab){if(state.open&&tab&&tab!==state.tab)emit({tab});}
export function configureWorkbench({autoOpen}={}){if(typeof autoOpen==='boolean'&&autoOpen!==state.autoOpen)emit({autoOpen});}
// Once per key, and only while the user allows it; the key is consumed only on a yes.
export function shouldAutoOpen(key){
  if(!state.autoOpen||autoOpened.has(key))return false;
  autoOpened.add(key);return true;
}

function flush(){
  clearTimeout(timer);timer=0;
  const next=queued;queued=null;
  if(next&&state.open&&state.kind==='html'&&state.html?.key===next.key)emit({html:{...state.html,...next}});
}
// While a reply streams, the iframe is rewritten at most every DEBOUNCE_MS with the newest code.
export function updateLivePreview(key,code,{streaming=false,title}={}){
  if(!state.open||state.kind!=='html'||state.html?.key!==key)return;
  const current=queued||state.html;
  if(current.code===code&&current.streaming===streaming&&(title===undefined||current.title===title))return;
  queued={key,code,streaming,...(title!==undefined?{title}:{})};
  if(!streaming){flush();return;}
  if(!timer)timer=setTimeout(flush,DEBOUNCE_MS);
}
// A streaming message is remounted under its saved id once it finalizes; the same block keeps the pane.
const blockOf=key=>String(key||'').slice(String(key||'').lastIndexOf(':')+1);
export function rebindLivePreview(key,code){
  if(!state.open||state.kind!=='html'||!state.html||state.html.key===key)return;
  if(blockOf(state.html.key)!==blockOf(key))return;
  if(state.html.code!==code&&!state.html.streaming&&!queued)return;
  clearTimeout(timer);timer=0;queued=null;
  emit({html:{...state.html,key,code,streaming:false}});
}
export function resetWorkbench(){clearTimeout(timer);timer=0;queued=null;opener=null;autoOpened.clear();state=initial();listeners.forEach(fn=>fn());}

// Open the pane once for a job that turns out to be a web app while it is running
// in this session. Historical jobs loaded with a conversation never open it.
export function useDaedalusAutoOpen(job){
  const seenActive=useRef(false);
  const active=!!job&&!terminalJobStates.has(job.state);
  if(active)seenActive.current=true;
  const web=!!job&&isWebApp(job);
  const hasPreview=!!(job?.artifact||job?.candidate_artifact)||(job?.checks||[]).some(check=>check?.screenshots?.length);
  useEffect(()=>{
    if(!job||!web||!seenActive.current)return;
    if(!shouldAutoOpen(`job:${job.id}`))return;
    openWorkbench({kind:'daedalus',job:{id:job.id,workflow_version:3,state:job.state,event_sequence:job.event_sequence},tab:hasPreview?'Preview':'Activity'});
  },[job?.id,web,active,hasPreview]);
}
