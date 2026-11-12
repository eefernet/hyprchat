// Read-only compatibility for historical probes that recorded advisory as passed.
export function checkOutcome(check,legacy=false){
  if(!check.environment_fault&&(check.workflow_status==='advisory'||!check.passed&&(check.optional||legacy&&['lint','typecheck'].includes(check.phase)&&!check.is_test)))return 'advisory';
  return check.skipped?'skipped':check.status==='unverified'?'unverified':check.passed?'passed':'failed';
}
export function currentChecks(job={}){
  const latest=new Map();
  (Array.isArray(job.checks)?job.checks:[]).forEach(check=>{
    if(check?.id&&!(job.engine==='openhands159'&&job.revision_id&&check.revision_id!==job.revision_id))latest.set(check.id,check);
  });
  return [...latest.values()];
}
export function onlyInconclusiveChecks(job={}){
  const rows=currentChecks(job);
  return job.engine==='openhands159'&&rows.length>0&&!rows.some(c=>checkOutcome(c)==='passed');
}
