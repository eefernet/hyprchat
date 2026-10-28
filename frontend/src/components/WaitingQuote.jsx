import React,{useEffect,useState} from 'react';

// Shows a rotating loading quote once a reply has been pending for `delayMs`
// with no first token (cold model load, long retrieval). Pass a STABLE quotes
// array (state, not an inline literal) or the timer restarts every render.
export default function WaitingQuote({active,quotes,t,delayMs=4000,everyMs=9000}){
  const [idx,setIdx]=useState(-1);
  useEffect(()=>{
    if(!active||!quotes?.length){setIdx(-1);return;}
    let i=Math.floor(Math.random()*quotes.length), tick=0;
    const start=setTimeout(()=>{setIdx(i);tick=setInterval(()=>{i=(i+1)%quotes.length;setIdx(i);},everyMs);},delayMs);
    return()=>{clearTimeout(start);if(tick)clearInterval(tick);};
  },[active,quotes,delayMs,everyMs]);
  if(!active||idx<0||!quotes?.[idx])return null;
  const q=quotes[idx];
  return <div key={idx} className="hc-waiting-quote hc-reveal" role="status" aria-live="polite" style={{color:t.mut}}>
    <span className="hc-waiting-quote-text">“{q.t}”</span>
    {q.a&&<span className="hc-waiting-quote-author">— {q.a}</span>}
  </div>;
}
