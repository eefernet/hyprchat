import React, {useId} from 'react';
import './settingRows.css';

// One setting: its name and a line of help on the left, the control on the right. `wide` puts the
// control under the text instead (a long row of choices, a text field). `below` is extra content that
// belongs to the setting and always sits under the row (a dependent choice, a warning).
export default function SettingRow({label, help, children, below, wide = false, disabled = false}) {
  const id = useId();
  return <div className={`sr-row${wide ? ' is-wide' : ''}${disabled ? ' is-disabled' : ''}`} role="group" aria-labelledby={`${id}-name`} aria-describedby={help ? `${id}-help` : undefined}>
    <div className="sr-text">
      <div className="sr-name" id={`${id}-name`}>{label}</div>
      {help ? <div className="sr-help" id={`${id}-help`}>{help}</div> : null}
    </div>
    <div className="sr-control">{children}</div>
    {below ? <div className="sr-below">{below}</div> : null}
  </div>;
}

// Rows of one section card, separated by hairlines.
export function SettingRows({children}) {
  return <div className="sr-list">{children}</div>;
}

// A few exclusive choices drawn as one tray. Options: {v, label, title}.
export function Segmented({options, value, onChange, label, color}) {
  return <div className="sr-seg" role="radiogroup" aria-label={label} style={color ? {'--seg-c': color} : undefined}>
    {options.map(option => {
      const active = value === option.v;
      return <button key={String(option.v)} type="button" role="radio" aria-checked={active} title={option.title} className="sr-seg-option" onClick={() => onChange(option.v)}>{option.label}</button>;
    })}
  </div>;
}
