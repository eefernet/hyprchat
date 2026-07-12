import React from 'react';

import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import EmptyState from '../components/EmptyState.jsx';

export default function PromptLibraryPanel({
  t,
  btnS,
  cardS,
  inputS,
  prompts,
  setPrompts,
  editPrompt,
  setEditPrompt,
  setInp,
  insertPrompt,
  setPanel,
}){
  const safePrompts=Array.isArray(prompts)?prompts.filter(p=>p&&typeof p==="object"):[];
  const cleanPrompt=p=>({
    id:typeof p.id==="string"&&p.id?p.id:`p-${Date.now()}`,
    title:typeof p.title==="string"&&p.title?p.title:"Untitled Prompt",
    content:typeof p.content==="string"?p.content:"",
    category:typeof p.category==="string"&&p.category?p.category:"General",
    is_system:!!p.is_system,
  });
  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.f1} icon={<IC.Zap/>} title="Prompt Library"
      subtitle="Reusable prompts — insert into any chat or trigger with / in the composer.">
      <button onClick={()=>{const id=`p-${Date.now()}`;setPrompts(p=>[{id,title:"New Prompt",content:"",category:"General",is_system:false},...(Array.isArray(p)?p.filter(x=>x&&typeof x==="object"):[])]);}} style={btnS(t.f1)}><IC.Plus/> New Prompt</button>
    </PanelHeader>
    <div style={{flex:1,overflowY:"auto",padding:20}}>
      {!safePrompts.length&&<EmptyState t={t} icon={<IC.Zap/>} title="No saved prompts yet"
        hint="Save reusable prompts here and insert them into any chat with one click."
        action={<button onClick={()=>{setPrompts([
          {id:`p-${Date.now()}-1`,title:"Explain Simply",content:"Explain this as if I'm 12 years old, using simple language and analogies.",category:"Learning"},
          {id:`p-${Date.now()}-2`,title:"Code Review",content:"Review this code for bugs, performance issues, security vulnerabilities, and suggest improvements.",category:"Coding"},
          {id:`p-${Date.now()}-3`,title:"Summarize",content:"Summarize the key points from the above in bullet points.",category:"Writing"},
          {id:`p-${Date.now()}-4`,title:"Pros & Cons",content:"List the pros and cons of this in a balanced way.",category:"Analysis"},
        ]);}} style={{...btnS(t.f1),marginTop:4,justifyContent:"center"}}>Load Starter Prompts</button>}/>}
      {safePrompts.map(raw=>{
        const p=cleanPrompt(raw);
        const isEditing=editPrompt?.id===p.id;
        return <div key={p.id} style={{...cardS,borderColor:isEditing?`${t.f1}55`:`${t.brd}44`}}>
          {isEditing?<div style={{animation:"fadeIn .2s"}}>
            <div style={{display:"flex",gap:8,marginBottom:8}}>
              <input value={editPrompt.title} onChange={e=>setEditPrompt(ep=>({...ep,title:e.target.value}))} placeholder="Title" style={{...inputS,flex:2,fontWeight:600}}/>
              <input value={editPrompt.category} onChange={e=>setEditPrompt(ep=>({...ep,category:e.target.value}))} placeholder="Category" style={{...inputS,flex:1,fontSize:10}}/>
            </div>
            <label style={{display:"flex",alignItems:"center",gap:6,fontSize:10,color:t.dim,cursor:"pointer",marginBottom:6}}>
              <input type="checkbox" checked={!!editPrompt.is_system} onChange={e=>setEditPrompt(ep=>({...ep,is_system:e.target.checked}))} style={{accentColor:t.warm,cursor:"pointer"}}/>
              Available as System Prompt
            </label>
            <textarea value={editPrompt.content} onChange={e=>setEditPrompt(ep=>({...ep,content:e.target.value}))} rows={5} placeholder="Your prompt text..." style={{...inputS,resize:"vertical",lineHeight:1.5,marginBottom:8}}/>
            <div style={{display:"flex",gap:4,justifyContent:"flex-end"}}>
              <button onClick={()=>{setPrompts(ps=>(Array.isArray(ps)?ps:[]).filter(x=>x&&typeof x==="object").map(x=>x.id===p.id?{...cleanPrompt(x),...editPrompt}:cleanPrompt(x)));setEditPrompt(null);}} style={btnS(t.ok)}>Save</button>
              <button onClick={()=>setEditPrompt(null)} style={btnS(t.mut)}>Cancel</button>
            </div>
          </div>:<div>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:6}}>
              <div style={{flex:1,minWidth:0}}>
                <div style={{fontSize:13,fontWeight:600,color:t.text}}>{p.title}</div>
                <div style={{fontSize:10,color:t.f1,marginTop:1,display:"flex",alignItems:"center",gap:4}}>{p.category||"General"}{p.is_system&&<span style={{fontSize:8,padding:"1px 5px",borderRadius:6,background:`${t.warm}18`,color:t.warm,fontWeight:600}}>System Prompt</span>}</div>
              </div>
              <div style={{display:"flex",gap:4,flexShrink:0}}>
                <button onClick={()=>{setPanel("chat");if(insertPrompt)insertPrompt(p);else setInp(p.content);}} style={{...btnS(t.f1),fontSize:9}}>⚡ Use</button>
                <button onClick={()=>setEditPrompt({id:p.id,title:p.title,content:p.content,category:p.category||"General",is_system:!!p.is_system})} style={btnS(t.mut)}><IC.Pencil/></button>
                <button onClick={()=>setPrompts(ps=>(Array.isArray(ps)?ps:[]).filter(x=>x&&typeof x==="object"&&x.id!==p.id))} style={btnS(t.err)}><IC.Trash/></button>
              </div>
            </div>
            <div style={{fontSize:11,color:t.dim,lineHeight:1.5,maxHeight:80,overflow:"hidden"}}>
              {p.content.slice(0,200)}{p.content.length>200?"...":""}
            </div>
          </div>}
        </div>;
      })}
    </div>
  </div>;
}
