export const researchTerminal = status => ['complete','partial','failed','cancelled'].includes(String(status||'').toLowerCase());
export const researchRevision = report => Number(report?.metrics?.snapshot_revision||0);
export function researchExportMarkdown(report, body) {
  const warnings=[...(report?.metrics?.completion_checks?.issues||[])];
  if(report?.status&&report.status!=='complete')warnings.unshift(`Report status: ${report.status}.`);
  if(report?.error)warnings.push(report.error);
  if(report?.metrics?.quality==='limited')warnings.push('Evidence coverage is limited. Review gaps before relying on conclusions.');
  return warnings.length?`${String(body||'').trim()}\n\n> [!WARNING]\n> ${warnings.join(' ').replace(/\n/g,' ')}\n`:String(body||'').trim();
}
export function mergeResearchReport(current, incoming) {
  if(!current||current.id!==incoming.id)return incoming;
  const oldRev=researchRevision(current),newRev=researchRevision(incoming);
  if(newRev<oldRev)return current;
  if(researchTerminal(current.status)&&!researchTerminal(incoming.status)&&newRev===oldRev)return current;
  return {...current,...incoming};
}
export function researchHeadings(markdown) {
  let fenced=false;const out=[];
  for(const line of String(markdown||'').split('\n')){
    if(/^\s*```/.test(line)){fenced=!fenced;continue;}
    const m=!fenced&&line.match(/^##\s+(.+)$/);
    if(m)out.push(m[1].trim());
  }
  return out;
}
// Replace only prose citations. Fenced and inline code remain byte-for-byte intact.
export function linkResearchCitations(markdown, sources) {
  const ids=new Set((sources||[]).map(s=>String(s.index||s.source_index)));
  let fenced=false;
  return String(markdown||'').split('\n').map(line=>{
    if(/^\s*```/.test(line)){fenced=!fenced;return line;}
    if(fenced)return line;
    return line.split(/(`[^`]*`)/g).map((part,i)=>i%2?part:part.replace(/\[\s*S\s*\d+(?:\s*[,;]\s*S?\s*\d+)*\s*\](?!\()/gi,group=>
      (group.match(/\d+/g)||[]).map(n=>ids.has(String(Number(n)))?`[S${Number(n)}](#research-source-S${Number(n)})`:`[S${Number(n)}]`).join(' '))).join('');
  }).join('\n');
}
