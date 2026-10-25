import React from 'react';
import ReactDOM from 'react-dom';
import useFocusScope from '../useFocusScope.js';
import usePresence from '../usePresence.js';
import {IC} from './icons.jsx';

const isMac=typeof navigator!=='undefined'&&/Mac|iPhone|iPad/.test(navigator.platform||navigator.userAgent||'');
export const MOD_KEY=isMac?'⌘':'Ctrl';
export const SHORTCUTS=[
  [`${MOD_KEY} K`,'Search messages'],
  [`${MOD_KEY} N`,'New chat'],
  [`${MOD_KEY} /`,'Show or hide the sidebar'],
  ['Enter','Send the message'],
  ['Shift Enter','New line in the composer'],
  ['/','Open the prompt library from an empty composer'],
  ['Esc','Close menus, dialogs and search'],
  ['?','Show this sheet'],
];

// Small keyboard-shortcut reference. Opens with `?` (outside inputs); Escape closes via the focus scope.
export default function ShortcutSheet({open,onClose,t,font}){
  const presence=usePresence(open,{token:'--hc-dur-2',fallback:180});
  const focus=useFocusScope(open,onClose);
  if(!presence.mounted)return null;
  return ReactDOM.createPortal(<div className="hc-viewport-overlay hc-scrim" {...presence.props} style={{position:'fixed',inset:0,zIndex:120,display:'flex',alignItems:'center',justifyContent:'center',fontFamily:font,color:t.text}} onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <div ref={focus} role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" tabIndex={-1} className="hc-dialog hc-dialog-card hc-elev-3" style={{zoom:'var(--hc-ui-scale,1)',width:'min(440px,calc((100vw - 24px) / var(--hc-ui-scale,1)))',maxHeight:'calc((var(--hc-viewport-height) - 36px) / var(--hc-ui-scale,1))',padding:0,display:'flex',flexDirection:'column',overflow:'hidden'}}>
      <div className="hc-dialog-head"><h3>Keyboard shortcuts</h3><button aria-label="Close" data-tip="Close" className="hc-icon-btn is-lg tip-below" onClick={onClose} style={{color:t.mut}}><IC.X/></button></div>
      <div className="hc-dialog-body">
        <dl className="hc-shortcut-list">
          {SHORTCUTS.map(([keys,label])=><div key={keys} className="hc-shortcut-row"><dt>{keys.split(' ').map(k=><kbd key={k} className="hc-kbd">{k}</kbd>)}</dt><dd>{label}</dd></div>)}
        </dl>
      </div>
    </div>
  </div>,document.body);
}
