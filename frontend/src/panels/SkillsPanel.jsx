// Skills: the procedures HyprChat has saved from its own work. The library, the review queue for
// anything that had to wait for the owner, and the switches that control learning.
import React,{useState,useEffect,useCallback,useRef} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtUtcToLocal } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import EmptyState from '../components/EmptyState.jsx';
import { SkeletonCard } from '../components/Skeleton.jsx';
import useIsMobile from '../useIsMobile.js';
import { TABS, ORIGIN_LABELS, STATUS_LABELS, EMPTY_SKILL, skillsForTab, reviewCount, needsReview, usageLabel, heldLabel, lineDiff, diffStats, slugify } from '../skills.js';

const MONO={fontFamily:"ui-monospace, SFMono-Regular, Menlo, monospace",fontSize:"var(--hc-text-secondary)"};
const LABEL={display:"block",fontSize:"var(--hc-text-secondary)",marginBottom:10};

function Chip({tone,children}){return <span className="hc-chip is-status" style={{'--chip-c':tone}}>{children}</span>;}

function Diff({t,before,after}){
  const rows=lineDiff(before,after),stats=diffStats(rows);
  return <div>
    <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:4}}>{stats.added} line{stats.added===1?"":"s"} added, {stats.removed} removed</div>
    <pre style={{...MONO,margin:0,maxHeight:320,overflow:"auto",border:`1px solid ${t.brd}`,borderRadius:8,padding:"8px 0",whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>
      {rows.map((row,i)=><div key={i} style={{padding:"0 10px",background:row.type==="add"?`${t.ok}22`:row.type==="del"?`${t.err}22`:"transparent",color:row.type==="same"?t.dim:t.text}}>{row.type==="add"?"+ ":row.type==="del"?"− ":"  "}{row.text||" "}</div>)}
    </pre>
  </div>;
}

function Settings({t,inputS,settings,onChange}){
  const number=(key,label,min,max)=><label style={LABEL}>{label}
    <input type="number" min={min} max={max} defaultValue={settings[key]} key={settings[key]} onBlur={e=>{const v=Number(e.target.value);if(Number.isFinite(v)&&v!==settings[key])onChange({[key]:v});}} style={{...inputS,width:110,display:"block",marginTop:3}}/></label>;
  return <div>
    <label style={LABEL}><input type="checkbox" checked={settings.enabled} onChange={e=>onChange({enabled:e.target.checked})}/> Use skills in chats</label>
    <label style={LABEL}><input type="checkbox" checked={settings.auto_learn} disabled={!settings.enabled} onChange={e=>onChange({auto_learn:e.target.checked})}/> Learn from finished tasks automatically</label>
    <label style={LABEL}>When HyprChat writes a skill
      <select value={settings.approval} onChange={e=>onChange({approval:e.target.value})} style={{...inputS,display:"block",marginTop:3,width:"auto",maxWidth:"100%"}}>
        <option value="auto">Use it right away</option>
        <option value="review">Always ask me first</option>
      </select>
      <span style={{color:t.mut,display:"block",marginTop:3}}>Skills from scheduled runs, from chats that read email or connector data, and anything a safety check flags always wait for you.</span>
    </label>
    {number("min_tool_calls","Review a turn after this many tool calls",2,30)}
    <label style={LABEL}>Model that writes skills
      <input defaultValue={settings.model} key={settings.model} placeholder="Same as the chat model" onBlur={e=>{if(e.target.value.trim()!==settings.model)onChange({model:e.target.value.trim()});}} style={{...inputS,display:"block",marginTop:3,maxWidth:360}}/>
      <span style={{color:t.mut,display:"block",marginTop:3}}>Leave empty to reuse the model already loaded for the chat. A cloud model named here is called for every review.</span>
    </label>
    <div style={{display:"flex",gap:16,flexWrap:"wrap"}}>
      {number("stale_days","Mark learned skills unused after (days)",1,3650)}
      {number("archive_days","Archive them after (days)",2,3650)}
    </div>
  </div>;
}

export default function SkillsPanel({t,btnS,cardS,inputS,confirmAction,notify}){
  const isMobile=useIsMobile();
  const [skills,setSkills]=useState([]);
  const [loaded,setLoaded]=useState(false);
  const [tab,setTab]=useState("library");
  const [query,setQuery]=useState("");
  const [open,setOpen]=useState(null);          // full skill being read
  const [form,setForm]=useState(null);          // skill being written or edited
  const [settings,setSettings]=useState(null);
  const [showSettings,setShowSettings]=useState(false);
  const [importText,setImportText]=useState(null);
  const [fileForm,setFileForm]=useState(null);
  const [fileView,setFileView]=useState(null);
  const [err,setErr]=useState("");
  const [loadErr,setLoadErr]=useState("");

  const loadEpoch=useRef(0);
  const load=useCallback(async()=>{
    const version=++loadEpoch.current;
    try{const data=await apiJson("/api/skills");if(version===loadEpoch.current){setSkills(data);setLoadErr("");}}
    catch(e){if(version===loadEpoch.current)setLoadErr(String(e.message||e));}
    finally{if(version===loadEpoch.current)setLoaded(true);}
  },[]);
  useEffect(()=>{load();apiJson("/api/skills/settings").then(setSettings).catch(()=>{});},[load]);

  // Run one change, show any refusal, then reload the list. A returned skill replaces the open one.
  const act=async(path,opts,after)=>{
    setErr("");
    try{const data=await apiJson(path,opts);if(data&&data.id)setOpen(o=>o&&o.id===data.id?data:o);after&&after(data);return data;}
    catch(e){setErr(String(e.message||e));notify&&notify({type:"error",text:"Skill change failed",detail:String(e.message||e)});return null;}
    finally{load();}
  };
  const openSkill=id=>act(`/api/skills/${id}`,{},data=>{setOpen(data);setForm(null);setFileForm(null);setFileView(null);});
  const save=async()=>{
    const body={name:form.name,description:form.description,category:form.category||"",body:form.body};
    await act(form.id?`/api/skills/${form.id}`:"/api/skills",{method:form.id?"PATCH":"POST",body},data=>{setForm(null);setOpen(data);});
  };
  const remove=async skill=>{
    if(!await confirmAction({title:"Delete skill",body:`Delete "${skill.name}" and its history? This cannot be undone.`,confirmLabel:"Delete",tone:"danger"}))return;
    await act(`/api/skills/${skill.id}`,{method:"DELETE"},()=>setOpen(null));
  };
  const patch=(skill,body)=>act(`/api/skills/${skill.id}`,{method:"PATCH",body});
  const decide=(skill,verdict)=>act(`/api/skills/${skill.id}/${verdict}`,{method:"POST"},data=>{if(verdict==="reject"&&data.status==="archived")setOpen(null);});
  const saveSettings=async change=>{try{setSettings(s=>({...s,...change}));setSettings(await apiJson("/api/skills/settings",{method:"PUT",body:change}));}catch(e){setErr(String(e.message||e));}};
  const exportSkill=async skill=>{
    try{
      const r=await fetch(`${API}/api/skills/${skill.id}/export`);
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const url=URL.createObjectURL(new Blob([await r.text()],{type:"text/markdown"}));
      const a=document.createElement("a");a.href=url;a.download=`${skill.name}.SKILL.md`;a.click();URL.revokeObjectURL(url);
    }catch(e){setErr(String(e.message||e));}
  };
  const readFile=e=>{const file=e.target.files?.[0];if(!file)return;file.text().then(setImportText);e.target.value="";};

  const shown=skillsForTab(skills,tab,query);
  const waiting=reviewCount(skills);
  const limits=settings?.limits||{description:100,body:6000};
  const statusTone=s=>s==="active"?t.ok:s==="draft"?t.warm:t.mut;

  const chips=skill=><span className="hc-chip-row">
    {skill.pinned&&<Chip tone={t.warm}><IC.Pin/>Pinned</Chip>}
    {skill.status!=="active"&&<Chip tone={statusTone(skill.status)}>{STATUS_LABELS[skill.status]}</Chip>}
    {skill.pending&&<Chip tone={t.warm}>Change waiting</Chip>}
    <Chip tone={t.mut}>{ORIGIN_LABELS[skill.origin]||skill.origin}</Chip>
  </span>;

  const review=skill=>needsReview(skill)&&<div style={{...cardS,border:`1px solid ${t.warm}66`,marginBottom:12}}>
    <div style={{fontWeight:700,color:t.text,marginBottom:4}}>{skill.status==="draft"?"This skill is not in use yet":"A change to this skill is waiting"}</div>
    <div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginBottom:10}}>{heldLabel(skill)}{skill.pending?.reason?` — ${skill.pending.reason}`:""}</div>
    {skill.pending&&<div style={{marginBottom:10}}>
      {skill.pending.description!==skill.description&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginBottom:6}}>New description: {skill.pending.description}</div>}
      <Diff t={t} before={skill.body} after={skill.pending.body}/>
    </div>}
    <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
      <button onClick={()=>decide(skill,"approve")} style={btnS(t.ok)}><IC.Check/> {skill.pending&&skill.status!=="draft"?"Apply change":"Approve"}</button>
      <button onClick={()=>decide(skill,"reject")} style={btnS(t.err)}><IC.X/> {skill.pending&&skill.status!=="draft"?"Discard change":"Reject"}</button>
    </div>
  </div>;

  const detail=open&&!form&&<div>
    <button onClick={()=>setOpen(null)} style={{...btnS(t.mut),marginBottom:12}}><IC.ChevLeft/> All skills</button>
    {review(open)}
    <div style={{...cardS,marginBottom:12}}>
      <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginBottom:4}}>
        <span style={{...MONO,fontSize:"var(--hc-text-control)",fontWeight:700,color:t.text}}>{open.name}</span>{chips(open)}
      </div>
      <div style={{color:t.dim,marginBottom:6,overflowWrap:"anywhere"}}>{open.description}</div>
      <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:10}}>
        Version {open.version} · {usageLabel(open)}{open.last_used_at?`, last ${fmtUtcToLocal(open.last_used_at)}`:""} · category {open.category}
      </div>
      <pre style={{...MONO,margin:"0 0 12px",whiteSpace:"pre-wrap",overflowWrap:"anywhere",color:t.text,borderTop:`1px solid ${t.brd}`,paddingTop:10}}>{open.body}</pre>
      <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
        <button onClick={()=>setForm({id:open.id,name:open.name,description:open.description,category:open.category,body:open.body})} style={btnS(t.acc)}><IC.Pencil/> Edit</button>
        <button onClick={()=>patch(open,{pinned:!open.pinned})} style={btnS(t.warm)}><IC.Pin/> {open.pinned?"Unpin":"Pin"}</button>
        {open.status==="archived"?<button onClick={()=>patch(open,{status:"active"})} style={btnS(t.ok)}>Restore</button>
          :open.status!=="draft"&&<button onClick={()=>patch(open,{status:"archived"})} style={btnS(t.mut)}>Archive</button>}
        <button onClick={()=>exportSkill(open)} style={btnS(t.mut)}><IC.Download/> Export</button>
        <button onClick={()=>remove(open)} style={btnS(t.err)}><IC.Trash/> Delete</button>
      </div>
    </div>

    <div style={{...cardS,marginBottom:12}}>
      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:8}}>
        <div style={{fontWeight:700,color:t.text,flex:1}}>Files</div>
        <button onClick={()=>setFileForm({path:"scripts/",content:""})} style={btnS(t.acc)}><IC.Plus/> Add file</button>
      </div>
      {!(open.files||[]).length&&!fileForm&&<div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>No files. Scripts saved here are copied into the code sandbox when the skill is loaded.</div>}
      {(open.files||[]).map(file=><div key={file.path} style={{display:"flex",gap:8,alignItems:"center",padding:"4px 0"}}>
        <span style={{...MONO,flex:1,minWidth:0,overflowWrap:"anywhere",color:t.text}}>{file.path}</span>
        <span style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{file.size} chars</span>
        <button aria-label={`View ${file.path}`} onClick={()=>fileView?.path===file.path?setFileView(null):apiJson(`/api/skills/${open.id}/files?path=${encodeURIComponent(file.path)}`).then(setFileView).catch(e=>setErr(String(e.message||e)))} style={{...btnS(t.acc),padding:"3px 8px"}}><IC.File/></button>
        <button aria-label={`Delete ${file.path}`} onClick={()=>act(`/api/skills/${open.id}/files?path=${encodeURIComponent(file.path)}`,{method:"DELETE"},()=>setFileView(null))} style={{...btnS(t.err),padding:"3px 8px"}}><IC.Trash/></button>
      </div>)}
      {fileView&&<pre style={{...MONO,margin:"8px 0 0",maxHeight:300,overflow:"auto",whiteSpace:"pre-wrap",overflowWrap:"anywhere",border:`1px solid ${t.brd}`,borderRadius:8,padding:10,color:t.text}}>{fileView.content}</pre>}
      {fileForm&&<div style={{marginTop:8}}>
        <input aria-label="File path" value={fileForm.path} onChange={e=>setFileForm(f=>({...f,path:e.target.value}))} placeholder="scripts/convert.py" style={{...inputS,...MONO,marginBottom:6}}/>
        <textarea aria-label="File content" value={fileForm.content} onChange={e=>setFileForm(f=>({...f,content:e.target.value}))} rows={8} style={{...inputS,...MONO,marginBottom:8,resize:"vertical"}}/>
        <div style={{display:"flex",gap:8}}>
          <button onClick={()=>act(`/api/skills/${open.id}/files`,{method:"PUT",body:fileForm},()=>setFileForm(null))} style={btnS(t.ok)}>Save file</button>
          <button onClick={()=>setFileForm(null)} style={btnS(t.mut)}>Cancel</button>
        </div>
      </div>}
    </div>

    {(open.revisions||[]).length>0&&<div style={cardS}>
      <div style={{fontWeight:700,color:t.text,marginBottom:8}}>Earlier versions</div>
      {open.revisions.map(rev=><details key={rev.id} style={{borderTop:`1px solid ${t.brd}`,padding:"6px 0"}}>
        <summary style={{cursor:"pointer",fontSize:"var(--hc-text-secondary)",color:t.dim}}>Version {rev.version} · replaced {fmtUtcToLocal(rev.created_at)}{rev.reason?` · ${rev.reason}`:""}</summary>
        <div style={{margin:"8px 0"}}><Diff t={t} before={rev.body} after={open.body}/></div>
        <button onClick={()=>act(`/api/skills/${open.id}/revert`,{method:"POST",body:{revision_id:rev.id}})} style={btnS(t.warm)}><IC.Refresh/> Restore version {rev.version}</button>
      </details>)}
    </div>}
  </div>;

  const editor=form&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.warm}55`}}>
    <label style={LABEL}>Name
      <input value={form.name} onChange={e=>setForm(f=>({...f,name:e.target.value}))} onBlur={e=>setForm(f=>({...f,name:slugify(e.target.value)}))} placeholder="batch-resize-images" style={{...inputS,...MONO,display:"block",marginTop:3}} autoFocus={!form.id}/></label>
    <label style={LABEL}>When to use it <span style={{color:form.description.length>limits.description?t.err:t.mut}}>({form.description.length}/{limits.description})</span>
      <input value={form.description} onChange={e=>setForm(f=>({...f,description:e.target.value}))} placeholder="Use when many images need resizing in one batch" style={{...inputS,display:"block",marginTop:3}}/>
      <span style={{color:t.mut,display:"block",marginTop:3}}>This line is the only part every chat sees; the rest is loaded when the skill is used.</span></label>
    <label style={LABEL}>Category
      <input value={form.category} onChange={e=>setForm(f=>({...f,category:e.target.value}))} placeholder="general" style={{...inputS,display:"block",marginTop:3,maxWidth:220}}/></label>
    <label style={LABEL}>Procedure <span style={{color:form.body.length>limits.body?t.err:t.mut}}>({form.body.length}/{limits.body})</span>
      <textarea value={form.body} onChange={e=>setForm(f=>({...f,body:e.target.value}))} rows={isMobile?12:18} style={{...inputS,...MONO,display:"block",marginTop:3,resize:"vertical"}}/></label>
    <div style={{display:"flex",gap:8}}>
      <button onClick={save} style={btnS(t.ok)}>{form.id?"Save":"Add skill"}</button>
      <button onClick={()=>setForm(null)} style={btnS(t.mut)}>Cancel</button>
    </div>
  </div>;

  const emptyHint=tab==="review"?"Skills that had to wait for you show up here: ones learned in scheduled runs, in chats that read email, or flagged by a safety check."
    :tab==="archived"?"Skills you archive, reject, or that go unused for a long time are kept here."
    :"HyprChat saves one after it works out a multi-step task. You can also write one yourself.";

  const list=!open&&!form&&<>
    <input aria-label="Search skills" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search skills" style={{...inputS,marginBottom:12,maxWidth:320}}/>
    {!loaded?Array.from({length:3},(_,i)=><SkeletonCard key={i} t={t} h={72} lines={1} style={{marginBottom:8}}/>)
    :shown.length===0?<EmptyState icon={<IC.Star/>} title={query?"No skills match":tab==="review"?"Nothing to review":tab==="archived"?"Nothing archived":"No skills yet"} hint={query?"Try fewer words.":emptyHint}
        action={tab==="library"&&!query?<button onClick={()=>setForm({...EMPTY_SKILL})} className="hc-button is-primary"><IC.Plus/> New skill</button>:null}/>
    :shown.map(skill=><div key={skill.id} style={{...cardS,marginBottom:8,display:"flex",gap:10,alignItems:"flex-start"}}>
      <button onClick={()=>openSkill(skill.id)} style={{flex:1,minWidth:0,textAlign:"left",background:"none",border:0,padding:0,color:"inherit",font:"inherit",cursor:"pointer",overflowWrap:"anywhere"}}>
        <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
          <span style={{...MONO,fontSize:"var(--hc-text-control)",fontWeight:700,color:t.text}}>{skill.name}</span>{chips(skill)}
        </div>
        <div style={{fontSize:"var(--hc-text-secondary)",color:t.dim,marginTop:3}}>{skill.description}</div>
        <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginTop:4}}>{tab==="review"?heldLabel(skill):`${usageLabel(skill)} · version ${skill.version}`}</div>
      </button>
      {tab==="review"?<>
        <button aria-label={`Approve ${skill.name}`} onClick={()=>decide(skill,"approve")} style={{...btnS(t.ok),padding:"3px 8px"}}><IC.Check/></button>
        <button aria-label={`Reject ${skill.name}`} onClick={()=>decide(skill,"reject")} style={{...btnS(t.err),padding:"3px 8px"}}><IC.X/></button>
      </>:<>
        <button aria-label={skill.pinned?`Unpin ${skill.name}`:`Pin ${skill.name}`} aria-pressed={skill.pinned} onClick={()=>patch(skill,{pinned:!skill.pinned})} style={{...btnS(skill.pinned?t.warm:t.mut),padding:"3px 8px"}}><IC.Pin/></button>
        <button aria-label={`Delete ${skill.name}`} onClick={()=>remove(skill)} style={{...btnS(t.err),padding:"3px 8px"}}><IC.Trash/></button>
      </>}
    </div>)}
  </>;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} color={t.warm} icon={<IC.Star/>} title="Skills"
      subtitle="Procedures HyprChat has learned from its own work. It loads one when a task matches."
      nav={TABS.map(item=><button key={item.id} onClick={()=>{setTab(item.id);setOpen(null);setForm(null);}} style={{...btnS(tab===item.id?t.warm:t.mut),padding:"5px 12px"}}>
        {item.label}{item.id==="review"&&waiting>0?` (${waiting})`:""}</button>)}>
      <button onClick={load} title="Refresh skills" aria-label="Refresh skills" style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
      <button onClick={()=>setShowSettings(v=>!v)} aria-expanded={showSettings} style={btnS(t.mut)}><IC.Sliders/> Settings</button>
      <button onClick={()=>setImportText(v=>v===null?"":null)} style={btnS(t.mut)}><IC.Upload/> Import</button>
      <button onClick={()=>{setOpen(null);setForm({...EMPTY_SKILL});}} style={btnS(t.warm)}><IC.Plus/> New</button>
    </PanelHeader>
    <div style={{overflowY:"auto",padding:isMobile?"14px 12px":"20px 28px",flex:1}}>
      <div style={{maxWidth:820}}>
        {(err||loadErr)&&<div role="alert" style={{color:t.err,fontSize:"var(--hc-text-secondary)",marginBottom:12}}>{err||loadErr}</div>}
        {settings&&!settings.enabled&&!showSettings&&<div style={{...cardS,marginBottom:12,color:t.dim}}>Skills are switched off, so chats neither use nor learn them. <button onClick={()=>saveSettings({enabled:true})} style={{...btnS(t.ok),marginLeft:8}}>Switch on</button></div>}
        {showSettings&&settings&&<div style={{...cardS,marginBottom:16}}><Settings t={t} inputS={inputS} settings={settings} onChange={saveSettings}/></div>}
        {importText!==null&&<div style={{...cardS,marginBottom:16}}>
          <div style={{fontWeight:700,color:t.text,marginBottom:6}}>Import a SKILL.md</div>
          <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut,marginBottom:8}}>Paste the file or choose one. It needs a name and a description at the top, between --- lines.</div>
          <input type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" aria-label="Choose a SKILL.md file" onChange={readFile} style={{marginBottom:8,display:"block"}}/>
          <textarea aria-label="SKILL.md text" value={importText} onChange={e=>setImportText(e.target.value)} rows={8} placeholder={"---\nname: batch-resize-images\ndescription: Use when many images need resizing\n---\n\n## Steps\n..."} style={{...inputS,...MONO,marginBottom:8,resize:"vertical"}}/>
          <div style={{display:"flex",gap:8}}>
            <button disabled={!importText.trim()} onClick={()=>act("/api/skills/import",{method:"POST",body:{content:importText}},data=>{setImportText(null);setOpen(data);})} style={btnS(t.ok)}>Import</button>
            <button onClick={()=>setImportText(null)} style={btnS(t.mut)}>Cancel</button>
          </div>
        </div>}
        {editor}{detail}{list}
      </div>
    </div>
  </div>;
}
