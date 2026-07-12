import React from 'react';

// Shared designed empty state — icon + title + hint + optional action button.
// (Extracted from hyprChatWidgets.jsx so artifactComponents.jsx can use it
// without creating an import cycle; hyprChatWidgets re-exports it.)
export default function EmptyState({t,font,icon,title,hint,action,compact}){
  return <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:8,textAlign:"center",
    padding:compact?"18px 14px":"36px 20px",border:`1px dashed ${t.brd}44`,borderRadius:10,
    background:`${t.surface}30`,color:t.mut,fontFamily:font}}>
    <div style={{fontSize:compact?22:30,lineHeight:1,opacity:.9,animation:"float 4s ease-in-out infinite",display:"flex",justifyContent:"center"}}>{icon}</div>
    <div style={{fontSize:compact?11:13,fontWeight:800,color:t.dim}}>{title}</div>
    {hint&&<div style={{fontSize:compact?10:11,lineHeight:1.55,maxWidth:420}}>{hint}</div>}
    {action||null}
  </div>;
}
