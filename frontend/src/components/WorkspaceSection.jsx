import React from 'react';
import ActionMenu from './ActionMenu.jsx';
import {IC} from './icons.jsx';
import {moreLabel} from '../sidebarSections.js';
import './sidebarSections.css';

// One workspace in the left bar: a header that opens and closes, with the workspace's chats under it.
// `section` comes from sidebarSections(); `renderChat` draws one chat row (the root component's ConvRow
// wiring). The header only toggles: it never changes the open chat or the panel, so on a phone the
// drawer stays open.
export default function WorkspaceSection({section,t,renderChat,on,busy=false}){
  const {name,open,chats,total,more,expanded}=section;
  const bodyId=`ws-sec-${section.id}`;
  return <div className="ws-sec" style={{'--ws-sec-accent':t.acc}}>
    <div className="ws-sec-head">
      <button type="button" className="ws-sec-toggle" aria-expanded={open} aria-controls={bodyId} onClick={on.toggle}
        title={open?`Close ${name}`:`Open ${name}`}>
        <span className="ws-sec-icon" aria-hidden="true">{open?<IC.FolderOpen/>:<IC.Folder/>}</span>
        <span className="ws-sec-name">{name}</span>
        {!open&&total>0&&<span className="ws-sec-count" aria-label={`${total} chat${total===1?'':'s'}`}>{total}</span>}
      </button>
      <button type="button" className="ws-sec-add" aria-label={`New chat in ${name}`} title={`New chat in ${name}`} onClick={on.newChat}><IC.Plus/></button>
      <ActionMenu label={`Actions for ${name}`} compact items={[
        {label:'New chat here',onClick:on.newChat},
        {label:'Open workspace',onClick:on.openWorkspace},
      ]}/>
    </div>
    {open&&<div className="ws-sec-body" id={bodyId}>
      {chats.map(chat=>renderChat(chat,{nested:true,key:`${section.id}:${chat.id}`}))}
      {chats.length===0&&total===0&&<div className="ws-sec-empty">No chats yet</div>}
      {more>0&&<button type="button" className="ws-sec-more" disabled={busy} onClick={on.showMore}>{busy?'Loading…':moreLabel(more)}</button>}
      {more===0&&expanded&&<button type="button" className="ws-sec-more" onClick={on.showFewer}>Show fewer</button>}
    </div>}
  </div>;
}
