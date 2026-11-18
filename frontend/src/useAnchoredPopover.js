import { useEffect, useState } from 'react';

// Places a portaled popover under (or above) the control that opened it, right edges aligned, and keeps
// it there on resize and scroll. Closes on a press outside. The same placement ActionMenu uses.
export default function useAnchoredPopover(open, trigger, panel, onClose, maxWidth = 360) {
  const [position, setPosition] = useState({ style: {}, up: false });
  useEffect(() => {
    if (!open) return undefined;
    const place = () => {
      const node = trigger.current;
      if (!node) return;
      const r = node.getBoundingClientRect();
      const scale = Number(getComputedStyle(document.documentElement).getPropertyValue('--hc-ui-scale')) || 1;
      const width = Math.min(maxWidth * scale, window.innerWidth - 24);
      const viewport = window.visualViewport, bottom = (viewport?.height || window.innerHeight) + (viewport?.offsetTop || 0);
      const below = bottom - r.bottom - 20, above = r.top - 20, up = below < 260 && above > below;
      setPosition({ up, style: {
        zoom: scale, width: width / scale,
        left: Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12)) / scale,
        ...(up ? { bottom: (window.innerHeight - r.top + 8) / scale } : { top: (r.bottom + 8) / scale }),
        maxHeight: Math.max(120, up ? above : below) / scale,
      } });
    };
    place();
    const outside = event => { if (!panel.current?.contains(event.target) && !trigger.current?.contains(event.target)) onClose(false); };
    const scroll = event => { if (!panel.current?.contains(event.target)) place(); };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', scroll, true);
    window.visualViewport?.addEventListener('resize', place);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', scroll, true);
      window.visualViewport?.removeEventListener('resize', place);
    };
  }, [open]);
  return position;
}
