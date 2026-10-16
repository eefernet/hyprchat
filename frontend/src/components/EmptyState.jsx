import React from 'react';

export default function EmptyState({icon,title,hint,action,compact}) {
  return <div className={`hc-empty${compact?' is-compact':''}`}>
    <div className="hc-empty-icon" aria-hidden="true" style={{fontSize:compact?24:32,lineHeight:1,display:'flex'}}>{icon}</div>
    <div className="hc-empty-title">{title}</div>
    {hint&&<div className="hc-empty-hint">{hint}</div>}
    {action||null}
  </div>;
}
