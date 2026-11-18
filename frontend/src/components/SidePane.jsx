import React,{useState} from 'react';
import {API,userScopedUrl} from '../session.js';
import useFocusScope from '../useFocusScope.js';
import {IC} from './icons.jsx';
import {MDWrap} from './hyprChatWidgets.jsx';
import DocumentPreviewPanel from './DocumentPreviewPanel.jsx';
import {useWorkbench,closeWorkbench} from '../workbench.js';
import HtmlLivePreview from './HtmlLivePreview.jsx';
import DocLivePreview,{Table} from './DocLivePreview.jsx';
import PreviewBar from './PreviewBar.jsx';
import {TEXT_PREVIEW_TYPES,prettyJson} from '../docPreview.js';
import DaedalusWorkbench from './DaedalusWorkbench.jsx';

const IMAGE_TYPES=["png","jpg","jpeg","gif","webp","svg"];
export function readPreviewWidth(){try{return parseInt(localStorage.getItem("hc-preview-w")||"420");}catch{return 420;}}

// The resizable right-hand pane beside the chat. One slot: the workbench store
// (a Daedalus job, a live HTML preview, or a document / drawing / table from a reply) wins
// when open, else the file preview main.jsx drives through previewFile. On mobile it is a
// full overlay dialog. Every preview uses the same header row (PreviewBar.jsx).
export default function SidePane({previewFile,previewContent,previewError,previewArchive,onClosePreview,onRevision,t,font,md,codeFont,evts,isMobile,onOpenArtifact,conversationId}){
  const workbench=useWorkbench();
  const [width,setWidth]=useState(readPreviewWidth);
  const open=workbench.open||!!previewFile;
  const close=()=>{if(workbench.open)closeWorkbench();else onClosePreview();};
  const focus=useFocusScope(open&&isMobile,close);
  const startDrag=e=>{
    e.preventDefault();
    const startX=e.clientX,startW=width;
    let lastW=startW;  // the state value in onUp would be the pre-drag width
    const onMove=ev=>{const next=Math.max(260,Math.min(900,startW+(startX-ev.clientX)));lastW=next;setWidth(next);};
    const onUp=()=>{try{localStorage.setItem("hc-preview-w",String(lastW));}catch{}window.removeEventListener("mousemove",onMove);window.removeEventListener("mouseup",onUp);};
    window.addEventListener("mousemove",onMove);window.addEventListener("mouseup",onUp);
  };
  if(!open)return null;
  const bench=workbench.open;
  const label=bench?(workbench.kind==='daedalus'?"Workbench panel":"Preview panel"):"Preview panel";
  return <div ref={focus} role={isMobile?"dialog":"complementary"} aria-modal={isMobile?true:undefined} aria-label={label}
    style={{width:isMobile?"auto":width,flexShrink:0,borderLeft:isMobile?"none":"1px solid var(--hc-line)",background:t.bgDeep,display:"flex",flexDirection:"column",position:"relative",overflow:"hidden",transition:"width .15s",...(isMobile?{position:"absolute",inset:0,zIndex:30}:{})}}>
    {!isMobile&&<div onMouseDown={startDrag} style={{position:"absolute",left:0,top:0,bottom:0,width:5,cursor:"col-resize",zIndex:10,background:"transparent"}} onMouseEnter={e=>e.currentTarget.style.background=`${t.acc}44`} onMouseLeave={e=>e.currentTarget.style.background="transparent"}/>}
    {bench&&workbench.kind==='daedalus'
      ?<DaedalusWorkbench key={workbench.job?.id} job={workbench.job} tab={workbench.tab} t={t} font={font} md={md} onOpenArtifact={onOpenArtifact} onClose={close}/>
      :bench&&workbench.kind==='doc'
      ?<DocLivePreview doc={workbench.doc} md={md} font={font} codeFont={codeFont} conversationId={conversationId} onClose={close} onOpenArtifact={onOpenArtifact}/>
      :bench
      // A chat HTML preview draws its own header and runs edge to edge (HtmlLivePreview.jsx).
      ?<HtmlLivePreview html={workbench.html} font={font} conversationId={conversationId} onClose={close} onOpenArtifact={onOpenArtifact}/>
      :<FilePreview previewFile={previewFile} previewContent={previewContent} previewError={previewError} previewArchive={previewArchive} onRevision={onRevision} t={t} font={font} md={md} codeFont={codeFont} evts={evts} onClose={close}/>}
  </div>;
}

// A delivered file, a web source or an archive, under the same header row as the live previews.
const EDGE_TYPES=["html","pdf"];
function FilePreview({previewFile,previewContent,previewError,previewArchive,onRevision,t,font,md,codeFont,evts,onClose}){
  const host=(()=>{try{return new URL(previewFile.url).hostname.replace("www.","");}catch{return previewFile.filename;}})();
  const title=previewFile.isExternal?host:previewFile.filename;
  const edge=!previewError&&!previewFile.artifactId&&EDGE_TYPES.includes(previewFile.type);
  const status=previewFile.isExternal?`A web page from ${host}, shown through HyprChat. Scripts in it do not run.`:`A file from this chat (.${previewFile.type}).`;
  return <div className="hp-root" style={{fontFamily:font}}>
    <PreviewBar title={title} status={status} onClose={onClose}>
      <span className="hp-kind">{previewFile.isExternal?"web":`.${previewFile.type}`}</span>
      {previewFile.isExternal
        ?<a className="hp-link" href={previewFile.url} target="_blank" rel="noopener" title="Open the page in a new tab" aria-label="Open the page in a new tab"><IC.External/></a>
        :<a className="hp-link" href={userScopedUrl(previewFile.url)} download={previewFile.filename} title={`Download ${previewFile.filename}`} aria-label={`Download ${previewFile.filename}`}><IC.Download/></a>}
    </PreviewBar>
    <div className={edge?"hp-file is-edge":"hp-file"}>
      <FilePreviewBody previewFile={previewFile} previewContent={previewContent} previewError={previewError} previewArchive={previewArchive} onRevision={onRevision} t={t} font={font} md={md} codeFont={codeFont} evts={evts}/>
    </div>
  </div>;
}

// The file/url/archive renderer, moved verbatim from main.jsx.
export function FilePreviewBody({previewFile,previewContent,previewError,previewArchive,onRevision,t,font,md,codeFont,evts}){
  const image=IMAGE_TYPES.includes(previewFile.type);
  if(previewFile.artifactId)return <DocumentPreviewPanel key={previewFile.artifactId} artifactId={previewFile.artifactId} events={evts} t={t} font={font} onRevision={onRevision}/>;
  if(previewError)return <div style={{display:"flex",flexDirection:"column",gap:10,padding:16,background:`${t.err}10`,border:`1px solid ${t.err}33`,borderRadius:"var(--hc-radius-card)",color:t.err}}>
    <div style={{fontWeight:700,fontSize:"var(--hc-text-secondary)"}}>⚠ {previewError}</div>
    {previewFile.isExternal
      ?<a href={previewFile.url} target="_blank" rel="noopener" style={{color:t.acc,fontSize:"var(--hc-text-secondary)",textDecoration:"none",padding:"5px 10px",border:`1px solid ${t.acc}44`,borderRadius:"var(--hc-radius)",display:"inline-flex",alignItems:"center",gap:4}}>↗ Open in new tab</a>
      :<a href={userScopedUrl(previewFile.url)} download={previewFile.filename} style={{color:t.ok,fontSize:"var(--hc-text-secondary)",textDecoration:"none",padding:"5px 10px",border:`1px solid ${t.ok}44`,borderRadius:"var(--hc-radius)",display:"inline-flex",alignItems:"center",gap:4}}>⬇ Download anyway</a>}
  </div>;
  if(!previewFile.isExternal&&previewFile.type==="pdf")return <iframe src={userScopedUrl(previewFile.url)} title={previewFile.filename} style={{width:"100%",height:"100%",minHeight:500,border:0,background:"white"}}/>;
  if(previewFile.isExternal&&previewFile.type==="pdf")return <iframe src={`${API}/api/proxy-preview?url=${encodeURIComponent(previewFile.url)}`} style={{width:"100%",height:"100%",border:"none",borderRadius:"var(--hc-radius)",minHeight:500}} title={previewFile.filename}/>;
  if(previewFile.isExternal&&image)return <img src={`${API}/api/proxy-preview?url=${encodeURIComponent(previewFile.url)}`} style={{maxWidth:"100%",borderRadius:"var(--hc-radius)",display:"block"}} alt={previewFile.filename}/>;
  if(image&&!previewFile.isExternal)return <img src={`${API}${previewFile.url}`} style={{maxWidth:"100%",borderRadius:"var(--hc-radius)",display:"block"}} alt={previewFile.filename}/>;
  if(previewFile.type==="archive"&&previewArchive)return <div>
    <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12,padding:"8px 12px",background:`${t.surface}F5`,borderRadius:"var(--hc-radius)",border:"1px solid var(--hc-line-subtle)"}}>
      <span style={{fontSize:"var(--hc-text-display)"}}>📦</span>
      <div style={{flex:1}}>
        <div style={{fontSize:"var(--hc-text-secondary)",fontWeight:700,color:t.text}}>{previewArchive.filename}</div>
        <div style={{fontSize:"var(--hc-text-secondary)",color:t.mut}}>{previewArchive.file_count} files, {previewArchive.entries.length} entries</div>
      </div>
      <a href={userScopedUrl(previewFile.url)} download={previewFile.filename} style={{color:t.ok,fontSize:"var(--hc-text-secondary)",textDecoration:"none",padding:"4px 10px",border:`1px solid ${t.ok}44`,borderRadius:"var(--hc-radius)",fontWeight:600}}>⬇ Download</a>
    </div>
    <div style={{borderRadius:"var(--hc-radius)",border:"1px solid var(--hc-line-subtle)",overflow:"hidden"}}>
      {previewArchive.entries.filter(e=>e.name!=="./"&&e.name!=="."&&e.name!=="/").map((entry,i)=>{
        const depth=Math.max(0,(entry.name.match(/\//g)||[]).length-(entry.is_dir?1:0));
        const basename=entry.is_dir?entry.name.replace(/\/$/,"").split("/").pop()+"/":entry.name.split("/").pop();
        const sizeStr=entry.is_dir?"":entry.size>=1048576?(entry.size/1048576).toFixed(1)+"MB":entry.size>=1024?(entry.size/1024).toFixed(1)+"KB":entry.size+"B";
        const extC=(basename.match(/\.(\w+)$/)||[])[1]||"";
        const iconColor=entry.is_dir?t.warm:["py","js","ts","rs","go","java","c","cpp","sh"].includes(extC)?t.acc:["md","txt","log","csv"].includes(extC)?t.dim:["json","yaml","toml","xml","html","css"].includes(extC)?t.f1:t.mut;
        return <div key={i} style={{display:"flex",alignItems:"center",gap:6,padding:"4px 10px",paddingLeft:10+depth*16,background:i%2===0?"transparent":`${t.surface}33`,borderBottom:i<previewArchive.entries.length-1?"1px solid var(--hc-line-subtle)":"none",fontSize:"var(--hc-text-secondary)",color:entry.is_dir?t.warm:t.dim}}>
          <span style={{fontSize:"var(--hc-text-secondary)",flexShrink:0,color:iconColor}}>{entry.is_dir?"📁":"📄"}</span>
          <span style={{flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",fontWeight:entry.is_dir?600:400}}>{basename}</span>
          {sizeStr&&<span style={{fontSize:"var(--hc-text-secondary)",color:t.mut,flexShrink:0,fontFamily:codeFont}}>{sizeStr}</span>}
        </div>;
      })}
    </div>
  </div>;
  // The frame is drawn at real size whatever the UI scale (.hp-stage cancels the app's zoom; see htmlPreview.css).
  if(previewFile.type==="html"&&previewContent)return <div className="hp-stage"><iframe className="hp-frame" sandbox={previewFile.isExternal?"":"allow-scripts"} srcDoc={previewContent} style={{width:"100%"}} title={previewFile.filename}/></div>;
  if(["csv","tsv"].includes(previewFile.type)&&previewContent)return <Table code={previewContent} label={previewFile.type} filename={previewFile.filename}/>;
  if(previewFile.type==="json"&&previewContent)return <pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:"var(--hc-text-secondary)",color:t.dim,fontFamily:codeFont,lineHeight:1.6}}>{prettyJson(previewContent)}</pre>;
  if(["md","markdown","mdx"].includes(previewFile.type)&&previewContent)return <div style={{lineHeight:1.7,fontSize:"var(--hc-text-secondary)",color:t.dim}}><MDWrap>{md(previewContent)}</MDWrap></div>;
  if(previewContent)return <pre style={{margin:0,whiteSpace:"pre-wrap",fontSize:"var(--hc-text-secondary)",color:t.dim,fontFamily:codeFont,lineHeight:1.6}}>{previewContent}</pre>;
  // A type the pane does not fetch (it used to say "Loading..." for good): say so and offer the file.
  if(!image&&previewFile.type!=="archive"&&!previewFile.isExternal&&!TEXT_PREVIEW_TYPES.includes(previewFile.type))return <div style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:200,color:t.mut,fontSize:"var(--hc-text-secondary)",gap:10,textAlign:"center"}}>
    <span>A .{previewFile.type} file has no preview here.</span>
    <a href={userScopedUrl(previewFile.url)} download={previewFile.filename} style={{color:t.ok,textDecoration:"none",padding:"5px 10px",border:`1px solid ${t.ok}44`,borderRadius:"var(--hc-radius)"}}>Download {previewFile.filename}</a>
  </div>;
  if(!image&&previewFile.type!=="archive")return <div role="status" style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:200,color:t.mut,fontSize:"var(--hc-text-secondary)",gap:6}}>Loading…</div>;
  if(previewFile.type==="archive"&&!previewArchive&&!previewError)return <div style={{display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",height:200,color:t.mut,fontSize:"var(--hc-text-secondary)",gap:6}}><span style={{fontSize:32}}>📦</span>Reading archive...</div>;
  return null;
}
