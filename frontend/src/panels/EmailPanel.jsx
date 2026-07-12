import React,{useState,useEffect,useCallback} from 'react';

import { API } from '../session.js';
import { apiJson } from '../api.js';
import { fmtLocalMinute, fmtUtcToLocal } from '../datetime.js';
import { IC } from '../components/icons.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';
import { colorFor } from '../components/identityColor.js';
import useIsMobile from '../useIsMobile.js';

const EMPTY_ACCOUNT={label:"",imap_host:"",imap_port:993,imap_ssl:true,smtp_host:"",smtp_port:587,smtp_ssl:true,username:"",password:"",from_address:""};
// Hash the bare address (not the display-name formatting) so a sender's avatar color is stable.
const emailOf=s=>{const m=/<([^>]+)>/.exec(s||"");return(m?m[1]:s||"").toLowerCase().trim();};

export default function EmailPanel({t,btnS,cardS,inputS,confirmAction,notify}){
  const isMobile=useIsMobile();
  const [accounts,setAccounts]=useState([]);
  const [messages,setMessages]=useState([]);
  const [reader,setReader]=useState(null);      // {msg, body|null, loading, reply}
  const [acctForm,setAcctForm]=useState(null);
  const [showSettings,setShowSettings]=useState(false);
  const [compose,setCompose]=useState(null);    // {to,subject,body}
  const [unreadOnly,setUnreadOnly]=useState(false);
  const [polling,setPolling]=useState(false);
  const [err,setErr]=useState("");

  const loadAccounts=useCallback(async()=>{
    try{setAccounts(await apiJson("/api/email/accounts"));}catch(e){setErr(String(e.message||e));}
  },[]);
  const loadMessages=useCallback(async()=>{
    try{setMessages(await apiJson(`/api/email/messages?limit=100${unreadOnly?"&unread_only=true":""}`));}catch(e){setErr(String(e.message||e));}
  },[unreadOnly]);
  useEffect(()=>{loadAccounts();loadMessages();},[loadAccounts,loadMessages]);

  const saveAccount=async()=>{
    if(!acctForm)return;
    setErr("");
    const r=await fetch(`${API}/api/email/accounts${acctForm.id?`/${acctForm.id}`:""}`,{
      method:acctForm.id?"PATCH":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(acctForm)});
    if(r.ok){setAcctForm(null);loadAccounts();}
    else{const d=await r.json().catch(()=>({}));setErr(d.detail||`HTTP ${r.status}`);}
  };
  const testAccount=async id=>{
    setErr("");
    const r=await fetch(`${API}/api/email/accounts/${id}/test`,{method:"POST"});
    const d=await r.json().catch(()=>({}));
    if(r.ok)notify({type:"success",text:"Connected",detail:`${d.inbox_messages} messages in INBOX.`});
    else setErr(d.detail||"Connection failed");
  };
  const pollNow=async()=>{
    setPolling(true);setErr("");
    try{
      const r=await fetch(`${API}/api/email/poll`,{method:"POST"});
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||`HTTP ${r.status}`);}
      await loadMessages();await loadAccounts();
    }catch(e){setErr(String(e.message||e));}
    finally{setPolling(false);}
  };
  const openMessage=async m=>{
    setReader({msg:m,body:null,loading:true,reply:m.draft_reply||""});
    try{
      const r=await fetch(`${API}/api/email/messages/${m.id}/body`);
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
      setReader(p=>p&&p.msg.id===m.id?{...p,body:d,loading:false}:p);
      loadMessages();
    }catch(e){setReader(p=>p&&p.msg.id===m.id?{...p,body:{body:`Failed to load: ${e.message||e}`},loading:false}:p);}
  };
  const msgAction=async(id,action)=>{
    setErr("");
    try{
      await apiJson(`/api/email/messages/${id}/${action}`,{method:"POST"});
      if(reader?.msg?.id===id&&(action==="archive"||action==="delete"))setReader(null);
    }catch(e){setErr(String(e.message||e));}
    loadMessages();
  };
  const sendReply=async()=>{
    if(!reader?.reply?.trim())return;
    setErr("");
    const r=await fetch(`${API}/api/email/messages/${reader.msg.id}/reply`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({body:reader.reply})});
    const d=await r.json().catch(()=>({}));
    if(r.ok){notify({type:"success",text:"Reply sent",detail:`To ${d.to||reader.msg.from_addr}`});setReader(null);loadMessages();}
    else setErr(d.detail||"Send failed");
  };
  const sendCompose=async()=>{
    if(!compose?.to||!compose?.subject||!compose?.body)return;
    setErr("");
    const r=await fetch(`${API}/api/email/send`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(compose)});
    const d=await r.json().catch(()=>({}));
    if(r.ok){notify({type:"success",text:"Email sent",detail:`To ${d.to||compose.to}`});setCompose(null);}
    else setErr(d.detail||"Send failed");
  };

  const urgencyChip=u=>u==="urgent"?<span style={{fontSize:8,fontWeight:800,color:t.bg,background:t.err,borderRadius:4,padding:"1px 5px"}}>URGENT</span>
    :u==="low"?<span style={{fontSize:8,fontWeight:700,color:t.mut,border:`1px solid ${t.brd}55`,borderRadius:4,padding:"1px 5px"}}>low</span>:null;
  const fmtTs=fmtLocalMinute;
  const senderAvatar=addr=>{const key=emailOf(addr);const m=key.match(/[a-z0-9]/i);const c=colorFor(key,t);
    return <div style={{width:30,height:30,borderRadius:"50%",flexShrink:0,display:"flex",alignItems:"center",justifyContent:"center",fontSize:12,fontWeight:800,color:c,background:`${c}2E`,border:`1px solid ${c}44`}}>{(m?m[0]:"?").toUpperCase()}</div>;};

  const settingsCard=(showSettings||accounts.length===0)&&<div style={{...cardS,marginBottom:16}}>
    <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
      <div style={{fontSize:12,fontWeight:800,color:t.acc}}>Accounts (IMAP/SMTP)</div>
      <div style={{flex:1}}/>
      <button onClick={()=>setAcctForm({...EMPTY_ACCOUNT})} style={{...btnS(t.warm),padding:"4px 9px",fontSize:10}}><IC.Plus/> Add</button>
    </div>
    {accounts.map(a=><div key={a.id} style={{fontSize:11,color:t.dim,padding:"6px 0",borderBottom:`1px solid ${t.brd}18`}}>
      <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
        <span style={{fontWeight:700,color:t.text}}>{a.label||a.username}</span>
        <span style={{color:t.mut,fontSize:9}}>{a.imap_host}</span>
        {a.last_error?<span style={{color:t.err,fontSize:9}} title={a.last_error}>error</span>
          :a.last_checked_at?<span style={{color:t.ok,fontSize:9}}>checked {fmtUtcToLocal(a.last_checked_at)}</span>:null}
        <div style={{flex:1}}/>
        <button onClick={()=>testAccount(a.id)} style={{...btnS(t.acc),padding:"3px 8px",fontSize:9}}>Test</button>
        <button onClick={()=>setAcctForm({id:a.id,label:a.label,imap_host:a.imap_host,imap_port:a.imap_port,imap_ssl:a.imap_ssl,smtp_host:a.smtp_host,smtp_port:a.smtp_port,smtp_ssl:a.smtp_ssl,username:a.username,password:"",from_address:a.from_address})} style={{...btnS(t.mut),padding:"3px 8px",fontSize:9}}><IC.Pencil/></button>
        <button onClick={async()=>{if(await confirmAction({title:"Remove email account",body:"Remove this email account and its cached messages?",confirmLabel:"Remove",tone:"danger"})){await fetch(`${API}/api/email/accounts/${a.id}`,{method:"DELETE"});loadAccounts();loadMessages();}}} style={{...btnS(t.err),padding:"3px 8px",fontSize:9}}><IC.Trash/></button>
      </div>
      <label style={{fontSize:10,color:a.allow_autonomous_send?t.warm:t.mut,display:"flex",gap:6,alignItems:"center",marginTop:5}}>
        <input type="checkbox" checked={!!a.allow_autonomous_send} onChange={async e=>{await fetch(`${API}/api/email/accounts/${a.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({allow_autonomous_send:e.target.checked})});loadAccounts();}}/>
        Allow the assistant to SEND email autonomously (off = it drafts, you review and send)
      </label>
      <label style={{fontSize:10,color:a.allow_autonomous_delete?t.err:t.mut,display:"flex",gap:6,alignItems:"center",marginTop:4}}>
        <input type="checkbox" checked={!!a.allow_autonomous_delete} onChange={async e=>{await fetch(`${API}/api/email/accounts/${a.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({allow_autonomous_delete:e.target.checked})});loadAccounts();}}/>
        Allow the assistant to DELETE email autonomously (off = it must ask you first; deleting here in the panel always works)
      </label>
    </div>)}
    {acctForm&&<div style={{marginTop:10,borderTop:`1px solid ${t.brd}33`,paddingTop:10}}>
      <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8}}>
        <input value={acctForm.label} onChange={e=>setAcctForm(f=>({...f,label:e.target.value}))} placeholder="Label" style={{...inputS,flex:1,minWidth:100}}/>
        <input value={acctForm.username} onChange={e=>setAcctForm(f=>({...f,username:e.target.value}))} placeholder="Email / username" style={{...inputS,flex:2,minWidth:180}}/>
        <input type="password" value={acctForm.password} onChange={e=>setAcctForm(f=>({...f,password:e.target.value}))} placeholder={acctForm.id?"Password (blank = keep)":"Password / app password"} style={{...inputS,flex:1,minWidth:150}}/>
      </div>
      <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:8,alignItems:"center"}}>
        <input value={acctForm.imap_host} onChange={e=>setAcctForm(f=>({...f,imap_host:e.target.value}))} placeholder="IMAP host (imap.gmail.com)" style={{...inputS,flex:2,minWidth:160}}/>
        <input type="number" value={acctForm.imap_port} onChange={e=>setAcctForm(f=>({...f,imap_port:Number(e.target.value)}))} style={{...inputS,width:80}}/>
        <label style={{fontSize:10,color:t.mut}}><input type="checkbox" checked={acctForm.imap_ssl} onChange={e=>setAcctForm(f=>({...f,imap_ssl:e.target.checked}))}/> SSL</label>
      </div>
      <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:10,alignItems:"center"}}>
        <input value={acctForm.smtp_host} onChange={e=>setAcctForm(f=>({...f,smtp_host:e.target.value}))} placeholder="SMTP host (smtp.gmail.com)" style={{...inputS,flex:2,minWidth:160}}/>
        <input type="number" value={acctForm.smtp_port} onChange={e=>setAcctForm(f=>({...f,smtp_port:Number(e.target.value)}))} style={{...inputS,width:80}}/>
        <input value={acctForm.from_address} onChange={e=>setAcctForm(f=>({...f,from_address:e.target.value}))} placeholder="From address (optional)" style={{...inputS,flex:1,minWidth:150}}/>
      </div>
      <div style={{display:"flex",gap:8}}>
        <button onClick={saveAccount} style={btnS(t.ok)}>{acctForm.id?"Save":"Add account"}</button>
        <button onClick={()=>setAcctForm(null)} style={btnS(t.mut)}>Cancel</button>
      </div>
    </div>}
  </div>;

  const composeCard=compose&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.warm}55`}}>
    <div style={{fontSize:12,fontWeight:800,color:t.warm,marginBottom:10}}>New email</div>
    <input value={compose.to} onChange={e=>setCompose(c=>({...c,to:e.target.value}))} placeholder="To" style={{...inputS,marginBottom:8}}/>
    <input value={compose.subject} onChange={e=>setCompose(c=>({...c,subject:e.target.value}))} placeholder="Subject" style={{...inputS,marginBottom:8}}/>
    <textarea value={compose.body} onChange={e=>setCompose(c=>({...c,body:e.target.value}))} rows={6} placeholder="Message" style={{...inputS,marginBottom:10,resize:"vertical"}}/>
    <div style={{display:"flex",gap:8}}>
      <button onClick={sendCompose} style={btnS(t.ok)}><IC.Send/> Send</button>
      <button onClick={()=>setCompose(null)} style={btnS(t.mut)}>Cancel</button>
    </div>
  </div>;

  const readerCard=reader&&<div style={{...cardS,marginBottom:16,border:`1px solid ${t.acc}55`}}>
    <div style={{display:"flex",gap:8,alignItems:"center",marginBottom:8}}>
      {!isMobile&&senderAvatar(reader.msg.from_addr)}
      <div style={{flex:1,minWidth:0}}>
        <div style={{fontSize:13,fontWeight:800,color:t.text}}>{reader.msg.subject}</div>
        <div style={{fontSize:10,color:t.mut,marginTop:2}}>{reader.msg.from_addr} · {fmtTs(reader.msg.date_at)}</div>
      </div>
      <button onClick={()=>msgAction(reader.msg.id,"archive")} style={{...btnS(t.mut),padding:"4px 9px",fontSize:10}}>Archive</button>
      <button onClick={async()=>{if(await confirmAction({title:"Delete email",body:"Move this email to Trash?",confirmLabel:"Delete",tone:"danger"}))msgAction(reader.msg.id,"delete");}} style={{...btnS(t.err),padding:"4px 9px",fontSize:10}}>Delete</button>
      <button onClick={()=>setReader(null)} style={{...btnS(t.mut),padding:"4px 9px",fontSize:10}}><IC.X/></button>
    </div>
    {reader.msg.summary&&<div style={{fontSize:11,color:t.acc,marginBottom:8}}>AI summary: {reader.msg.summary}</div>}
    <div style={{fontSize:12,color:t.dim,whiteSpace:"pre-wrap",maxHeight:isMobile?320:"46vh",overflowY:"auto",background:`${t.bgDeep}66`,border:`1px solid ${t.brd}22`,borderRadius:8,padding:12}}>
      {reader.loading?"Loading body from server...":(reader.body?.body||"")}
    </div>
    <div style={{marginTop:10}}>
      <div style={{fontSize:10,color:t.mut,marginBottom:4}}>Reply{reader.msg.draft_reply?" (AI draft loaded — edit before sending)":""}</div>
      <textarea value={reader.reply} onChange={e=>setReader(p=>({...p,reply:e.target.value}))} rows={4} style={{...inputS,resize:"vertical",marginBottom:8}}/>
      <button onClick={sendReply} style={btnS(t.ok)}><IC.Send/> Send reply</button>
    </div>
  </div>;

  const msgRow=m=>{const sel=!isMobile&&reader?.msg?.id===m.id;
    return <div key={m.id} onClick={()=>openMessage(m)} style={{...cardS,marginBottom:6,padding:"9px 12px",cursor:"pointer",display:"flex",gap:10,alignItems:"center",...(sel?{border:`1px solid ${t.acc}55`,background:`${t.acc}0d`}:{}),borderLeft:`3px solid ${m.unread?t.warm:sel?t.acc:"transparent"}`}}>
      {!isMobile&&senderAvatar(m.from_addr)}
      <div style={{flex:1,minWidth:0}}>
        <div style={{display:"flex",gap:8,alignItems:"center"}}>
          <span style={{fontSize:12,fontWeight:m.unread?800:600,color:t.text,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{m.subject||"(no subject)"}</span>
          {urgencyChip(m.urgency)}
          {m.draft_reply&&<span style={{fontSize:8,fontWeight:700,color:t.acc,border:`1px solid ${t.acc}55`,borderRadius:4,padding:"1px 5px"}}>draft ready</span>}
        </div>
        <div style={{fontSize:10,color:t.mut,marginTop:3,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
          {m.from_addr} · {fmtTs(m.date_at)}{m.summary?` — ${m.summary}`:m.snippet?` — ${m.snippet.slice(0,90)}`:""}
        </div>
      </div>
      {m.unread&&<button onClick={e=>{e.stopPropagation();msgAction(m.id,"read");}} style={{...btnS(t.mut),padding:"3px 8px",fontSize:9}}>Read</button>}
      <button onClick={e=>{e.stopPropagation();msgAction(m.id,"archive");}} style={{...btnS(t.mut),padding:"3px 8px",fontSize:9}}>Archive</button>
      <button onClick={async e=>{e.stopPropagation();if(await confirmAction({title:"Delete email",body:"Move this email to Trash?",confirmLabel:"Delete",tone:"danger"}))msgAction(m.id,"delete");}} style={{...btnS(t.err),padding:"3px 8px",fontSize:9}}>Delete</button>
    </div>;};

  const emptyList=accounts.length===0?<EmptyState t={t} icon={<IC.Send/>} title="No email account yet" hint="Add your IMAP/SMTP account (use an app password for Gmail/iCloud). The assistant can then triage, summarize, and draft replies."/>
    :messages.length===0?<EmptyState t={t} icon={<IC.Send/>} title="Inbox cache is empty" hint='Press "Check now" — the background poller also runs every ~5 minutes.'/>:null;

  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <PanelHeader t={t} icon={<IC.Send/>} title="Email"
      subtitle="Inbox triage, drafts, and urgent-mail alerts. Bodies load live from IMAP."
      nav={<label style={{fontSize:10,color:t.mut,display:"flex",gap:5,alignItems:"center"}}>
        <input type="checkbox" checked={unreadOnly} onChange={e=>setUnreadOnly(e.target.checked)}/> unread only
      </label>}>
      {accounts.length>0&&<button onClick={pollNow} disabled={polling} style={btnS(t.acc)}>{polling?"Checking...":<><IC.Refresh/> Check now</>}</button>}
      {accounts.length>0&&<button onClick={()=>setCompose({to:"",subject:"",body:""})} style={btnS(t.warm)}><IC.Plus/> Compose</button>}
      <button onClick={()=>setShowSettings(s=>!s)} style={btnS(showSettings?t.acc:t.mut)}><IC.Settings/></button>
    </PanelHeader>
    {isMobile
      ?<div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
        <div style={{maxWidth:900}}>
          {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}
          {settingsCard}
          {composeCard}
          {readerCard}
          {emptyList||messages.map(msgRow)}
        </div>
      </div>
      :<div style={{flex:1,display:"flex",overflow:"hidden",minHeight:0}}>
        <div style={{width:"clamp(300px,40%,420px)",flexShrink:0,borderRight:`1px solid ${t.brd}22`,overflowY:"auto",padding:"14px 14px 20px"}}>
          {emptyList||messages.map(msgRow)}
        </div>
        <div style={{flex:1,minWidth:0,overflowY:"auto",padding:"14px 20px 20px"}}>
          {err&&<div style={{color:t.err,fontSize:12,marginBottom:12}}>{err}</div>}
          {settingsCard}
          {composeCard}
          {readerCard}
          {!settingsCard&&!composeCard&&!readerCard&&<EmptyState t={t} icon={<IC.Send/>} title="Select a message" hint="Pick an email from the list to read it, see the AI summary, and reply."/>}
        </div>
      </div>}
  </div>;
}
