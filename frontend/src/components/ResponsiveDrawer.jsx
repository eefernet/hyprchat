import useFocusScope from '../useFocusScope.js';
import React from 'react';
import {createPortal} from 'react-dom';

// On wide layouts the same content is inline; narrow layouts use a modal drawer.
export default function ResponsiveDrawer({inline,open,onClose,title,children}) {
  const ref=useFocusScope(!inline&&open,onClose);
  if(inline)return children;
  if(!open)return null;
  return createPortal(<div className="hc-drawer-backdrop hc-viewport-overlay" onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section ref={ref} className="hc-drawer" role="dialog" aria-modal="true" aria-label={title} style={{zoom:'var(--hc-ui-scale,1)'}}>
      <div className="hc-drawer-heading"><h2>{title}</h2><button className="hc-button" onClick={onClose} aria-label={`Close ${title}`}>Close</button></div>
      {children}
    </section>
  </div>,document.body);
}
