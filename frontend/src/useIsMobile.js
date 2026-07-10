import { useEffect, useState } from "react";

// Phones (portrait + small landscape) get the mobile layout; tablets 768+ keep desktop.
// Note: CSS zoom (the uiFontSize wrapper in main.jsx) does not affect matchMedia,
// so this flag is stable across UI font-size settings.
export const MOBILE_QUERY = "(max-width: 640px)";

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

// Height of the visual viewport while the on-screen keyboard is open, else null.
// iOS never resizes the layout viewport for the keyboard — it pans the page,
// which scrolls the header offscreen and clips content. Instead we shrink the
// app root to the visual viewport and cancel the pan, so the composer sits
// directly above the keyboard with the header still visible.
export function useKeyboardViewportHeight(enabled) {
  const [h, setH] = useState(null);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!enabled || !vv) { setH(null); return; }
    let raf = 0;
    const update = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        // >80px gap = keyboard (URL-bar collapse is smaller and handled by dvh)
        const keyboard = window.innerHeight - vv.height > 80;
        setH(keyboard ? Math.round(vv.height) : null);
        if (keyboard && (vv.offsetTop > 0 || window.scrollY > 0)) window.scrollTo(0, 0);
      });
    };
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      cancelAnimationFrame(raf);
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, [enabled]);
  return h;
}
