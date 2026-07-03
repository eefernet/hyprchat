export function safeLocalStorageSet(key,value){
  try{localStorage.setItem(key,value);}catch{}
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
