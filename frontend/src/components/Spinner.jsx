import React from 'react';

// Loading indicators styled by motion.css (.hc-spinner / .hc-dots); both
// inherit currentColor and announce as status for screen readers.
export function Spinner({size = 14, style}) {
  return <span className="hc-spinner" role="status" aria-label="Loading" style={{width: size, height: size, ...style}}/>;
}

export function Dots({style}) {
  return <span className="hc-dots" role="status" aria-label="Loading" style={style}><i/><i/><i/></span>;
}
