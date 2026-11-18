// `interrupted` is what a report cut off by a restart is marked (reap_stale_runs). Left out, such a report
// read as still running: a live badge, a Stop button, Start disabled and polling that never ended.
export const researchTerminal = status => ['complete','partial','failed','cancelled','interrupted'].includes(String(status||'').toLowerCase());
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

// A counter tile: "12/48" while under the target. Past it the target was a plan that was exceeded, not a
// total ("59/48" read like an error: follow-up search rounds add sources after the target is reached),
// so the tile shows the count and the label carries the target.
export function targetTile(label, value, target) {
  const count = Number(value) || 0, goal = Number(target) || 0;
  if (!goal) return [label, String(count)];
  return count > goal ? [`${label} · target ${goal}`, String(count)] : [label, `${count}/${goal}`];
}

// A report's source count. List rows carry `source_count`; a full record (which replaces the row once
// the report is opened or polled) has only `metrics` and `sources`, so cards used to say "0 sources".
export function reportSourceCount(report) {
  const direct = Number(report?.source_count);
  if (direct > 0) return direct;
  return Number(report?.metrics?.source_count) || (Array.isArray(report?.sources) ? report.sources.length : 0) || 0;
}

// The card line under a report title. Summaries saved before 2026-11-18 start with the Method section
// ("## Method Collected 59 source records…"): skip to the text after it and drop Markdown marks.
export function reportCardSummary(report) {
  let text = String(report?.summary || '').trim();
  if (/^#{1,6}\s*Method\b/i.test(text)) {
    const next = text.slice(2).search(/#{2,6}\s+\S/);
    text = next === -1 ? '' : text.slice(2 + next);
  }
  text = text.replace(/>\s*\[![A-Z]+\]\s*/g, '').replace(/(^|\s)#{1,6}\s+/g, '$1').replace(/\*\*|__|`/g, '').replace(/\s+/g, ' ').trim();
  return text || String(report?.query || '');
}

// The report list with `report` updated where it already is, or added on top when it is new. Opening or
// polling a report used to move it to the top of the list, under the pointer that had just clicked it.
export function putReport(list, report) {
  const rows = list || [];
  return rows.some(row => row.id === report.id) ? rows.map(row => (row.id === report.id ? report : row)) : [report, ...rows];
}

