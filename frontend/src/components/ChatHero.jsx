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

export default function ChatHero({t,font,user,tagline,isCouncil,lifted,compact=false,mobile=false}){
  const [now,setNow]=useState(()=>new Date());
  useEffect(()=>{const id=setInterval(()=>setNow(new Date()),60000);return()=>clearInterval(id);},[]);
  const dp=daypartOf(now.getHours()),name=greetableName(user);
  const greeting=dp==="night"?(name?`Up late, ${name}?`:"Up late?"):`Good ${dp}${name?`, ${name}`:""}.`;
  return <div className={`hc-hero${compact?" is-compact":""}`} style={{fontFamily:font,transform:lifted?"translateY(-40px)":"none",...(isCouncil?{"--hc-accent":t.pink}:{}),width:mobile?"100%":undefined}}>
    {!compact&&<div className="hc-hero-mark" aria-hidden="true">{isCouncil?<span style={{fontSize:28}}>⚖️</span>:<><i/><i/></>}</div>}
    <div className="hc-hero-wordmark">{isCouncil?"Ask the council":"HyprChat"}</div>
    {!isCouncil&&<div className="hc-hero-greeting">{greeting}</div>}
    {!compact&&!isCouncil&&<div className="hc-hero-tagline">{tagline}</div>}
  </div>;
}
