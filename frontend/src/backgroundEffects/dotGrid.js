// Dot Grid: a regular grid of small dots that is quietly alive. The still grid is painted once and
// copied every frame; on top of it only the dots that are lit are drawn again:
//   - a slow band of light crossing the grid diagonally every few seconds,
//   - a soft spotlight around the pointer, where dots grow and take the second accent colour,
//   - now and then a ripple spreading from a random point.
// With motion reduced, and in the picker's thumbnails, it is just the still grid.

export const GAP = 24;            // distance between dots
const BAND = 150;                 // half-width of the travelling band of light
const WAVE_MS = 9000, CROSS_MS = 5600;   // a band starts every WAVE_MS and takes CROSS_MS to cross
const SPOT = 150;                 // radius of the pointer's spotlight
const RING = 22;                  // thickness of a ripple

// Where the band is at `time`: its position along x + y, or null between two crossings.
export function bandAt(time, w, h) {
  const into = ((time % WAVE_MS) + WAVE_MS) % WAVE_MS;
  if (into > CROSS_MS) return null;
  return -BAND + (into / CROSS_MS) * (w + h + 2 * BAND);
}

export function create(env) {
  const {ctx, t, layer, pointer, random} = env;
  const ink = env.light ? t.text : t.acc, hot = t.acc2 || t.warm || t.acc;
  let ripples = [], rippleIn = 2600;

  const lit = (x, y, glow, colour) => {
    ctx.fillStyle = colour;
    ctx.globalAlpha = Math.min(.95, .25 + glow * .7);
    const r = 1 + glow * 1.5;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283); ctx.fill();
  };

  return {
    seed() { ripples = []; rippleIn = 2600; },
    draw(time, w, h, dtf) {
      ctx.clearRect(0, 0, w, h);
      const grid = layer(`dots:${Math.round(w)}x${Math.round(h)}:${ink}`, w, h, (g, lw, lh) => {
        g.fillStyle = ink;
        g.globalAlpha = env.light ? .3 : .32;
        g.beginPath();
        for (let y = GAP / 2; y < lh + GAP; y += GAP) for (let x = GAP / 2; x < lw + GAP; x += GAP) { g.moveTo(x + 1, y); g.arc(x, y, 1, 0, 6.283); }
        g.fill();
      });
      ctx.globalAlpha = 1;
      ctx.drawImage(grid.img, 0, 0, w, h);
      if (env.still) return;
      const first = GAP / 2, snapUp = v => first + Math.ceil((v - first) / GAP) * GAP;

      // The travelling band: on each row only the dots inside the band are visited.
      const band = bandAt(time, w, h);
      if (band !== null) for (let y = first; y < h + GAP; y += GAP) {
        for (let x = snapUp(Math.max(0, band - y - BAND)); x <= Math.min(w, band - y + BAND); x += GAP) {
          const near = 1 - Math.abs(x + y - band) / BAND;
          if (near > 0) lit(x, y, near * near * .75, ink);
        }
      }

      // The pointer's spotlight.
      if (pointer.seen) for (let y = snapUp(pointer.y - SPOT); y <= pointer.y + SPOT; y += GAP) {
        for (let x = snapUp(pointer.x - SPOT); x <= pointer.x + SPOT; x += GAP) {
          const near = 1 - Math.hypot(x - pointer.x, y - pointer.y) / SPOT;
          if (near > 0) lit(x, y, near * near, hot);
        }
      }

      // Ripples.
      rippleIn -= 16.667 * dtf;
      if (rippleIn <= 0 && ripples.length < 3) {
        ripples.push({x: first + Math.floor(random() * (w / GAP)) * GAP, y: first + Math.floor(random() * (h / GAP)) * GAP, r: 0, max: 220 + random() * 160});
        rippleIn = 3800 + random() * 4200;
      }
      for (let i = ripples.length - 1; i >= 0; i--) {
        const rp = ripples[i];
        rp.r += 1.5 * dtf;
        if (rp.r > rp.max) { ripples.splice(i, 1); continue; }
        const fade = 1 - rp.r / rp.max, reach = rp.r + RING;
        for (let y = snapUp(rp.y - reach); y <= rp.y + reach; y += GAP) {
          for (let x = snapUp(rp.x - reach); x <= rp.x + reach; x += GAP) {
            const off = Math.abs(Math.hypot(x - rp.x, y - rp.y) - rp.r);
            if (off < RING) lit(x, y, (1 - off / RING) * fade, ink);
          }
        }
      }
      ctx.globalAlpha = 1;
    },
  };
}
