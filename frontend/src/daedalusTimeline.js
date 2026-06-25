const _DAEDALUS_TOOLS = new Set([
  "plan_project","generate_code","run_review","run_acceptance_review",
  "run_fixer","run_aider_fix","ask_project","download_project","download_file",
  "list_files","read_file","write_file","run_shell",
]);

const _DAEDALUS_FULL_BUILD_TOOLS = new Set(["plan_project","generate_code"]);
const _DAEDALUS_ACTIVE_WORKFLOW_STATES = new Set(["queued","pending","running","planning","reviewing","fixing","accepting"]);

const _eventsHaveDaedalus = (evts)=>{
  if(!Array.isArray(evts))return false;
  return evts.some(e=>{
    const tool=e?.data?.tool||"";
    return _DAEDALUS_TOOLS.has(tool) || !!(e?.data?.run_id||e?.run_id);
  });
};

const _eventsHaveDaedalusFullBuild = (evts)=>{
  if(!Array.isArray(evts))return false;
  return evts.some(e=>_DAEDALUS_FULL_BUILD_TOOLS.has(e?.data?.tool||""));
};

const _runRolesHaveDaedalusFullBuild = (roles)=>{
  if(!Array.isArray(roles))return false;
  return roles.some(role=>{
    const r=String(role||"").toLowerCase();
    return r==="architect"||r.startsWith("builder");
  });
};

const _daedalusRunIdsForMessage = (meta,isLast,liveEvts,runIdsFromEvents)=>{
  const saved=Array.isArray(meta?.saved_events)?meta.saved_events:[];
  const ridsFromMeta=Array.isArray(meta?.run_ids)?meta.run_ids:[];
  const ridsFromEvts=typeof runIdsFromEvents==="function"
    ? runIdsFromEvents(isLast&&liveEvts?.length?liveEvts:saved)
    : [];
  const seen=new Set(),out=[];
  for(const rid of [...ridsFromMeta,...ridsFromEvts]){
    if(rid&&!seen.has(rid)){seen.add(rid);out.push(rid);}
  }
  return out;
};

const _isDaedalusFullBuildOutput = ({meta={},savedEvents=[],liveEvents=[],runIds=[],workflows=[]}={})=>{
  if(meta?.has_full_product_build===true)return true;
  if(_eventsHaveDaedalusFullBuild(savedEvents)||_eventsHaveDaedalusFullBuild(liveEvents))return true;
  if(_runRolesHaveDaedalusFullBuild(meta?.run_roles))return true;
  if((meta?.in_progress||liveEvents.length>0)&&Array.isArray(workflows)&&workflows.some(w=>{
    const mode=String(w?.mode||"").toLowerCase();
    const state=String(w?.state||"").toLowerCase();
    return mode==="build_from_prompt"&&_DAEDALUS_ACTIVE_WORKFLOW_STATES.has(state);
  }))return true;
  if(meta?.has_full_product_build===false)return false;
  // Legacy round snapshots only stored run ids. Treat larger run stacks as
  // build workflows so older full-product turns still render their summary.
  return !savedEvents.length&&!liveEvents.length&&runIds.length>=3;
};

const _isDaedalusOutput = ({meta={},savedEvents=[],liveEvents=[],runIds=[],workflows=[]}={})=>{
  return !!(
    runIds.length ||
    (Array.isArray(workflows)&&workflows.some(w=>_DAEDALUS_ACTIVE_WORKFLOW_STATES.has(String(w?.state||"").toLowerCase()))) ||
    _eventsHaveDaedalus(savedEvents) ||
    _eventsHaveDaedalus(liveEvents) ||
    (Array.isArray(meta.run_ids)&&meta.run_ids.length)
  );
};

export {
  _eventsHaveDaedalus,
  _eventsHaveDaedalusFullBuild,
  _daedalusRunIdsForMessage,
  _isDaedalusFullBuildOutput,
  _isDaedalusOutput,
};
