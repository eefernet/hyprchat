import PanelHeader from '../components/PanelHeader.jsx';
import {IC} from '../components/icons.jsx';
import React,{useEffect,useState,useRef} from 'react';
import {apiJson} from '../api.js';
export default function LibrarySearchPanel({t,inputS,btnS,onNavigate,state,setState}){
  const {query,kind,workspace,start,end,offset}=state;
  const update=(key,value)=>setState(p=>({...p,[key]:typeof value==='function'?value(p[key]):value}));
  const setQuery=v=>update('query',v),setKind=v=>update('kind',v),setWorkspace=v=>update('workspace',v),setStart=v=>update('start',v),setEnd=v=>update('end',v),setOffset=v=>update('offset',v);
  const scroll=useRef(null), restored=useRef(false);
  const [loading,setLoading]=useState(false);
  const [results,setResults]=useState([]),[workspaces,setWorkspaces]=useState([]),[error,setError]=useState('');
  useEffect(()=>{const c=new AbortController();apiJson('/api/workspaces',{signal:c.signal}).then(setWorkspaces).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[]);
  useEffect(()=>{const c=new AbortController();setLoading(true);
    const timer=setTimeout(async()=>{try{
      const until=end?new Date(Date.parse(end+'T00:00:00Z')+86400000).toISOString().slice(0,10):'';
      const params=new URLSearchParams({query,kind,workspace_id:workspace,start,end:until,offset:String(offset)});
      const data=await apiJson('/api/library/search?'+params,{signal:c.signal});setResults(data.results);setError('');setLoading(false);if(!restored.current){restored.current=true;requestAnimationFrame(()=>{if(scroll.current)scroll.current.scrollTop=state.scroll||0;});}
    }catch(e){if(e.name!=='AbortError'){setError(e.message);setLoading(false);}}},200);
    return()=>{clearTimeout(timer);c.abort();};
  },[query,kind,workspace,start,end,offset]);
  return <section ref={scroll} onScroll={e=>update("scroll",e.currentTarget.scrollTop)} style={{padding:20,overflowY:'auto',height:'100%',boxSizing:'border-box',color:t.text}}>
    <PanelHeader t={t} icon={<IC.Search/>} title="Search saved content" subtitle="Find chats, notes, research and artifacts."/>
    <input aria-label="Search saved content" autoFocus value={query} onChange={e=>{setQuery(e.target.value);setOffset(0);}} placeholder="Search chats, notes, research, and artifacts" style={{...inputS,width:'100%'}}/>
    <div style={{display:'flex',gap:8,flexWrap:'wrap',margin:'12px 0'}}>
      <select aria-label="Content type" value={kind} onChange={e=>{setKind(e.target.value);setOffset(0);}} style={{...inputS,flex:'1 1 160px',width:'auto'}}><option value="">All types</option>{['chat','note','research','artifact'].map(k=><option key={k}>{k}</option>)}</select>
      <select aria-label="Workspace" value={workspace} onChange={e=>{setWorkspace(e.target.value);setOffset(0);}} style={{...inputS,flex:'1 1 160px',width:'auto'}}><option value="">All workspaces</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select>
      <label>From <input type="date" value={start} onChange={e=>{setStart(e.target.value);setOffset(0);}} style={{...inputS,flex:'1 1 160px',width:'auto'}}/></label>
      <label>Through <input type="date" value={end} onChange={e=>{setEnd(e.target.value);setOffset(0);}} style={{...inputS,flex:'1 1 160px',width:'auto'}}/></label>
    </div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {loading&&<p role="status">Searching…</p>}{!loading&&!results.length&&<p style={{color:t.dim}}>{query?'No matching saved content.':'Enter a search term.'}</p>}
    {results.map(result=><button key={result.kind+result.source_id} onClick={()=>onNavigate(result)} style={{display:'block',width:'100%',textAlign:'left',padding:14,marginBottom:10,background:t.surface,color:t.text,border:`1px solid ${t.brd}`,borderRadius:8,cursor:'pointer'}}>
      <strong>{result.title||'Untitled'}</strong><div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,margin:'6px 0'}}>{result.kind} · {result.created_at}</div>
      <div style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{result.excerpt}</div>
    </button>)}
    <div style={{display:'flex',gap:8}}><button disabled={!offset} style={btnS(t.acc)} onClick={()=>setOffset(n=>Math.max(0,n-50))}>Previous</button><button disabled={results.length<50} style={btnS(t.acc)} onClick={()=>setOffset(n=>n+50)}>Next</button></div>
  </section>;
}
