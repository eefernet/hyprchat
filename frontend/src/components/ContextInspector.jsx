import useFocusScope from '../useFocusScope.js';
import React,{useEffect,useState} from 'react';
import {apiJson} from '../api.js';
export default function ContextInspector({messageId,t,onClose,onRestore}){
  const focus=useFocusScope(true,onClose);
  const [data,setData]=useState(null),[error,setError]=useState('');
  const [reviews,setReviews]=useState(null),[restoring,setRestoring]=useState(false);
  useEffect(()=>{const controller=new AbortController();setData(null);setError('');
    apiJson(`/api/messages/${messageId}/context`,{signal:controller.signal}).then(setData).catch(e=>{if(e.name!=='AbortError')setError(e.message);});
    setReviews(null);
    apiJson(`/api/messages/${messageId}/reviews`,{signal:controller.signal}).then(setReviews).catch(e=>{if(e.name!=='AbortError')setError(e.message);});
    return()=>controller.abort();},[messageId]);
  const restore=async(index)=>{setRestoring(true);try{
    const saved=await apiJson(`/api/messages/${messageId}/reviews/${index}/restore`,{method:'POST'});
    onRestore?.(messageId,saved);onClose();
  }catch(e){if(e.name!=='AbortError')setError(e.message);}finally{setRestoring(false);}};
  return <div ref={focus} role="dialog" aria-modal="true" aria-label="Response context" style={{position:'fixed',inset:0,zIndex:500,background:'#0008'}} onClick={onClose}>
    <section onClick={e=>e.stopPropagation()} style={{position:'absolute',right:0,top:0,bottom:0,width:'min(620px,100%)',overflowY:'auto',background:t.bgDeep,color:t.text,padding:20,boxSizing:'border-box'}}>
      <button aria-label="Close context" onClick={onClose} style={{float:'right'}}>Close</button><h2>Response context</h2>
      {error&&<p role="alert">{error}</p>}{!data&&!error&&<p>Loading…</p>}
      {data&&!data.available&&<p>Context was not recorded for this response.</p>}
      {!!reviews?.revisions?.length&&<details><summary>Answer review history ({reviews.revisions.length})</summary>
        {reviews.revisions.map((revision,index)=><details key={index}><summary>{index===0?'Initial answer':`Review ${index}`}</summary>
          <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{revision.content}</pre>
          <button disabled={restoring||reviews.in_progress} onClick={()=>restore(index)}>Restore this answer</button>
        </details>)}
      </details>}
      {(data?.rounds||[]).map(round=><article key={round.sequence} style={{borderTop:`1px solid ${t.brd}`,paddingTop:12}}>
        <h3>Call {round.sequence} · {round.model}</h3>
        {round.effort&&<p>{round.effort.mode==='native'?'Native reasoning':'Self-review'} · {round.effort.mode==='native'?String(round.effort.thinking):`${round.effort.review_rounds} passes`} · {round.effort.source}<br/>{round.effort.notice}</p>}
        {round.usage&&<p>{round.usage.prompt_tokens} input tokens · {round.usage.gen_tokens} output tokens · {round.usage.elapsed_seconds}s</p>}
        <p>{round.provider} · {round.message_count} messages · {round.context_chars} characters · {round.truncated_chars} characters removed</p>
        <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(round.settings,null,2)}</pre>
        {Object.entries(round.memories||{}).map(([name,memory])=><details key={name}><summary>{name} memories ({memory.ids.length})</summary><p>{memory.ids.join(', ')||'None selected'}</p><pre style={{whiteSpace:'pre-wrap'}}>{memory.selected_context}</pre></details>)}
        <details><summary>Retrieved passages</summary><pre style={{whiteSpace:'pre-wrap'}}>{round.retrieved_context||'None recorded'}</pre></details>
        <details><summary>Search sources</summary>{round.search_sources.map((source,i)=><p key={i}>{source.title}<br/>{source.url}</p>)}</details>
        <p>Available tools: {round.available_tools.join(', ')||'None'}</p>
        <p>Tool calls: {(round.executed_tools||[]).join(', ')||'None recorded'}</p>
        <p style={{fontSize:"var(--hc-text-secondary)",color:t.dim}}>{round.limits_note}</p>
      </article>)}
    </section>
  </div>;
}
