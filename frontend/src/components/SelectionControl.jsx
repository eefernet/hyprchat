import React from 'react';
import ActionMenu from './ActionMenu.jsx';

// Shares the portaled action menu's placement, focus restoration and keyboard controls.
export default function SelectionControl({label,value,options,onChange,disabled=false}) {
  const selected=options.find(option=>option.value===value);
  return <ActionMenu label={label} triggerText={selected?.label||'Choose…'} disabled={disabled}
    items={options.map(option=>({label:option.label,role:'menuitemradio',checked:option.value===value,
      disabled:option.disabled,onClick:()=>onChange(option.value)}))}/>;
}
