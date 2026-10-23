import React from 'react';

// Presentational track; the owning element carries role="switch" + aria-checked.
export function SwitchTrack({on, color}) {
  return <span aria-hidden="true" className="hc-switch" data-on={on ? 'true' : 'false'} style={color ? {'--switch-c': color} : undefined}/>;
}

// Accessible switch: keyboard-operable, announced with its label.
export default function Switch({on, onChange, label, color, disabled = false, children}) {
  return <button type="button" role="switch" aria-checked={!!on} aria-label={children ? undefined : label} title={label} disabled={disabled} className="hc-switch-btn" onClick={onChange}>
    <SwitchTrack on={on} color={color}/>
    {children}
  </button>;
}
