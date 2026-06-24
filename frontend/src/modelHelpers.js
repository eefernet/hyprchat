// Model metadata and research markdown helpers.
export const cloudModelProvider=(m)=>{
  const s=String(m||"");
  if(s.startsWith("openai:"))return"openai";
  if(s.startsWith("anthropic:"))return"anthropic";
  return"";
};
export const isCloudModelName=(m)=>!!cloudModelProvider(m);
export const cloudModelName=(m)=>String(m||"").replace(/^(openai|anthropic):/,"");
export const modelInfo=(modelDetails,m)=>modelDetails?.[m]||{};
export const modelDetail=(modelDetails,m)=>modelInfo(modelDetails,m).details||{};
export const modelFamilies=(modelDetails,m)=>{
  const d=modelDetail(modelDetails,m);
  return [d.family,...(Array.isArray(d.families)?d.families:[])].filter(Boolean).map(x=>String(x).toLowerCase());
};
export const modelContextLength=(modelDetails,m)=>{
  const d=modelDetail(modelDetails,m);
  const n=Number(d.context_length||d.num_ctx||d.context||0);
  return Number.isFinite(n)&&n>0?n:0;
};
export const formatModelCtx=n=>{
  const v=Number(n)||0;
  if(!v)return"";
  if(v>=1000000)return`${Math.round(v/100000)/10}M`;
  return`${Math.round(v/1000)}K`;
};
export const isMoeModelName=(m,modelDetails={})=>{
  const s=String(m||"").toLowerCase();
  const fam=modelFamilies(modelDetails,m).join(" ");
  return /\bmoe\b|a\d+b|qwen\d*moe|qwen3moe|qwen35moe|mixtral/.test(`${s} ${fam}`);
};
export const isEmbeddingModelName=(m,modelDetails={})=>{
  const s=String(m||"").toLowerCase();
  const d=modelDetail(modelDetails,m);
  const fam=modelFamilies(modelDetails,m).join(" ");
  return /embed|embedding|nomic-bert/.test(`${s} ${fam}`)||Number(d.embedding_length||0)>0&&/embed|nomic/.test(s);
};
export const researchModelOptions=(models,modelDetails={})=>(models||[]).filter(m=>m===""||!isEmbeddingModelName(m,modelDetails));
export const cleanResearchMarkdown=text=>{
  let s=String(text||"");
  if(!s)return"";
  s=s.replace(/<think\b[^>]*>[\s\S]*?<\/think\s*>/gi,"");
  const lower=s.toLowerCase();
  const close=lower.lastIndexOf("</think>");
  if(close>=0)s=s.slice(close+"</think>".length);
  s=s.replace(/<think\b[^>]*>/gi,"").replace(/<\/think\s*>/gi,"");
  s=s.replace(/^\s*thought\s*\n[\s\S]*?(?:<\|channel\|>|<channel\|>|<\|message\|>)/i,"");
  return s.trim();
};

