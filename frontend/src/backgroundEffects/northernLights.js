// Northern Lights: three curtains of light hanging in the upper part of the screen. A curtain is a row
// of thin vertical rays standing on a slowly folding line; each ray is the same gradient strip (green
// at the lower edge, through teal to violet and pink at the top) stretched to its own height and
// brightness, both taken from moving noise, so the curtain ripples and shimmers sideways.
import {auroraColors} from '../backgrounds.js';

const STEP = 4;     // width of one ray in CSS pixels

export function create(env) {
  const {ctx, noise, layer, ox, oy, clampCount, random} = env;
  const pal = auroraColors(env.light);
  let curtains = [], stars = [];

  const strip = index => layer(`aurora-strip:${index}`, 4, 256, (g, w, h) => {
    const grd = g.createLinearGradient(0, 0, 0, h);
    // Curtains differ a little in colour: the nearest is greenest, the farthest has more violet.
    const top = index === 2 ? pal.pink : pal.violet, mid = index === 0 ? pal.teal : pal.violet;
    grd.addColorStop(0, top + '00');
    grd.addColorStop(.3, top + '30');
    grd.addColorStop(.62, mid + '66');
    grd.addColorStop(.86, pal.teal + 'aa');
    grd.addColorStop(.955, pal.green + 'f0');
    grd.addColorStop(1, pal.green + '00');
    g.fillStyle = grd;
    g.fillRect(0, 0, w, h);
  });

  return {
    seed(w, h) {
      curtains = [0, 1, 2].map(i => ({i, base: .4 + i * .1, ph: random() * 40, dir: i % 2 ? -1 : 1, z: .25 + i * .3, gain: .95 - i * .12}));
      stars = env.light ? [] : Array.from({length: clampCount(w * h / 26000, 24, 80)}, () => ({x: random() * w, y: random() * h * .8, a: .12 + random() * .3, tw: random() * 6.283, s: random() > .86 ? 2 : 1}));
    },
    draw(time, w, h) {
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = pal.star;
      for (const star of stars) {
        ctx.globalAlpha = star.a * (.6 + .4 * Math.sin(time * .0013 + star.tw));
        ctx.fillRect(star.x, star.y, star.s, star.s);
      }
      // Light adds up where curtains overlap; on a light page that would only wash out, so they are layered.
      ctx.globalCompositeOperation = env.light ? 'source-over' : 'lighter';
      ctx.imageSmoothingEnabled = true;
      for (const c of curtains) {
        const img = strip(c.i).img, shift = Math.round(ox(c.z)), dy = oy(c.z) * .5, column = Math.floor((img.width || 2) / 2);
        for (let x = -STEP; x < w + STEP; x += STEP) {
          const fold = noise(x * .0016 + c.ph, time * .00005 + c.i * 3);          // where the curtain hangs
          const body = noise(x * .004 - time * .00008 * c.dir, c.i * 5 + 20);      // broad bright and dark parts
          const ray = noise(x * .013 + time * .00036 * c.dir, c.i * 11 + time * .00012);   // the fine shimmer
          const strength = Math.max(0, body * 1.6 - .38) * (.58 + .42 * ray);
          if (strength < .02) continue;
          const edge = h * c.base + (fold - .5) * h * .26 + Math.sin(x * .004 + time * .0002 + c.ph) * h * .03 + dy;
          const height = h * (.2 + .32 * body) * (.78 + .44 * ray);
          ctx.globalAlpha = Math.min(1, strength * c.gain);
          // One middle column of the strip is stretched, and rays sit on whole pixels exactly side by side:
          // light adds up, so any overlap or gap between neighbours shows as a bright or dark seam.
          ctx.drawImage(img, column, 0, 1, img.height || 1, x + shift, edge - height, STEP, height);
        }
      }
      ctx.imageSmoothingEnabled = false;
    },
  };
}
