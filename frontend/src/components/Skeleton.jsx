import React from 'react';

// Shimmer loading placeholders. All components take the theme object `t`.
// The keyframe is injected once into document.head (rather than main.jsx's
// global <style> block) so skeletons also work inside portals.
let injected=false;
function inject(){
  if(injected||typeof document==="undefined")return;
  injected=true;
  const s=document.createElement("style");
  s.textContent=`@keyframes hcShimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){.hc-skel{animation:none!important}}`;
  document.head.appendChild(s);
}

export function Skeleton({t,w="100%",h=12,r=6,style}){
  inject();
  return <div className="hc-skel" style={{width:w,height:h,borderRadius:r,background:`linear-gradient(90deg, ${t.surface}66 25%, ${t.sfBri}88 37%, ${t.surface}66 63%)`,backgroundSize:"200% 100%",animation:"hcShimmer 1.4s ease infinite",flexShrink:0,...style}}/>;
}

export function SkeletonText({t,lines=3,style}){
  return <div style={{display:"flex",flexDirection:"column",gap:8,...style}}>
    {Array.from({length:lines},(_,i)=><Skeleton key={i} t={t} h={11} w={i===lines-1?"62%":`${88-(i%3)*7}%`}/>)}
  </div>;
}

export function SkeletonCard({t,h=90,lines=2,style}){
  return <div style={{background:`${t.surface}55`,border:`1px solid ${t.brd}22`,borderRadius:12,padding:14,minHeight:h,display:"flex",flexDirection:"column",gap:10,...style}}>
    <Skeleton t={t} h={14} w="40%"/>
    <SkeletonText t={t} lines={lines}/>
  </div>;
}

export function SkeletonList({t,rows=4,avatar=false,style}){
  return <div style={{display:"flex",flexDirection:"column",gap:14,...style}}>
    {Array.from({length:rows},(_,i)=><div key={i} style={{display:"flex",gap:10,alignItems:"flex-start"}}>
      {avatar&&<Skeleton t={t} w={i%2?28:36} h={i%2?28:36} r="50%"/>}
      <div style={{flex:1,display:"flex",flexDirection:"column",gap:7}}>
        <Skeleton t={t} h={10} w="24%"/>
        <SkeletonText t={t} lines={i%2?1:2}/>
      </div>
    </div>)}
  </div>;
}

export function SkeletonGrid({t,cards=6,minW=250,cardH=120,style}){
  return <div style={{display:"grid",gridTemplateColumns:`repeat(auto-fill,minmax(${minW}px,1fr))`,gap:12,...style}}>
    {Array.from({length:cards},(_,i)=><SkeletonCard key={i} t={t} h={cardH}/>)}
  </div>;
}
