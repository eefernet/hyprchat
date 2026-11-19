// Snowfall: flakes at three depths. Far flakes are small, dim and slow; near ones are large, soft and
// fast. Each sways on its own rhythm, the wind slowly changes direction, and a low bank of snow lies
// along the bottom edge.
import {festiveColors} from '../backgrounds.js';

// [depth, share of all flakes, smallest radius, largest radius]
const LAYERS = [[.3, .5, .8, 1.5], [.6, .32, 1.5, 2.5], [1, .18, 2.6, 4.2]];

export function makeFlake(w, h, random, top = false) {
  const pick = random();
  const [z, , small, large] = pick < LAYERS[0][1] ? LAYERS[0] : pick < LAYERS[0][1] + LAYERS[1][1] ? LAYERS[1] : LAYERS[2];
  return {x: random() * w, y: top ? -8 - random() * 30 : random() * h, z, r: small + random() * (large - small), v: (.28 + z * .95) * (.7 + random() * .6), ph: random() * 6.283, sw: .6 + random() * .9, a: .3 + z * .5};
}

// Shared with Silent Night, which has lighter snow over its trees.
export function drawFlakes(env, flakes, colour, time, w, h, dtf, amount = 1) {
  const {ctx, drawGlow, ox, oy, noise, random} = env;
  const wind = (noise(time * .00006, 3.7) - .5) * 1.8 + Math.sin(time * .00011) * .35;
  ctx.fillStyle = colour;
  for (let i = 0; i < flakes.length; i++) {
    const f = flakes[i];
    f.y += f.v * dtf;
    f.x += (wind * f.z + Math.sin(time * .001 * f.sw + f.ph) * .4 * f.z) * dtf;
    if (f.y > h + 8) Object.assign(f, makeFlake(w, h, random, true));
    if (f.x < -12) f.x += w + 24; else if (f.x > w + 12) f.x -= w + 24;
    const x = f.x + ox(f.z), y = f.y + oy(f.z), alpha = f.a * amount;
    if (f.z === 1) {
      drawGlow(colour, f.r * 2.4, x, y, alpha * .7);        // near flakes are soft, slightly out of focus
      ctx.globalAlpha = alpha * .8;
      ctx.beginPath(); ctx.arc(x, y, f.r * .55, 0, 6.283); ctx.fill();
    } else {
      ctx.globalAlpha = alpha;
      ctx.beginPath(); ctx.arc(x, y, f.r, 0, 6.283); ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
}

export function create(env) {
  const {ctx, layer, noise, clampCount, random} = env;
  const pal = festiveColors(env.light);
  let flakes = [];
  return {
    seed(w, h) {
      flakes = Array.from({length: clampCount(w * h / 5200, 70, 360)}, () => makeFlake(w, h, random));
    },
    draw(time, w, h, dtf) {
      ctx.clearRect(0, 0, w, h);
      const bankH = Math.min(90, h * .14);
      const bank = layer(`snow-bank:${Math.round(w)}x${Math.round(h)}`, w, bankH, (g, lw, lh) => {
        const grd = g.createLinearGradient(0, 0, 0, lh);
        grd.addColorStop(0, pal.snow + (env.light ? '40' : '2e'));
        grd.addColorStop(1, pal.snow + '08');
        g.fillStyle = grd;
        g.beginPath(); g.moveTo(0, lh);
        for (let x = 0; x <= lw + 16; x += 16) g.lineTo(x, lh * (.28 + .5 * noise(x * .004, 9.1) - .16 * noise(x * .02, 4.4)));
        g.lineTo(lw, lh); g.closePath(); g.fill();
      });
      ctx.globalAlpha = 1;
      ctx.drawImage(bank.img, 0, h - bankH, w, bankH);
      drawFlakes(env, flakes, pal.snow, time, w, h, dtf);
    },
  };
}
