import React from 'react';

// `eyebrow` renders above the title (e.g. a breadcrumb <nav>).
export default function PanelHeader({ t, color, icon, title, subtitle, nav, children, embedded=false, eyebrow=null }) {
  return <header className={embedded?"hc-toolbar hc-embedded-toolbar":"hc-panel-header"}>
    {!embedded&&<div className="hc-panel-heading">
      <span aria-hidden="true" style={{display:'flex',flexShrink:0,color:color||t.acc}}>{icon}</span>
      <div>{eyebrow}<h1 className="hc-panel-title">{title}</h1>
        {subtitle&&<p className="hc-panel-subtitle">{subtitle}</p>}
      </div>
    </div>
    }{nav&&<div className="hc-panel-nav">{nav}</div>}
    {children&&<div className="hc-panel-actions">{children}</div>}
  </header>;
}
