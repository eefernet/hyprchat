import React, {useEffect, useId, useState} from 'react';
import {API} from '../session.js';
import './daedalusSettings.css';

const roles=['chat','architect','builder','reviewer','acceptance','qa','fixer','aider','compaction','visual'];
const thinkingModes=['inherit','off','on','low','medium','high'];
const span=seconds=>{const n=Number(seconds);return !(n>0)?'this long':n<90?`${n} seconds`:n<5400?`about ${Math.round(n/60)} minutes`:`about ${+(n/3600).toFixed(1)} hours`;};
// [key, accessible name, visible label, unit, hint]. The accessible names are stable identifiers: the browser test finds fields by them.
const groups={
  context:[
    ['openhands_num_ctx','Daedalus context window','Context window','tokens','Shared by every coding stage. 0 inherits the global context window.'],
    ['generation_num_predict','Completion allowance','Completion allowance','tokens','Reserved for each model response.'],
    ['context_headroom_percent','Context headroom (%)','Context headroom','%','Kept free for request overhead.'],
    ['context_compaction_threshold','Compact at (%)','Compact at','%','Older turns are summarized once this share of the working input is used.'],
  ],
  budget:[
    ['daedalus_job_seconds','Execution allowance (seconds)','Execution allowance','sec',value=>`A job pauses at its checkpoint after ${span(value)}; Continue grants another allowance.`],
    ['daedalus_model_calls','Model calls per allowance','Model calls per allowance','calls','Shared by planning, coding, compaction and review. Evidence-gated builds hold back about a quarter for verification.'],
    ['daedalus_attempt_turns','Turns per coding attempt','Turns per coding attempt','turns','The builder returns to verification after this many model turns.'],
    ['daedalus_command_seconds','Command timeout (seconds)','Command timeout','sec','Longest a single build or test command may run.'],
  ],
  browser:[
    ['daedalus_browser_step_seconds','Browser interaction timeout (seconds)','Browser interaction timeout','sec','Longest wait for one browser action or assertion before the failure goes back for repair.'],
    ['daedalus_browser_startup_seconds','Preview startup timeout (seconds)','Preview startup timeout','sec','Stop waiting when the preview server does not become ready.'],
    ['daedalus_visual_batch_size','Screenshots per visual review call','Screenshots per visual review call','','Image batches share the job execution allowance.'],
    ['daedalus_image_tokens','Estimated tokens per image','Estimated tokens per image','tokens','Reserved separately from text; actual model usage is checked.'],
  ],
  storage:[
    ['daedalus_upload_mb','Project archive limit (MB)','Project archive limit','MB','Largest compressed upload.'],
    ['daedalus_extracted_mb','Extracted project limit (MB)','Extracted project limit','MB','Largest expanded source size.'],
    ['daedalus_storage_mb','Worker project storage (MB)','Worker project storage','MB','Allowance for source workspaces and checkpoints.'],
    ['daedalus_min_free_mb','Worker free disk reserve (MB)','Worker free disk reserve','MB','New work pauses before disk space runs out.'],
  ],
};
const fields=Object.values(groups).flat();
const sources={daedalus:'Daedalus window',global:'global context'};

const payload=(values,exclusions)=>({
  ...Object.fromEntries(fields.map(([key])=>[key,Number(values[key])])),
  aider_num_ctx:0,
  daedalus_v3_enabled:!!values.daedalus_v3_enabled,
  daedalus_visual_review:!!values.daedalus_visual_review,
  daedalus_policy7_edits:!!values.daedalus_policy7_edits,
  daedalus_policy7_builds:!!values.daedalus_policy7_builds,
  daedalus_visual_model:values.daedalus_visual_model||'',
  daedalus_browser_viewports:values.daedalus_browser_viewports,
  daedalus_compaction:values.daedalus_compaction,
  daedalus_role_contexts:Object.fromEntries(roles.map(role=>[role,Number(values.daedalus_role_contexts?.[role]||0)])),
  daedalus_role_outputs:Object.fromEntries(roles.map(role=>[role,Number(values.daedalus_role_outputs?.[role]||0)])),
  daedalus_role_thinking:Object.fromEntries(roles.map(role=>[role,values.daedalus_role_thinking?.[role]||'inherit'])),
  helper_contexts:Object.fromEntries(Object.entries(values.helper_contexts||{}).map(([key,value])=>[key,Number(value)])),
  daedalus_exclude_dirs:exclusions.split(',').map(value=>value.trim()).filter(Boolean),
});
const loaded=data=>({...data,daedalus_role_contexts:{...data.daedalus_role_contexts,aider:data.daedalus_role_contexts?.aider||data.aider_num_ctx||0}});
const effective=resolved=>!resolved?'—':resolved.error||`${resolved.num_ctx?.toLocaleString()||'—'} tokens · output ${resolved.num_predict?.toLocaleString()||'—'} · from ${resolved.source?.startsWith('stage:')?'stage override':sources[resolved.source]||'inherited'}`;

function Section({title,hint,children}){
  return <section className="ds-section">
    <div className="ds-head"><h3 className="ds-title">{title}</h3>{hint&&<span className="ds-help">{hint}</span>}</div>
    {children}
  </section>;
}

function Toggle({label,description,chip,checked,disabled,onChange}){
  const id=useId();
  return <button type="button" role="switch" aria-checked={checked} aria-label={chip?`${label} (${chip.toLowerCase()})`:label} aria-describedby={id} disabled={disabled} className="ds-toggle" onClick={()=>onChange(!checked)}>
    <span className="ds-track" aria-hidden="true"/>
    <span className="ds-toggle-text">
      <span className="ds-toggle-title">{label}{chip&&<span className="ds-chip">{chip}</span>}</span>
      <span id={id} className="ds-help">{description}</span>
    </span>
  </button>;
}

function NumberField({field:[key,name,label,unit,hint],value,onChange}){
  const share=key.endsWith('percent')||key.endsWith('threshold');
  return <label className="ds-field"><span className="ds-label">{label}</span>
    <span className="ds-control">
      <input aria-label={name} type="number" min={key==='openhands_num_ctx'?0:share?0.01:1} max={share?99.99:undefined} step={share?'any':1} value={value??''} onChange={e=>onChange(key,e.target.value)}/>
      {unit&&<span className="ds-unit">{unit}</span>}
    </span>
    <span className="ds-help">{typeof hint==='function'?hint(value):hint}</span>
  </label>;
}

export default function DaedalusSettings({t,font,onSaved}){
  const [values,setValues]=useState(null),[message,setMessage]=useState(''),[failed,setFailed]=useState(false),[saving,setSaving]=useState(false);
  const [exclusions,setExclusions]=useState(''),[saved,setSaved]=useState('');
  const vars={'--ds-text':t.text,'--ds-dim':t.dim||t.text,'--ds-muted':t.mut,'--ds-accent':t.acc,'--ds-err':t.err,'--ds-ok':t.ok,'--ds-warn':t.warm||t.acc,
    '--ds-card':`${t.surface}40`,'--ds-solid':t.surface,'--ds-well':`${t.bgDeep}70`,'--ds-deep':t.bgDeep,'--ds-line':`${t.brd}24`,'--ds-hair':`${t.brd}18`,'--ds-edge':`${t.brd}55`,
    '--ds-hover':`${t.brd}1f`,'--ds-track':`${t.mut}44`,'--ds-accent-soft':`${t.acc}18`,'--ds-accent-line':`${t.acc}4d`,'--ds-warn-soft':`${t.warm||t.acc}12`,'--ds-warn-line':`${t.warm||t.acc}40`,fontFamily:font};
  const adopt=data=>{const next=loaded(data),dirs=(data.daedalus_exclude_dirs||[]).join(', ');setValues(next);setExclusions(dirs);setSaved(JSON.stringify(payload(next,dirs)));};
  useEffect(()=>{let stopped=false;fetch(`${API}/api/settings`).then(async r=>{
    if(!r.ok)throw new Error('Unable to load coding settings');
    const data=await r.json();if(!stopped)adopt(data);
  }).catch(e=>{if(!stopped)setMessage(e.message);});return()=>{stopped=true;};},[]);
  if(!values)return <div role="status" className="ds-loading" style={vars}>{message||'Loading coding settings…'}</div>;
  const edit=change=>{setMessage('');setValues(change);};
  const set=(key,value)=>edit(v=>({...v,[key]:value}));
  const nested=(group,key,value)=>edit(v=>({...v,[group]:{...v[group],[key]:value}}));
  const body=payload(values,exclusions),dirty=JSON.stringify(body)!==saved;
  const save=async()=>{
    setSaving(true);setMessage('');setFailed(false);
    try{
      const response=await fetch(`${API}/api/settings`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Invalid coding settings');
      adopt(data);onSaved?.(data);
      setMessage(data.pending_context_jobs?.length?'Saved; waiting for worker settings acknowledgement.':'Saved. Active jobs use these values at the next model request.');
    }catch(error){setFailed(true);setMessage(error.message);}finally{setSaving(false);}
  };
  const grid=group=><div className="ds-grid">{groups[group].map(field=><NumberField key={field[0]} field={field} value={values[field[0]]} onChange={set}/>)}</div>;
  // Same arithmetic as context_policy.resolve, so the numbers match what the worker will use.
  const context=body.openhands_num_ctx,headroom=Math.ceil(context*body.context_headroom_percent/100),room=context-body.generation_num_predict-headroom;
  const mode=(values.daedalus_compaction||'inherit')==='inherit'?values.context_compaction:values.daedalus_compaction;
  const compacting=['on','true','1'].includes(String(mode).toLowerCase());
  const persistent=!!values.daedalus_v3_enabled;
  return <div className="ds-root" style={vars}>
    <Section title="Workflow" hint="How new coding jobs run. Jobs already in progress keep the workflow they started with.">
      <div className="ds-toggles">
        <Toggle label="Persistent jobs" chip="Experimental" checked={persistent} onChange={value=>set('daedalus_v3_enabled',value)}
          description="New coding jobs run in the background with checkpoints, Stop and Continue, and recovery after a restart."/>
        <div className="ds-nested">
          <Toggle label="Evidence-gated repairs and edits" chip="Experimental" disabled={!persistent} checked={!!values.daedalus_policy7_edits} onChange={value=>set('daedalus_policy7_edits',value)}
            description="Uploaded projects and follow-up edits are verified with executed tests, an independent audit and a review."/>
          <Toggle label="Evidence-gated new builds" chip="Experimental" disabled={!persistent} checked={!!values.daedalus_policy7_builds} onChange={value=>set('daedalus_policy7_builds',value)}
            description="New projects are built by an agent that compiles and runs the code as it works, then verified the same way."/>
        </div>
      </div>
      <p className="ds-note">{persistent
        ?'Evidence-gated jobs are delivered as Ready for review, or Build incomplete when the result does not run. None is marked Complete yet: that needs trusted proof for each requirement, and generated tests guide repairs but do not count as proof. Review the changes and test coverage before relying on them.'
        :'Evidence gating needs persistent jobs. With persistent jobs off, new coding jobs use the legacy workflow.'}</p>
    </Section>

    <Section title="Context" hint="How much the coding model can read and write in one call.">
      {grid('context')}
      <label className="ds-field"><span className="ds-label">Compaction</span>
        <span className="ds-control"><select aria-label="Daedalus compaction" value={values.daedalus_compaction||'inherit'} onChange={e=>set('daedalus_compaction',e.target.value)}>
          <option value="inherit">Inherit global compaction setting</option><option value="on">Enabled</option><option value="off">Disabled</option>
        </select></span>
      </label>
      {!context?<span className="ds-help">The context window inherits the global setting. Effective values for each stage are listed under Advanced.</span>
        :!(room>0)?<span className="ds-help ds-err">The completion allowance and headroom leave no room for input. Raise the context window or lower the completion allowance.</span>
        :<div className="ds-stats">
          <div className="ds-stat"><b>{room.toLocaleString()}</b><span>Working input per call</span></div>
          <div className="ds-stat"><b>{body.generation_num_predict.toLocaleString()}</b><span>Reserved for output</span></div>
          {compacting?<div className="ds-stat"><b>{Math.floor(room*body.context_compaction_threshold/100).toLocaleString()}</b><span>Compaction starts at</span></div>
            :<div className="ds-stat"><b>{room.toLocaleString()}</b><span>Compaction off · job pauses at</span></div>}
        </div>}
    </Section>

    <Section title="Job budget" hint="How far a job may run before it pauses for you.">{grid('budget')}</Section>

    <Section title="Browser and visual review" hint="Checks for projects that serve a web page.">
      <Toggle label="AI visual review by default" checked={!!values.daedalus_visual_review} onChange={value=>set('daedalus_visual_review',value)}
        description="Screenshots of the running page are reviewed by a local vision model. Each request can still switch this on or off."/>
      <div className="ds-grid">
        <label className="ds-field ds-wide"><span className="ds-label">Local vision model</span>
          <span className="ds-control"><input aria-label="Daedalus local vision model" value={values.daedalus_visual_model||''} onChange={e=>set('daedalus_visual_model',e.target.value)} placeholder="Use coding model if it supports vision"/></span>
          <span className="ds-help">Installed local models only. Unsupported or unavailable vision is reported as skipped.</span>
        </label>
        {groups.browser.map(field=><NumberField key={field[0]} field={field} value={values[field[0]]} onChange={set}/>)}
      </div>
      <div className="ds-field"><span className="ds-label">Browser viewports</span>
        <div className="ds-viewports">{(values.daedalus_browser_viewports||[]).map((viewport,index)=><div key={viewport.name} className="ds-viewport">
          <span className="ds-tag">{viewport.name}</span>
          {['width','height'].map(dimension=><label key={dimension} className="ds-mini">{dimension}
            <input aria-label={`${viewport.name} ${dimension}`} type="number" min="1" value={viewport[dimension]} onChange={e=>edit(v=>({...v,daedalus_browser_viewports:v.daedalus_browser_viewports.map((p,i)=>i===index?{...p,[dimension]:Number(e.target.value)}:p)}))}/>
          </label>)}
          <span>px</span>
        </div>)}</div>
        <span className="ds-help">Page sizes used for browser checks and screenshots.</span>
      </div>
    </Section>

    <Section title="Storage and uploads" hint="Limits for uploaded projects and worker disk use.">
      {grid('storage')}
      <label className="ds-field"><span className="ds-label">Excluded directory names</span>
        <span className="ds-control"><input aria-label="Excluded directory names" value={exclusions} onChange={e=>{setMessage('');setExclusions(e.target.value);}}/></span>
        <span className="ds-help">Comma-separated folder names left out of repository inspection.</span>
      </label>
    </Section>

    <Section title="Advanced" hint="Per-stage overrides. Leave these alone unless one stage needs a different window.">
      <details className="ds-more"><summary><span className="ds-more-title">Stage context overrides and effective values</span><span className="ds-help">Give one stage its own context window, output allowance or thinking mode. Empty fields inherit.</span></summary>
        <div className="ds-more-body">{roles.map(role=>{const resolved=values.resolved_contexts?.[role];return <div key={role} className="ds-stage">
          <div className="ds-stage-name"><strong>{role==='qa'?'QA':role}</strong><span className={resolved?.error?'ds-help ds-err':'ds-help'}>{effective(resolved)}</span></div>
          <label className="ds-mini">Context<input aria-label={`${role} context override`} type="number" min="0" step="1" placeholder="Inherit" value={values.daedalus_role_contexts?.[role]||''} onChange={e=>nested('daedalus_role_contexts',role,e.target.value)}/></label>
          <label className="ds-mini">Output<input aria-label={`${role} output override`} type="number" min="0" step="1" placeholder="Inherit" value={values.daedalus_role_outputs?.[role]||''} onChange={e=>nested('daedalus_role_outputs',role,e.target.value)}/></label>
          <label className="ds-mini">Thinking<select aria-label={`${role} thinking override`} value={values.daedalus_role_thinking?.[role]||'inherit'} onChange={e=>nested('daedalus_role_thinking',role,e.target.value)}>
            {thinkingModes.map(mode=><option key={mode} value={mode}>{mode}</option>)}
          </select></label>
        </div>;})}</div>
      </details>
      <details className="ds-more"><summary><span className="ds-more-title">Helper model contexts</span><span className="ds-help">Context windows for short helper calls outside the coding stages.</span></summary>
        <div className="ds-more-body"><div className="ds-grid">{Object.entries(values.helper_contexts||{}).map(([key,value])=><label key={key} className="ds-mini">{key}
          <input aria-label={`${key} helper context`} type="number" min="1" step="1" value={value} onChange={e=>nested('helper_contexts',key,e.target.value)}/>
        </label>)}</div></div>
      </details>
    </Section>

    <div className={`ds-footer${dirty||message?' is-pinned':''}`}>
      <div role="status" className={`ds-status${message?failed?' is-err':' is-ok':''}`}>{message||(dirty?'Unsaved changes':'')}</div>
      <button type="button" disabled={saving} onClick={save} className={`ds-save${dirty?' is-dirty':''}`}>{saving?'Saving…':'Save coding settings'}</button>
    </div>
  </div>;
}
