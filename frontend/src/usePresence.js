import {useEffect, useRef, useState} from 'react';

// "120ms" -> 120, ".2s" -> 200. NaN for anything else.
export const parseMs = value => {
  const match = String(value || '').trim().match(/^([\d.]+)(ms|s)?$/);
  return match ? parseFloat(match[1]) * (match[2] === 's' ? 1000 : 1) : NaN;
};

// Reads a motion token from :root. motion.css zeroes the tokens under
// prefers-reduced-motion, so closing becomes instant there.
export function motionDuration(token = '--hc-dur-1', fallback = 120) {
  if (typeof document === 'undefined') return fallback;
  const ms = parseMs(getComputedStyle(document.documentElement).getPropertyValue(token));
  return Number.isFinite(ms) ? ms : fallback;
}

// Keeps a closing surface mounted long enough for its exit animation.
// `value` is the open flag, or the data the surface renders from; the last
// truthy value is held while closing. Callers pass the REAL open flag to
// useFocusScope so focus returns to the opener the moment the user closes;
// spread `props` on the closing element so it is inert and hidden from AT.
export default function usePresence(value, {token = '--hc-dur-1', fallback = 120} = {}) {
  const open = !!value;
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);
  const held = useRef(value);
  if (open) held.current = value;
  useEffect(() => {
    if (open) { setMounted(true); setClosing(false); return; }
    if (!mounted) return;
    const ms = motionDuration(token, fallback);
    if (ms <= 0) { setMounted(false); setClosing(false); return; }
    setClosing(true);
    const id = setTimeout(() => { setMounted(false); setClosing(false); }, ms + 20);
    return () => clearTimeout(id);
  }, [open]);
  const isClosing = closing && !open;
  return {
    mounted: mounted || open,
    closing: isClosing,
    state: isClosing ? 'closing' : 'open',
    value: held.current,
    props: isClosing ? {'data-state': 'closing', inert: '', 'aria-hidden': 'true'} : {'data-state': 'open'},
  };
}
