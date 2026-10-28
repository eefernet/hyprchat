import {isDocumentEvent} from './documentFiles.js';

export function activityDetail(event) {
  const value=event?.data?.detail;
  if(typeof value!=='string')return value;
  try{return JSON.parse(value);}catch{return value;}
}

const clean=value=>String(value||'').replace(/^[\p{Extended_Pictographic}\u200d\ufe0f\s]+/u,'').trim();
const names={quick_search:'Searching',research:'Searching',deep_research:'Researching',fetch_url:'Reading page',execute_code:'Running code',run_shell:'Running command',read_file:'Reading file',write_file:'Writing file',list_files:'Listing files',generate_image:'Generating image',kb:'Searching knowledge',memory:'Reading memory',download_file:'Preparing file',download_project:'Preparing project',install_package:'Installing package'};
const toolLabel=tool=>names[tool]||String(tool||'Tool').replaceAll('_',' ').replace(/^./,s=>s.toUpperCase());
const identity=data=>data.tool_call_id||data.call_id||data.operation_id||data.run_id||null;

// Events are already ordered by arrival. Merge the saved prefix with the live
// window without counting replayed events twice; never group by category, which
// makes old initialization/thinking events look like the latest activity.
export function chatActivity({events=[],savedEvents=[],live=false,content='',status='',reviewing=false}={}) {
  const seen=new Set(),ordered=[];
  for(const event of [...savedEvents,...events]){
    if(!event||isDocumentEvent(event))continue;
    const stamp=event.timestamp??event.ts;
    const key=stamp==null?event:JSON.stringify([event.type,stamp,event.data]);
    if(seen.has(key))continue;
    seen.add(key);ordered.push(event);
  }
  const entries=[];
  let phase='starting',label='Starting',thought=null,review=null,thoughtBoundary=false;
  const add=(kind,event,extra={})=>{
    const row={id:`${kind}-${entries.length}`,kind,event,state:'running',...extra};
    entries.push(row);return row;
  };
  const update=(row,event,state)=>{
    // A short final snapshot must not replace the fuller available reasoning.
    const old=activityDetail(row.event),next=activityDetail(event);
    row.event=(old?.thinking?.length>(next?.thinking?.length||0)||(!next&&old))?{...event,data:{...event.data,detail:row.event.data.detail}}:event;
    if(state)row.state=state;
  };
  const setPhase=(next,text)=>{phase=next;label=text;};
  for(const event of ordered){
    const {type,data={}}=event,tool=data.tool;
    if(type==='thinking'){
      if(!thought||thought.state!=='running')thought=add('reasoning',event,{label:'Reasoning'});
      else update(thought,event);
      thoughtBoundary=false;
      setPhase('thinking','Thinking');
    }else if(type==='thought_done'){
      const previous=activityDetail(thought?.event)?.thinking||'',next=activityDetail(event)?.thinking||'';
      const sameTrace=previous&&next&&(previous.includes(next)||next.includes(previous));
      if(!thought||(thoughtBoundary&&!sameTrace))thought=add('reasoning',event,{label:'Reasoning'});
      if(phase!=='tool'&&phase!=='searching')thoughtBoundary=false;
      update(thought,event,'complete');
      if(phase==='thinking'||phase==='starting')setPhase('writing','Writing');
    }else if(type==='answer_token'||tool==='generating'){
      if(thought?.state==='running')thought.state='complete';
      setPhase('writing','Writing');
    }else if(tool==='processing'){
      // This is a transport placeholder, not a real tool invocation.
      continue;
    }else if(type==='error'){
      add('error',event,{label:'Error',state:'failed'});setPhase('failed','Failed');
    }else if(['tool_start','tool_progress','tool_status','tool_end','tool_done','tool_error'].includes(type)){
      const terminal=['tool_end','tool_done','tool_error'].includes(type);
      const kind=tool==='refinement'?'review':'tool',callId=identity(data);
      const candidates=entries.filter(e=>e.kind===kind&&e.tool===tool&&(!callId||e.callId===callId));
      let row=[...candidates].reverse().find(e=>e.state==='running');
      if(callId&&!row){
        row=[...entries].reverse().find(e=>e.kind===kind&&e.tool===tool&&!e.callId&&e.state==='running');
        if(row)row.callId=callId;
      }
      if(kind==='review'&&type==='tool_start'){
        if(review?.state==='running')review.state='complete';
        row=null;
      }
      // Identified nested starts are updates; uncorrelated starts are distinct
      // invocations. A later terminal update can enrich an identified result.
      if(type==='tool_start'&&(!callId||!row))row=null;
      if(!row&&terminal&&callId)row=candidates.at(-1);
      if(!row&&terminal&&kind==='review')row=candidates.at(-1);
      if(!row)row=add(kind,event,{tool,callId,label:kind==='review'?`Review ${data.round||1}${data.total?`/${data.total}`:''}`:toolLabel(tool)});
      update(row,event,terminal?(type==='tool_error'?'failed':data.outcome==='retained'?'retained':'complete'):'running');
      if(kind==='review')review=row;
      if(kind==='tool'||type==='tool_start')thoughtBoundary=true;
      if(!terminal)setPhase(kind==='review'?'reviewing':tool?.includes('search')||tool==='research'?'searching':'tool',kind==='review'?row.label.replace('Review ','Reviewing '):row.label);
      else{
        const pending=entries.filter(e=>e.kind==='tool'&&e.state==='running').at(-1);
        setPhase(type==='tool_error'?'failed':pending?'tool':'writing',type==='tool_error'?'Tool failed':pending?.label||'Writing');
      }
    }
  }
  const terminalStatus=String(status||'').toLowerCase();
  const stopped=['stopped','interrupted','canceled','cancelled'].includes(terminalStatus);
  const failed=['failed','error'].includes(terminalStatus)||(!terminalStatus&&phase==='failed');
  if(live&&!stopped&&!failed){
    if(review?.state==='running'||reviewing)setPhase('reviewing',review?.label.replace('Review ','Reviewing ')||'Reviewing');
    else if(phase==='starting'&&content)setPhase('writing','Writing');
  }else{
    for(const row of entries){
      if(row.state!=='running')continue;
      row.state=stopped?'interrupted':failed?'interrupted':row.kind==='review'&&terminalStatus==='completed'?'complete':'unknown';
    }
    setPhase(stopped?'stopped':failed?'failed':'complete',stopped?(terminalStatus==='stopped'?'Stopped':'Interrupted'):failed?'Failed':'Activity');
  }
  const tools=entries.filter(e=>e.kind==='tool').length;
  return {phase,label,entries,tools,active:live&&!stopped&&!failed,visible:live||entries.length>0||stopped||failed};
}

export function activityText(event){return clean(event?.data?.status||event?.data?.label||event?.data?.error||'');}
