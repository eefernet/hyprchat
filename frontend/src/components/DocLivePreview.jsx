import React,{useMemo,useState} from 'react';
import {IC} from './icons.jsx';
import {MDWrap} from './hyprChatWidgets.jsx';
import PreviewBar,{SaveButton} from './PreviewBar.jsx';
import {delimiterOf,fileNameFor,parseDelimited,svgComplete,svgDataUrl} from '../docPreview.js';
import {downloadText} from '../previewSave.js';
import './docPreview.css';

const NAMES={markdown:'Document',svg:'Drawing',table:'Table'};
const MIME={markdown:'text/markdown',svg:'image/svg+xml',table:'text/csv'};

// A document, a drawing or a table from a reply, in the side pane (docPreview.js has the rules).
// Nothing here can run code: Markdown goes through the chat's own renderer, a drawing is shown as an
// image (scripts inside an <img> SVG never execute), a table is text in cells.
export default function DocLivePreview({doc,md,font,codeFont,conversationId,onClose,onOpenArtifact}){
  const [source,setSource]=useState(false);
  const format=doc?.format||'markdown',code=doc?.code||'',live=!!doc?.streaming;
  const title=doc?.title||NAMES[format]||'Preview';
  const filename=fileNameFor(format,title,doc?.filename,code);
  const status=live?`${NAMES[format]||'Preview'}: updating as the reply is written.`:`${NAMES[format]||'Preview'} from this chat. Nothing in it can run.`;
  return <div className="hp-root" style={{fontFamily:font}}>
    <PreviewBar title={title} status={status} live={live} onClose={onClose}>
      <div className="hp-group" role="group" aria-label="View">
        <button type="button" aria-pressed={!source} onClick={()=>setSource(false)} title="Show it as it reads">{format==='table'?'Table':format==='svg'?'Drawing':'Read'}</button>
        <button type="button" aria-pressed={source} onClick={()=>setSource(true)} title="Show the text it is made of">Source</button>
      </div>
      <SaveButton conversationId={conversationId} filename={filename} kind={format} content={code} onOpenArtifact={onOpenArtifact}/>
      <button type="button" className="hp-icon" onClick={()=>downloadText(filename,code,MIME[format])} aria-label={`Download ${filename}`} title={`Download ${filename}`}><IC.Download/></button>
    </PreviewBar>
    <div className="dv-stage">
      {source?<pre className="dv-source" style={{fontFamily:codeFont}}>{code}</pre>
        :format==='svg'?<Drawing code={code} title={title} live={live}/>
        :format==='table'?<Table code={code} label={doc?.label} filename={doc?.filename}/>
        :<article className="dv-page hc-prose"><MDWrap>{md(code,{codePath:`doc:${doc?.key||''}`,streaming:live})}</MDWrap></article>}
    </div>
  </div>;
}

function Drawing({code,title,live}){
  if(!svgComplete(code))return <div className="dv-note">{live?'The drawing appears when it is finished…':'This drawing is incomplete. Open Source to see what was written.'}</div>;
  return <div className="dv-drawing"><img src={svgDataUrl(code)} alt={title}/></div>;
}

export function Table({code,label,filename}){
  const table=useMemo(()=>parseDelimited(code,{delimiter:delimiterOf(label,filename,code)}),[code,label,filename]);
  if(!table.header.length)return <div className="dv-note">This table is empty.</div>;
  return <div className="dv-table-wrap">
    <table className="dv-table">
      <thead><tr>{table.header.map((cell,i)=><th key={i} scope="col">{cell}</th>)}</tr></thead>
      <tbody>{table.rows.map((row,r)=><tr key={r}>{row.map((cell,c)=><td key={c}>{cell}</td>)}</tr>)}</tbody>
    </table>
    <div className="dv-count">{table.truncated?`First ${table.rows.length} of ${table.total} rows. Download or Save for all of them.`:`${table.total} row${table.total===1?'':'s'}`}</div>
  </div>;
}
