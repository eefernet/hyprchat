// Pure helpers for the composer tools popover (components/ComposerToolsMenu.jsx).

// Splits the merged tool list into built-in/custom tools and connector
// operations, and reports which enabled ids still name a known tool.
export function splitComposerTools(allTools,activeToolIds){
  const list=Array.isArray(allTools)?allTools.filter(tl=>tl&&tl.id):[];
  const active=new Set(Array.isArray(activeToolIds)?activeToolIds:[]);
  const on=list.filter(tl=>active.has(tl.id));
  return{
    tools:list.filter(tl=>!tl.connector),
    connectors:list.filter(tl=>tl.connector),
    activeCount:on.length,
    activeNames:on.map(tl=>String(tl.name||tl.id)),
  };
}

export function filterConnectorTools(list,query){
  const q=String(query||"").trim().toLowerCase();
  const rows=Array.isArray(list)?list:[];
  if(!q)return rows;
  return rows.filter(tl=>String(tl.name||"").toLowerCase().includes(q)||String(tl.description||"").toLowerCase().includes(q)||String(tl.id||"").toLowerCase().includes(q));
}

// "GitHub: list issues" -> "list issues"; the connector name is dropped only
// when it is a short prefix.
export function connectorLabel(name){
  return String(name||"").replace(/^([^:]{1,28}):\s*/,"");
}

export function toolsTitle(activeNames,max=6){
  const names=Array.isArray(activeNames)?activeNames:[];
  if(!names.length)return"Tools (none on)";
  const shown=names.slice(0,max).join(", ");
  return`Tools: ${shown}${names.length>max?` +${names.length-max} more`:""}`;
}
