import React from 'react';
import './storage.css';

// Settings → Storage & Cleanup, first card: the files chats delivered, kept on the HyprChat server in
// outputs/<month>/<chat>/. Numbers and wording come from storageSummary.js (`view`).
export default function LocalFilesCard({view,intervals,days,onDays,onRefresh,onClean,loading,saving,cleaning,error,result,folder,reasons=[],t,btnS}){
  return <section className="sto-card" aria-labelledby="local-storage-title">
    <div className="sto-head">
      <div>
        <h3 id="local-storage-title" className="sto-title">Files on HyprChat</h3>
        <p className="sto-sub">Files your chats delivered, sorted by month and chat.</p>
      </div>
      <div className="sto-figure" aria-live="polite">{view.headline}{view.folders!=null&&<small>{view.folders} chat folder{view.folders===1?'':'s'}</small>}</div>
    </div>
    {view.where.length>0&&<div className="sto-chips" aria-label="Where the files are">
      {view.where.map(row=><span key={row.label} className="sto-chip" title={row.size}>{row.label}<b>{row.files}</b></span>)}
    </div>}
    {view.months.length>0&&<div className="sto-rows" aria-label="Files by month">
      {view.months.map(row=><div key={row.key} className="sto-row"><span>{row.label}</span><span>{row.files} file{row.files===1?'':'s'} · {row.size}</span></div>)}
    </div>}
    <div>
      <div className="sto-label" id="local-retention-label">Remove temporary files after</div>
      <div className="sto-options" role="group" aria-labelledby="local-retention-label">
        {intervals.map(option=><button key={option.days} onClick={()=>onDays(option.days)} disabled={saving||loading||!view.known} aria-pressed={days===option.days}
          style={{...btnS(days===option.days?t.acc:t.mut),background:days===option.days?`${t.acc}22`:`${t.surface}55`,fontWeight:days===option.days?700:400,fontSize:'var(--hc-text-control)'}}>{option.label}</button>)}
      </div>
    </div>
    <div className="sto-actions">
      <button onClick={onClean} disabled={cleaning||loading||!!error||!view.canClean} style={btnS(t.warm)}>{cleaning?'Cleaning…':'Clean now'}</button>
      <button onClick={onRefresh} disabled={loading||cleaning||saving} style={btnS(t.acc)}>{loading?'Refreshing…':'Refresh'}</button>
      {view.cleanLine&&<p className="sto-note" aria-live="polite">{view.cleanLine}</p>}
    </div>
    {error&&<p role="alert" className="sto-note" data-tone="bad">Could not load: {error}</p>}
    {result&&<p role={result.error?'alert':'status'} className="sto-note" data-tone={result.error?'bad':'ok'}>{result.error||`Removed ${result.deleted||0} temporary file${result.deleted===1?'':'s'}.`}</p>}
    <details className="sto-details">
      <summary>Details</summary>
      <p>Saved files, uploads, knowledge bases and chat history are never removed by cleanup. Only temporary files no longer in use are.</p>
      <p>Folder: <code>{folder||'Not loaded'}</code></p>
      {reasons.map(([reason,count])=><div key={reason}>{reason}: {count}</div>)}
    </details>
  </section>;
}
