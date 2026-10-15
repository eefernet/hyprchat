import React,{useEffect,useState} from 'react';
import {apiJson} from '../api.js';
export default function ContextInspector({messageId,t,onClose}){
  const [data,setData]=useState(null),[error,setError]=useState('');
  useEffect(()=>{const controller=new AbortController();setData(null);setError('');
    apiJson(`/api/messages/${messageId}/context`,{signal:controller.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);});
    return()=>controller.abort();},[messageId]);
  return <div role="dialog" aria-modal="true" aria-label="Response context" style={{position:'fixed',inset:0,zIndex:500,background:'#0008'}} onClick={onClose}>
    <section onClick={e=>e.stopPropagation()} style={{position:'absolute',right:0,top:0,bottom:0,width:'min(620px,100%)',overflowY:'auto',background:t.bgDeep,color:t.text,padding:20,boxSizing:'border-box'}}>
      <button aria-label="Close context" onClick={onClose} style={{float:'right'}}>Close</button><h2>Response context</h2>
      {error&&<p role="alert">{error}</p>}{!data&&!error&&<p>Loading…</p>}
      {data&&!data.available&&<p>Context was not recorded for this response.</p>}
      {(data?.rounds||[]).map(round=><article key={round.sequence} style={{borderTop:`1px solid ${t.brd}`,paddingTop:12}}>
        <h3>Call {round.sequence} · {round.model}</h3>
        <p>{round.provider} · {round.message_count} messages · {round.context_chars} characters · {round.truncated_chars} characters removed</p>
        <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(round.settings,null,2)}</pre>
        {Object.entries(round.memories||{}).map(([name,memory])=><details key={name}><summary>{name} memories ({memory.ids.length})</summary><p>{memory.ids.join(', ')||'None selected'}</p><pre style={{whiteSpace:'pre-wrap'}}>{memory.selected_context}</pre></details>)}
        <details><summary>Retrieved passages</summary><pre style={{whiteSpace:'pre-wrap'}}>{round.retrieved_context||'None recorded'}</pre></details>
        <details><summary>Search sources</summary>{round.search_sources.map((source,i)=><p key={i}>{source.title}<br/>{source.url}</p>)}</details>
        <p>Available tools: {round.available_tools.join(', ')||'None'}</p>
        <p>Tool calls: {(round.executed_tools||[]).join(', ')||'None recorded'}</p>
        <p style={{fontSize:11,color:t.dim}}>{round.limits_note}</p>
      </article>)}
    </section>
  </div>;
}
