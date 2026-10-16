export const REVIEW_LEVELS=[
  {emoji:'💭',name:'Blurt',desc:'Raw answer, no review',value:0},
  {emoji:'🧠',name:'Ponder',desc:'One self-review pass',value:1},
  {emoji:'🔥',name:'Forge',desc:'Two refinement passes',value:2},
  {emoji:'🌌',name:'Galaxy Brain',desc:'Three self-review passes',value:3},
];
export const reviewLevel=value=>Number.isInteger(value)&&value>=0&&value<=3?value:0;
export function effortChoices(capabilities){
  if(capabilities?.mode!=='native')return REVIEW_LEVELS;
  const values=capabilities.values||[];
  return values.map((value,i)=>{
    const brand=i===0?0:i===values.length-1?3:i===1?1:i===2?2:null;
    const label=typeof value==='boolean'?(value?'on':'off'):value;
    return {...(brand===null?{name:label,emoji:'🧠'}:REVIEW_LEVELS[brand]),value,
      desc:`Native reasoning · ${label}`,isDefault:value===capabilities.default};
  });
}
export function nativeChoice(capabilities,saved){
  const choices=effortChoices(capabilities);
  return choices.find(c=>c.value===saved)||(saved!=null?choices.find(c=>c.isDefault):null)||choices[0];
}
export function effortRequest(model,capabilities,saved,rounds){
  const result={effort_mode:'auto',reasoning_model:model,effort_rounds:reviewLevel(rounds)};
  if(capabilities?.mode==='native')result.reasoning_level=saved??capabilities.values[0];
  return result;
}
