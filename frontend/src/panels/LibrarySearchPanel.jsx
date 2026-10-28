import PanelHeader from '../components/PanelHeader.jsx';
import SelectionControl from '../components/SelectionControl.jsx';
import {IC} from '../components/icons.jsx';
import React,{useEffect,useState,useRef} from 'react';
import {apiJson} from '../api.js';
import EmptyState from '../components/EmptyState.jsx';
import Pager from '../components/Pager.jsx';
import {Dots} from '../components/Spinner.jsx';
export default function LibrarySearchPanel({t,inputS,btnS,onNavigate,state,setState,embedded=false,workspaceId='',active=true}){
  const {query,kind,workspace,start,end,offset}=state;
  const update=(key,value)=>setState(p=>({...p,[key]:value,offset:0,scroll:0}));
  const scroll=useRef(null),input=useRef(null), restored=useRef(false);
  const [loading,setLoading]=useState(false),[filters,setFilters]=useState(false);
  const [results,setResults]=useState([]),[workspaces,setWorkspaces]=useState([]),[error,setError]=useState('');
  useEffect(()=>{if(embedded)return;const c=new AbortController();apiJson('/api/workspaces',{signal:c.signal}).then(setWorkspaces).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[embedded]);
  const [started,setStarted]=useState(active);
  useEffect(()=>{if(active){setStarted(true);input.current?.focus({preventScroll:true});}},[active]);
  useEffect(()=>{
    // Hiding the embedded view preserves its results and scroll; returning does not refetch.
    if(!started)return;
    const c=new AbortController();setLoading(true);setError('');
    const timer=setTimeout(async()=>{try{
      const until=end?new Date(Date.parse(end+'T00:00:00Z')+86400000).toISOString().slice(0,10):'';
      const params=new URLSearchParams({query,kind,workspace_id:workspaceId||workspace,start,end:until,offset:String(offset)});
      const data=await apiJson('/api/library/search?'+params,{signal:c.signal});setResults(data.results);setError('');setLoading(false);if(!restored.current){restored.current=true;requestAnimationFrame(()=>{if(scroll.current)scroll.current.scrollTop=state.scroll||0;});}
    }catch(e){if(e.name!=='AbortError'){setError(e.message);setResults([]);setLoading(false);}}},200);
    return()=>{clearTimeout(timer);c.abort();};
  },[query,kind,workspace,workspaceId,start,end,offset,started]);
  const reset=()=>setState({query:'',kind:'',workspace:workspaceId||'',start:'',end:'',offset:0,scroll:0});
  return <section ref={scroll} onScroll={e=>{const top=e.currentTarget.scrollTop;setState(p=>({...p,scroll:top}));}} className={embedded?'hc-embedded-search':''} style={{padding:embedded?0:20,overflowY:'auto',height:'100%',boxSizing:'border-box',color:t.text,minHeight:0}}>
    {!embedded&&<PanelHeader t={t} icon={<IC.Search/>} title="Search saved content" subtitle="Find chats, notes, research and artifacts."/>}
    <div className="hc-workspace-card">
    <div className="hc-toolbar"><input ref={input} aria-label={embedded?'Search this workspace':'Search saved content'} value={query} onChange={e=>update('query',e.target.value)} placeholder="Search chats, notes, reports and artifacts" style={{...inputS,flex:'1 1 240px'}}/>
      <SelectionControl label="Search content type" value={kind} onChange={v=>update('kind',v)} options={[{value:'',label:'All types'},...['chat','note','research','artifact'].map(value=>({value,label:({chat:'Chats',note:'Notes',research:'Reports',artifact:'Artifacts'})[value]}))]}/>
      {!embedded&&<SelectionControl label="Search workspace" value={workspace} onChange={v=>update('workspace',v)} options={[{value:'',label:'All workspaces'},...workspaces.map(w=>({value:w.id,label:w.name}))]}/>}
      <button className="hc-button" aria-expanded={filters} onClick={()=>setFilters(v=>!v)}>Filters{start||end?' •':''}</button>
      <button style={btnS(t.mut)} onClick={reset}>Reset</button>
    </div>
    {filters&&<div className="hc-toolbar" style={{marginTop:12}}><label>From <input aria-label="From date" type="date" value={start} onChange={e=>update('start',e.target.value)} style={{...inputS,width:'auto'}}/></label><label>Through <input aria-label="Through date" type="date" value={end} onChange={e=>update('end',e.target.value)} style={{...inputS,width:'auto'}}/></label></div>}
    {embedded&&<p className="hc-secondary">Searching this workspace only</p>}
    </div>
    {error&&<p role="alert" style={{color:t.err}}>{error}</p>}
    {loading&&<div className="hc-secondary" style={{display:'flex',gap:8,alignItems:'center'}}><Dots/> Searching…</div>}{!loading&&!error&&!results.length&&<EmptyState compact icon={<IC.Search/>} title={query?'No matching saved content':'Search saved content'} hint={query?'Try fewer words or reset the filters.':'Enter a search term to find chats, notes, reports and artifacts.'}/>}
    {!loading&&results.map(result=><button className="hc-workspace-card hc-search-result" key={result.kind+result.source_id} onClick={()=>onNavigate(result)}>
      <strong>{result.title||'Untitled'}</strong><div className="hc-secondary">{result.kind} · {result.created_at}</div>
      <div style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{result.excerpt}</div>
    </button>)}
    <Pager page={Math.floor(offset/50)} pageSize={50} count={results.length} hasMore={results.length>=50} busy={loading} onPage={p=>setState(prev=>({...prev,offset:p*50,scroll:0}))} label="Search result pages"/>
  </section>;
}
