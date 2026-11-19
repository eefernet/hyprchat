export function safeLocalStorageSet(key,value){
  try{localStorage.setItem(key,value);}catch{}
}

// Debounced writer for the server-side user-pref KV (/api/prefs/{key}).
// Rapid state churn (typing a prompt, toggling tags) collapses into one PUT.
export function createPrefsSync({api,getContext=()=>({}),onError=()=>{}}){
  const timers=new Map(),controllers=new Set();
  const put=async(key,value,context=getContext(),{keepalive=false}={})=>{
    const controller=new AbortController();controllers.add(controller);
    try{
      if(context.userId!==getContext().userId||context.epoch!==getContext().epoch)return;
      const r=await fetch(`${api}/api/prefs/${encodeURIComponent(key)}`,{
        method:"PUT",signal:controller.signal,...(keepalive?{keepalive:true}:{}),
        headers:{"Content-Type":"application/json",...(context.userId?{"X-HyprChat-User":context.userId}:{}),...(context.session?{"X-HyprChat-Session":context.session}:{})},body:JSON.stringify(value),
      });
      if(!r.ok)throw new Error(`Preference save failed (HTTP ${r.status})`);
    }catch(error){if(error.name!=="AbortError")onError(error);}
    finally{controllers.delete(controller);}
  };
  const queue=(key,value,delay=1200)=>{
    const context=getContext();
    clearTimeout(timers.get(key)?.timer);
    timers.set(key,{value,context,timer:setTimeout(()=>{timers.delete(key);put(key,value,context);},delay)});
  };
  // The page is going away: send what is still waiting now. A change made in the last second before
  // closing the tab used to be lost, and the server's older value came back on the next visit.
  const flush=()=>{
    const waiting=[...timers.entries()];timers.clear();
    return waiting.map(([key,{value,context,timer}])=>{clearTimeout(timer);return put(key,value,context,{keepalive:true});});
  };
  const cancel=()=>{for(const entry of timers.values())clearTimeout(entry.timer);timers.clear();for(const c of controllers)c.abort();controllers.clear();};
  return {put,queue,flush,cancel,get waiting(){return timers.size;}};
}

export function createSettingsSync({api,settingsLoadedRef,seenSettingEffectRef,skipSettingPatchRef,flashSettingsPulse,onRefused=()=>{}}){
  const hydrateServerSetting=(settingKey,setter,value,current)=>{
    if(value!==current)skipSettingPatchRef.current[settingKey]=true;
    setter(value);
  };
  const persistServerSetting=(storageKey,settingKey,value,storageValue=String(value))=>{
    const seen=!!seenSettingEffectRef.current[settingKey];
    seenSettingEffectRef.current[settingKey]=true;
    safeLocalStorageSet(storageKey,storageValue);
    if(skipSettingPatchRef.current[settingKey]){delete skipSettingPatchRef.current[settingKey];return;}
    if(!settingsLoadedRef.current&&!seen)return;
    fetch(`${api}/api/settings`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({[settingKey]:value})})
      // 403: a server-wide setting changed by a profile that is not Main. Say so; "Failed" alone read as a glitch.
      .then(r=>{if(r.status===403){flashSettingsPulse("Main profile only","error");onRefused(settingKey);return;}flashSettingsPulse(r.ok?"Saved":"Failed",r.ok?"success":"error");})
      .catch(()=>flashSettingsPulse("Failed","error"));
  };
  return {hydrateServerSetting,persistServerSetting};
}
