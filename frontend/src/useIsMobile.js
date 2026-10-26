import { useEffect, useLayoutEffect, useState } from "react";

// Narrow screens and short touch-screen landscape views use the mobile layout.
// Note: CSS zoom (the uiFontSize wrapper in main.jsx) does not affect matchMedia,
// so this flag is stable across UI font-size settings.
export const MOBILE_QUERY = "(max-width: 640px), (pointer: coarse) and (max-width: 1024px) and (max-height: 500px)";

// For useState lazy initializers where the hook value isn't available yet.
export const isMobileNow = () =>
  typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;

export default function useIsMobile() {
  const [m, setM] = useState(isMobileNow);
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const fn = e => setM(e.matches);
    mq.addEventListener("change", fn);
    return () => mq.removeEventListener("change", fn);
  }, []);
  return m;
}

// One observer owns the app AND body portals. Follow iOS's visual viewport
// instead of fighting its focus pan with repeated window.scrollTo calls.
// Installed apps use the measured height even at rest: their usable viewport
// can be smaller than 100vh. Browser tabs keep CSS dynamic viewport sizing.
// The closed height also detects Android's resizes-content keyboard behavior.
export function observeMobileViewport(win, onChange) {
  const vv = win.visualViewport;
  const standaloneQuery = win.matchMedia?.("(display-mode: standalone)");
  let closedHeight = win.innerHeight, width = win.innerWidth;
  let keyboardOpen = false, raf = 0, disposed = false;
  const measure = () => {
    if (disposed || (vv && Math.abs(vv.scale - 1) > .01)) return;
    if (width !== win.innerWidth) {
      width = win.innerWidth;
      closedHeight = win.innerHeight;
    }
    const el = win.document.activeElement;
    const editable = !!el && (el.isContentEditable || el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && !/^(button|checkbox|radio|range|submit|reset|file|color|hidden)$/i.test(el.type)));
    const height = vv?.height || win.innerHeight;
    closedHeight = Math.max(closedHeight, win.innerHeight);
    keyboardOpen = (editable || keyboardOpen) && closedHeight - height > 80;
    if (!keyboardOpen) closedHeight = win.innerHeight;
    const standalone = standaloneQuery?.matches || win.navigator?.standalone === true;
    onChange({height: keyboardOpen || standalone ? height : null, offsetTop: keyboardOpen ? Math.max(0, vv?.offsetTop || 0) : 0, keyboardOpen});
  };
  const schedule = () => {
    win.cancelAnimationFrame(raf);
    raf = win.requestAnimationFrame(measure);
  };
  const events = [[win,"resize"],[win,"orientationchange"],[win,"pageshow"],
    [win.document,"focusin"],[win.document,"focusout"],[win.document,"visibilitychange"]];
  if (vv) events.push([vv,"resize"],[vv,"scroll"]);
  if (standaloneQuery) events.push([standaloneQuery,"change"]);
  events.forEach(([target,event]) => target.addEventListener(event,schedule));
  measure();
  return () => {
    disposed = true;
    win.cancelAnimationFrame(raf);
    events.forEach(([target,event]) => target.removeEventListener(event,schedule));
  };
}

export function useMobileViewport(enabled) {
  const [keyboardOpen, setKeyboardOpen] = useState(false);
  useLayoutEffect(() => {
    const root = document.documentElement;
    if (!enabled) { setKeyboardOpen(false); return; }
    root.dataset.hcMobile = "true";
    const stop = observeMobileViewport(window, ({height,offsetTop,keyboardOpen:open}) => {
      root.style.setProperty("--hc-viewport-height", height == null ? "var(--hc-screen-height)" : `${height}px`);
      root.style.setProperty("--hc-viewport-top", `${offsetTop}px`);
      root.style.setProperty("--hc-safe-bottom", open ? "0px" : "env(safe-area-inset-bottom, 0px)");
      root.dataset.hcKeyboard = String(open);
      setKeyboardOpen(open);
    });
    return () => {
      stop();
      delete root.dataset.hcMobile;
      delete root.dataset.hcKeyboard;
      ["--hc-viewport-height","--hc-viewport-top","--hc-safe-bottom"].forEach(key => root.style.removeProperty(key));
    };
  }, [enabled]);
  return keyboardOpen;
}
