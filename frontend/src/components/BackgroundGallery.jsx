import React, {useEffect, useRef, useState} from 'react';
import BackgroundCanvas from './BackgroundCanvas.jsx';
import {backgroundGroups} from '../backgrounds.js';
import './appearance.css';

// A thumbnail is drawn only once its card has been on screen: twenty canvases are not created for a
// Settings tab nobody opened, and none of them animates (`still`).
function useSeen(ref) {
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    if (seen) return undefined;
    const node = ref.current;
    if (!node) return undefined;
    if (!('IntersectionObserver' in window)) { setSeen(true); return undefined; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setSeen(true); observer.disconnect(); }
    }, {rootMargin: '160px'});
    observer.observe(node);
    return () => observer.disconnect();
  }, [seen]);
  return seen;
}

function Thumbnail({effect, t}) {
  const ref = useRef(null);
  const seen = useSeen(ref);
  let picture = null;
  // Scanlines is a plain element in the app shell, so it is drawn the same way here.
  if (effect === 'scanlines') picture = <span className="bg-fill" style={{backgroundImage: `linear-gradient(180deg,transparent 0%,${t.acc}30 50%,transparent 100%)`, backgroundSize: '100% 4px'}}/>;
  else if (effect === 'off') picture = null;
  else if (seen) picture = <BackgroundCanvas effect={effect} t={t} still strength={1.5}/>;
  return <span ref={ref} className="bg-shot" aria-hidden="true" style={{background: t.bg}}>{picture}</span>;
}

function Card({effect, name, current, onPick, t}) {
  return <button type="button" className="bg-card" aria-pressed={current} title={current ? `${name} (current)` : `Use ${name}`} onClick={() => onPick(effect)}>
    <Thumbnail effect={effect} t={t}/>
    <span className="bg-name">{name}</span>
  </button>;
}

export default function BackgroundGallery({effects, value, onPick, t}) {
  const groups = backgroundGroups(effects);
  const off = (effects || []).find(effect => effect.id === 'off');
  return <div className="bg-root">
    {groups.map(group => <div key={group.id} role="group" aria-label={`${group.name} backgrounds`}>
      <div className="ap-group-name">{group.name}</div>
      <div className="bg-grid">
        {group.items.map(item => <Card key={item.id} effect={item.id} name={item.name} current={value === item.id} onPick={onPick} t={t}/>)}
      </div>
    </div>)}
    {off && <div role="group" aria-label="No background">
      <div className="ap-group-name">None</div>
      <div className="bg-grid"><Card effect="off" name="No background" current={value === 'off'} onPick={onPick} t={t}/></div>
    </div>}
  </div>;
}
