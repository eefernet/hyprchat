import React,{useState,useEffect} from 'react';

// Empty-chat hero — logo, gradient wordmark, time-aware greeting, and the
// daily AI tagline. Extracted from main.jsx so the hero owns its own layout
// and animations. Also renders the council variant of the empty state.

export const daypartOf=h=>h>=5&&h<12?"morning":h>=12&&h<17?"afternoon":h>=17&&h<22?"evening":"night";

// First name for greetings — skips the generic seed profile names.
export const greetableName=user=>{
  const n=String(user?.name||"").trim();
  return n&&!/^(main|default|user)$/i.test(n)?n.split(/\s+/)[0]:"";
};

const HERO_CSS=`
@keyframes hcHeroRise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@media (prefers-reduced-motion:reduce){.hc-hero,.hc-hero *{animation:none !important}}
`;

const rise=d=>({animation:`hcHeroRise .5s cubic-bezier(.22,.9,.28,1) ${d}s both`});

export default function ChatHero({t,font,user,tagline,isCouncil,lifted}){
  const [now,setNow]=useState(()=>new Date());
  useEffect(()=>{const id=setInterval(()=>setNow(new Date()),60000);return()=>clearInterval(id);},[]);
  const dp=daypartOf(now.getHours());
  const name=greetableName(user);
  const greeting=dp==="night"
    ?(name?`Up late, ${name}?`:"Up late?")
    :`Good ${dp}${name?`, ${name}`:""}.`;
  const rootS={display:"flex",flexDirection:"column",alignItems:"center",gap:14,fontFamily:font,
    transform:lifted?"translateY(-40px)":"none",transition:"transform .35s ease"};
  if(isCouncil)return <div className="hc-hero" style={rootS}>
    <style>{HERO_CSS}</style>
    <div style={{...rise(0)}}><div style={{fontSize:36,animation:"float 4s ease-in-out infinite"}}>⚖️</div></div>
    <div style={{fontSize:12,color:t.mut,letterSpacing:1,...rise(.12)}}>Ask the council</div>
  </div>;
  return <div className="hc-hero" style={rootS}>
    <style>{HERO_CSS}</style>
    {/* Logo mark with a slow orbital accent ring behind it */}
    <div style={{position:"relative",width:68,height:68,...rise(0)}}>
      <div style={{position:"absolute",left:-9,top:-9,width:86,height:86,borderRadius:"50%",
        background:`conic-gradient(from 0deg, transparent 0 68%, ${t.acc}55 84%, ${t.acc2}33 94%, transparent)`,
        filter:"blur(5px)",opacity:.85,animation:"spin 14s linear infinite"}}/>
      <div style={{position:"relative",width:68,height:68,animation:"float 6s ease-in-out infinite"}}>
        <div style={{width:68,height:68,borderRadius:18,background:t.bgDeep,border:`1px solid ${t.brd}77`,position:"relative",overflow:"hidden",
          boxShadow:`0 10px 40px ${t.acc}1c, 0 0 26px ${t.acc}14`,display:"flex",alignItems:"center",justifyContent:"center"}}>
          <div style={{position:"absolute",inset:6,borderRadius:13,border:`1px solid ${t.brd}33`,background:`${t.surface}55`}}/>
          <div style={{position:"absolute",left:23,top:18,width:10,height:32,borderRadius:2,background:t.acc,opacity:.95}}/>
          <div style={{position:"absolute",right:23,bottom:18,width:10,height:32,borderRadius:2,background:t.warm,opacity:.9}}/>
        </div>
      </div>
    </div>
    <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:8}}>
      <div style={{fontSize:34,fontWeight:800,letterSpacing:1.2,lineHeight:1,
        backgroundImage:`linear-gradient(115deg, ${t.text}, ${t.acc} 45%, ${t.acc2})`,
        WebkitBackgroundClip:"text",backgroundClip:"text",color:"transparent",
        filter:`drop-shadow(0 2px 16px ${t.acc}22)`,...rise(.08)}}>HyprChat</div>
      <div style={{fontSize:15.5,fontWeight:650,color:t.text,letterSpacing:.3,...rise(.16)}}>{greeting}</div>
      <div style={{width:150,height:1,background:`linear-gradient(90deg,transparent,${t.acc}88,${t.acc2}66,transparent)`,...rise(.22)}}/>
      <div key={tagline} style={{fontSize:12.5,color:t.dim,letterSpacing:.5,textAlign:"center",maxWidth:460,lineHeight:1.65,...rise(.3)}}>{tagline}</div>
    </div>
  </div>;
}
