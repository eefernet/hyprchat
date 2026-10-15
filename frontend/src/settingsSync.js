export function safeLocalStorageSet(key,value){
  try{localStorage.setItem(key,value);}catch{}
}

// Debounced writer for the server-side user-pref KV (/api/prefs/{key}).
// Rapid state churn (typing a prompt, toggling tags) collapses into one PUT.
export function createPrefsSync({api,getContext=()=>({}),onError=()=>{}}){
  const timers=new Map(),controllers=new Set();
  const put=async(key,value,context=getContext())=>{
    const controller=new AbortController();controllers.add(controller);
    try{
      if(context.userId!==getContext().userId||context.epoch!==getContext().epoch)return;
      const r=await fetch(`${api}/api/prefs/${encodeURIComponent(key)}`,{
        method:"PUT",signal:controller.signal,
        headers:{"Content-Type":"application/json",...(context.userId?{"X-HyprChat-User":context.userId}:{}),...(context.session?{"X-HyprChat-Session":context.session}:{})},body:JSON.stringify(value),
      });
      if(!r.ok)throw new Error(`Preference save failed (HTTP ${r.status})`);
    }catch(error){if(error.name!=="AbortError")onError(error);}
    finally{controllers.delete(controller);}
  };
  const queue=(key,value,delay=1200)=>{
    const context=getContext();
    clearTimeout(timers.get(key));
    timers.set(key,setTimeout(()=>{timers.delete(key);put(key,value,context);},delay));
  };
  const cancel=()=>{for(const timer of timers.values())clearTimeout(timer);timers.clear();for(const c of controllers)c.abort();controllers.clear();};
  return {put,queue,cancel};
}

export function createSettingsSync({api,settingsLoadedRef,seenSettingEffectRef,skipSettingPatchRef,flashSettingsPulse}){
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
      .then(r=>{flashSettingsPulse(r.ok?"Saved":"Failed",r.ok?"success":"error");})
      .catch(()=>flashSettingsPulse("Failed","error"));
  };
  return {hydrateServerSetting,persistServerSetting};
}
