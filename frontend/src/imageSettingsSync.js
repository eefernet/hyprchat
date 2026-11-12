// Image preferences are server-owned and scoped to the captured profile.
// Writes are event-driven: loading values never echoes a PATCH.
export function createImageSettingsSync({request,getContext,onValues,onReady,onError,onSaved,onPreset}){
  let generation=0,ready=false,confirmed={},presets={},tail=Promise.resolve();
  const versions=new Map();
  const capture=()=>({generation,...getContext()});
  const current=c=>c.generation===generation&&c.userId===getContext().userId&&c.epoch===getContext().epoch;
  const reset=()=>{generation++;ready=false;confirmed={};presets={};versions.clear();tail=Promise.resolve();onReady(false);};
  const load=async()=>{
    reset();const context=capture();
    try{
      const data=await request('/api/images/settings');
      if(!current(context))return;
      confirmed={...data};onValues(data);ready=true;onReady(true);
    }catch(error){if(current(context)&&error.name!=="AbortError")onError(error.message);}
  };
  const queue=(run)=>{tail=tail.then(run,run);return tail;};
  const save=(key,value)=>{
    if(!ready)return Promise.resolve();
    const context=capture(),version=(versions.get(key)||0)+1;
    versions.set(key,version);onValues({[key]:value});
    return queue(async()=>{
      if(!current(context))return;
      try{
        const data=await request('/api/images/settings',{method:'PATCH',body:{[key]:value}});
        if(!current(context))return;
        confirmed[key]=data[key];
        if(versions.get(key)===version){onValues({[key]:data[key]},{[key]:value});onSaved();}
      }catch(error){
        if(!current(context)||error.name==="AbortError")return;
        if(versions.get(key)===version){onValues({[key]:confirmed[key]},{[key]:value});onError(error.message);}
      }
    });
  };
  const setPresets=data=>{presets={...data};};
  const savePreset=(checkpoint,patch)=>{
    if(!ready)return Promise.resolve();
    const context=capture(),key=`preset:${checkpoint}`,version=(versions.get(key)||0)+1;
    versions.set(key,version);
    return queue(async()=>{
      if(!current(context))return;
      try{
        const data=await request(`/api/images/model-settings/${encodeURIComponent(checkpoint)}`,{method:'PUT',body:patch});
        if(!current(context))return;
        presets[checkpoint]=data.settings;
        if(versions.get(key)===version){onPreset(checkpoint,data.settings,patch);onSaved();}
      }catch(error){
        if(!current(context)||error.name==="AbortError")return;
        if(versions.get(key)===version){onPreset(checkpoint,presets[checkpoint]||{},patch);onError(error.message);}
      }
    });
  };
  return {load,save,reset,setPresets,savePreset};
}
