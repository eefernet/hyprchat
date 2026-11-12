import React,{useEffect,useMemo,useState} from 'react';
import {playfulPhase,activityVerbOrder} from '../chatActivityVisual.js';

// The playful rotating status word. Lives in its own module so the Daedalus card
// can use it without importing hyprChatWidgets (which imports the card).
export const PlayfulStatusContext=React.createContext(true);

// Only this label ticks; tokens and historical markdown do not rerender with it.
export function ActivityLabel({label,phase,active=false,seed=''}){
  const enabled=React.useContext(PlayfulStatusContext);
  const pool=playfulPhase(label,phase);
  const words=useMemo(()=>activityVerbOrder(pool,seed),[pool,seed]);
  const [tick,setTick]=useState(0);
  useEffect(()=>{
    setTick(0);
    if(!enabled||!active||!pool)return;
    const media=window.matchMedia('(prefers-reduced-motion: reduce)');
    let timer;
    const update=()=>{
      clearInterval(timer);
      if(media.matches)setTick(0);
      if(!document.hidden&&!media.matches)timer=setInterval(()=>setTick(n=>n+1),3000);
    };
    document.addEventListener('visibilitychange',update);media.addEventListener('change',update);update();
    return()=>{clearInterval(timer);document.removeEventListener('visibilitychange',update);media.removeEventListener('change',update);};
  },[enabled,active,pool,seed]);
  const fun=enabled&&active&&pool;
  const word=fun?words[tick%words.length]:label;
  return <span className={`hc-activity-label${fun?' is-playful':''}`} title={label}>
    <span key={word} className="hc-activity-word" aria-hidden="true">{word}</span>
    <span className="hc-sr-only">{label}</span>
  </span>;
}
