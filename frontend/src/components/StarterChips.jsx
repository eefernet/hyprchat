import React from 'react';

// Suggested starting points on the empty chat surface. Items: {key,label,icon,color,text|run,hint}.
// Picking a `text` item fills the composer; a `run` item performs its own action.
export default function StarterChips({t,font,items,onPick}){
  if(!items?.length)return null;
  return <div className="hc-starter-chips" role="list" aria-label="Suggested starts" style={{fontFamily:font}}>
    {items.map((it,i)=><button key={it.key||i} type="button" role="listitem" className="hc-starter-chip" style={{'--i':i,'--chip-c':it.color||t.acc}} title={it.hint||undefined} onClick={()=>onPick(it)}>
      {it.icon&&<span className="hc-starter-chip-icon" aria-hidden="true">{it.icon}</span>}<span className="hc-starter-chip-label">{it.label}</span>
    </button>)}
  </div>;
}
