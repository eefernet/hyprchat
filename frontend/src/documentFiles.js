export const isOfficeFile=name=>/\.(docx?|pptx?|xlsx?|docm|pptm|xlsm)$/i.test(name||"");
export const isSpreadsheetFile=name=>/\.(xlsx?|xlsm)$/i.test(name||"");
export function documentAttachmentContext(a){
  return `📄 **${a.name}** — Office artifact \`${a.artifactId}\` (SHA-256: ${a.sha256}). Read it with document_read before editing. Original retained.${a.sandboxPath?` Spreadsheet analysis copy: \`${a.sandboxPath}\`; use execute_code to analyze the full file.`:""}`;
}

export const isDocumentEvent=e=>String(e?.data?.tool||'').startsWith('document_')||e?.data?.run_role==='documents'||String(e?.data?.run_id||e?.run_id||'').startsWith('doc-')||String(e?.data?.url||'').startsWith('/api/documents/files/');
export function documentRunIds(meta={},events=[]){
  const all=[...(meta.saved_events||[]),...events];
  const ids=new Set(all.filter(isDocumentEvent).map(e=>e.data?.run_id||e.run_id).filter(Boolean));
  for(const id of meta.run_ids||[])if(String(id).startsWith('doc-')||meta.run_types?.[id]==='documents')ids.add(id);
  return [...ids];
}
export function documentArtifactId(url){
  try{return new URL(url,'http://hyprchat.local').pathname.match(/^\/api\/documents\/files\/([^/]+)$/)?.[1]||null;}catch{return null;}
}
export function nextDocumentRevision(current,events){
  if(!current)return null;
  let id=current.id,match=null;
  // A PDF preview follows revisions of its Office source as well.
  const source=current.metadata?.document_role==='preview'?current.parent_artifact_id:null;
  if(source)id=source;
  for(const e of events||[]){
    const d=e.data||{};
    if(e.type==='file_ready'&&d.supersedes_artifact_id===id&&d.artifact_id!==id){id=d.artifact_id;match=d;}
  }
  return match;
}
