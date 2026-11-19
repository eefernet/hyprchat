import React from 'react';
import ActionMenu from './ActionMenu.jsx';
import {IC} from './icons.jsx';
import './convRow.css';

// One chat in the left bar. It was a single 4 KB line inside main.jsx; the root component now passes
// what the row shows and the handlers it calls (`on`), nothing else.
//
// Markers, left to right before the title: a pulsing dot while THIS chat is replying (it used to mark
// whichever chat was open), a steady dot for a reply that finished while another chat was open, then the
// workspace / council / persona / fork icons. After the title: a pencil for an unsent draft and the number
// of messages waiting in the queue.
//
// `nested`: the row sits under its workspace's header, so it does not repeat the folder icon.
// `workspaces`: [{id,name,member}] for the menu's "In workspace" items (a chat can be in several).
// `when` / `whenTitle`: how long ago the chat was last used ("2h", "Tue") and the full date for its tooltip.
// The time and the "…" button share one place at the end of the row: the button replaces the time on
// hover or keyboard focus, and on touch screens both are shown.
export default function ConvRow({conv,title,t,active,replying,unread,hasDraft,queued=0,council,persona,avatar,accent,pinned,tags=[],tagFilter,rename,tagEditor,inputS,btnS,on,nested=false,workspaces=[],when='',whenTitle=''}){
  const measureTitle=e=>{
    const row=e.currentTarget,clip=row.querySelector('.conv-title-clip'),text=row.querySelector('.conv-title-text');
    if(!clip||!text)return;
    const overflow=Math.max(0,text.scrollWidth-clip.clientWidth);
    row.style.setProperty('--conv-title-marquee',`${overflow+18}px`);
    row.setAttribute('data-title-overflow',overflow>3?'1':'0');
  };
  const resetTitle=e=>e.currentTarget.setAttribute('data-title-overflow','0');
  // Enter or Space on the row itself opens the chat (not when the key was pressed on its menu button).
  const openByKey=e=>{if(e.target!==e.currentTarget)return;if(e.key==='Enter'||e.key===' '){e.preventDefault();on.open();}};
  const rowBg=active?`${accent}14`:'transparent';
  const hoverBg=active?rowBg:`${t.sfBri}1f`;
  const state=[replying?'replying':'',unread?'new reply':'',hasDraft?'unsent draft':'',queued?`${queued} queued`:''].filter(Boolean).join(', ');
  return <div className={`conv-item${nested?' is-nested':''}`} style={{'--conv-warm':t.warm}}>
    <div className={`conv-row${active?' is-act':''}`} role="button" tabIndex={0} aria-current={active?'page':undefined}
      aria-label={state?`${title} (${state})`:title}
      onClick={on.open} onKeyDown={openByKey} onMouseEnter={measureTitle} onFocus={measureTitle} onMouseLeave={resetTitle} onBlur={resetTitle}
      style={{background:rowBg,'--conv-accent':accent,'--conv-row-hover-bg':hoverBg}}>
      <div className="conv-main" style={{color:active?t.text:t.dim,fontWeight:active?500:400}}>
        {replying&&<span className="hc-stream-dot" role="status" aria-label="Replying"/>}
        {!replying&&unread&&<span className="conv-unread" role="status" aria-label="New reply" title="A reply finished here while you were in another chat"/>}
        {!nested&&conv.effective_workspace?.name&&<span className="conv-mark" style={{color:t.mut,opacity:.7}} title={`Workspace: ${conv.effective_workspace.name}`}><IC.Folder/></span>}
        {council&&<span className="conv-mark" style={{color:t.pink,opacity:active?1:.7}}><IC.Council/></span>}
        {persona&&(avatar?<img className="conv-avatar" src={avatar} style={{opacity:active?1:.85}} alt=""/>
          :<span className="conv-mark conv-mark-box" style={{color:accent,opacity:active?1:.75}}>{persona==='persona'?<IC.User/>:<IC.Bot/>}</span>)}
        {conv.forked_from&&<span className="conv-mark" style={{color:t.f1||t.acc,opacity:.6}} title="Forked conversation"><IC.GitBranch/></span>}
        <span className="conv-title-clip" title={title}><span className="conv-title-text">{title}</span></span>
        {hasDraft&&<span className="conv-mark conv-draft" title="Unsent draft"><IC.Pencil/></span>}
        {queued>0&&<span className="conv-queued" title={`${queued} message${queued===1?'':'s'} waiting to be sent`}>{queued}</span>}
      </div>
      <div className="conv-tail">
      {when&&<span className="conv-when" title={whenTitle||undefined}>{when}</span>}
      <div className="conv-menu">
        <ActionMenu label={`Actions for ${title}`} compact items={[
          ...(on.pin?[{label:pinned?'Unpin':'Pin to top',onClick:on.pin}]:[]),   // not for chats that are not saved: the server has nothing to pin
          {label:'Rename',onClick:on.startRename},
          ...(on.generateTitle?[{label:'Generate title',onClick:on.generateTitle}]:[]),
          {label:'Edit tags',onClick:on.toggleTags},
          ...(on.setWorkspace?workspaces.map(ws=>({key:`ws:${ws.id}`,label:`In workspace: ${ws.name}`,checked:!!ws.member,onClick:()=>on.setWorkspace(ws.id,!ws.member)})):[]),
          {label:'Delete conversation',destructive:true,onClick:on.remove},
        ]}/>
      </div>
      </div>
    </div>
    {tags.length>0&&<div className="conv-tags">
      {tags.map(tag=><button type="button" key={tag} className="conv-tag" aria-pressed={tagFilter===tag} title={tagFilter===tag?'Show all chats':`Show only chats tagged ${tag}`}
        onClick={e=>{e.stopPropagation();on.filterTag(tag);}}>{tag}</button>)}
    </div>}
    {rename!=null&&<div className="conv-edit" onClick={e=>e.stopPropagation()}>
      <div className="conv-edit-row">
        <input autoFocus aria-label="Chat name" value={rename} maxLength={80} onChange={e=>on.renameChange(e.target.value)} onFocus={e=>e.target.select()}
          onKeyDown={e=>{if(e.nativeEvent?.isComposing||e.keyCode===229)return;if(e.key==='Enter'){e.preventDefault();on.renameSave();}if(e.key==='Escape'){e.stopPropagation();on.renameCancel();}}}
          placeholder="Chat name" style={{...inputS,flex:1,minWidth:0,padding:'4px 8px',fontSize:'var(--hc-text-control)'}}/>
        <button aria-label="Save name" onClick={on.renameSave} style={{...btnS(t.ok),padding:'3px 9px',fontSize:'var(--hc-text-control)'}}>Save</button>
        <button aria-label="Cancel rename" onClick={on.renameCancel} style={{...btnS(t.mut),padding:'3px 7px',fontSize:'var(--hc-text-control)'}}>×</button>
      </div>
    </div>}
    {tagEditor!=null&&<div className="conv-edit" onClick={e=>e.stopPropagation()}>
      <div className="conv-edit-row">
        <input aria-label={`New tag for ${title}`} value={tagEditor} onChange={e=>on.tagInput(e.target.value)}
          onKeyDown={e=>{if(e.nativeEvent?.isComposing||e.keyCode===229)return;if(e.key==='Enter'){e.preventDefault();on.addTag();}if(e.key==='Escape'){e.stopPropagation();on.toggleTags();}}}
          placeholder="Add tag..." style={{...inputS,padding:'3px 6px',fontSize:'var(--hc-text-control)',flex:1,minWidth:0}}/>
        <button aria-label="Add tag" onClick={on.addTag} style={{...btnS(t.warm),padding:'3px 7px',fontSize:'var(--hc-text-control)'}}>+</button>
        <button aria-label="Close tag editor" onClick={on.toggleTags} style={{...btnS(t.mut),padding:'3px 7px',fontSize:'var(--hc-text-control)'}}>×</button>
      </div>
      {tags.length>0&&<div className="conv-tags conv-tags-edit">
        {tags.map(tag=><span key={tag} className="conv-tag conv-tag-static">{tag}
          <button type="button" className="conv-tag-remove" aria-label={`Remove tag ${tag}`} onClick={()=>on.removeTag(tag)}>×</button>
        </span>)}
      </div>}
    </div>}
  </div>;
}
