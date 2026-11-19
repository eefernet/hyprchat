import React from 'react';
import {isLightTheme} from '../elevation.js';
import './appearance.css';

// The theme picker: one small preview card per theme, dark ones first. A click applies the theme at
// once; hovering or focusing a card only asks the caller to preview it (`onPreview(key | null)`).
function ThemeCard({id, palette, current, onPick, onPreview}) {
  return <button type="button" className="hc-theme-choice tg-card" aria-pressed={current} title={current ? `${palette.name} (current)` : `Use ${palette.name}`}
    onClick={() => onPick(id)} onMouseEnter={() => onPreview?.(id)} onMouseLeave={() => onPreview?.(null)} onFocus={() => onPreview?.(id)} onBlur={() => onPreview?.(null)}>
    <span className="tg-shot" aria-hidden="true" style={{background: palette.bg, borderColor: palette.brd}}>
      <span className="tg-side" style={{background: palette.bgDeep}}>
        <i style={{background: palette.acc}}/><i style={{background: palette.mut}}/><i style={{background: palette.mut}}/>
      </span>
      <span className="tg-body">
        <i className="tg-bubble" style={{background: palette.acc}}/>
        <i className="tg-line" style={{background: palette.text, width: '78%'}}/>
        <i className="tg-line" style={{background: palette.dim, width: '52%'}}/>
        <span className="tg-code" style={{background: palette.bgDeep}}>
          <i style={{background: palette.acc2 || palette.acc}}/><i style={{background: palette.warm}}/><i style={{background: palette.pink || palette.acc}}/>
        </span>
      </span>
    </span>
    <span className="tg-name"><span>{palette.name}</span>{current && <span className="tg-current">Current</span>}</span>
  </button>;
}

export default function ThemeGallery({themes, current, onPick, onPreview}) {
  const entries = Object.entries(themes);
  const groups = [
    {name: 'Dark', items: entries.filter(([, palette]) => !isLightTheme(palette))},
    {name: 'Light', items: entries.filter(([, palette]) => isLightTheme(palette))},
  ].filter(group => group.items.length);
  return <div className="tg-root">
    {groups.map(group => <div key={group.name} role="group" aria-label={`${group.name} themes`}>
      <div className="ap-group-name">{group.name}</div>
      <div className="hc-theme-grid tg-grid">
        {group.items.map(([id, palette]) => <ThemeCard key={id} id={id} palette={palette} current={id === current} onPick={onPick} onPreview={onPreview}/>)}
      </div>
    </div>)}
  </div>;
}
