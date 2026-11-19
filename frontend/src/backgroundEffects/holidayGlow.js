// Holiday Glow: large soft discs of red, gold and green light drifting slowly, like tree lights out of
// focus. One disc picture per colour is painted once and stretched to each disc's size.
import {festiveColors} from '../backgrounds.js';

export function create(env) {
  const {ctx, layer, clampCount, ox, oy, random} = env;
  const pal = festiveColors(env.light);
  const colours = [pal.red, pal.gold, pal.green, pal.gold, pal.red, pal.green, pal.blue];
  let discs = [];
  const make = (w, h, i) => {
    const z = .3 + random() * .7;
    return {x: random() * w, y: random() * h, r: (36 + random() * 90) * (.6 + z * .6), colour: colours[i % colours.length], a: (.1 + random() * .16) * (.5 + z * .5), vx: (random() - .5) * .16, vy: -(.05 + random() * .14) * z, ph: random() * 6.283, z};
  };
  const disc = colour => layer(`bokeh:${colour}`, 128, 128, (g, w, h) => {
    const grd = g.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
    // A flat middle with a slightly brighter rim, then a soft fall-off: what an out-of-focus light looks like.
    grd.addColorStop(0, colour + '9a');
    grd.addColorStop(.62, colour + '9a');
    grd.addColorStop(.74, colour + 'c8');
    grd.addColorStop(.86, colour + '40');
    grd.addColorStop(1, colour + '00');
    g.fillStyle = grd;
    g.fillRect(0, 0, w, h);
  });
  return {
    seed(w, h) {
      discs = Array.from({length: clampCount(w * h / 52000, 12, 30)}, (_, i) => make(w, h, i));
    },
    draw(time, w, h, dtf) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = env.light ? 'source-over' : 'lighter';
      ctx.imageSmoothingEnabled = true;
      discs.forEach((d, i) => {
        d.x += (d.vx + Math.sin(time * .00021 + d.ph) * .08) * dtf;
        d.y += d.vy * dtf;
        if (d.y < -d.r - 10) Object.assign(d, make(w, h, i), {y: h + d.r});
        if (d.x < -d.r - 10) d.x = w + d.r; else if (d.x > w + d.r + 10) d.x = -d.r;
        const breathe = .5 + .5 * Math.sin(time * .0006 + d.ph), r = d.r * (.94 + .1 * breathe);
        ctx.globalAlpha = d.a * (.6 + .4 * breathe);
        ctx.drawImage(disc(d.colour).img, d.x + ox(d.z) - r, d.y + oy(d.z) - r, r * 2, r * 2);
      });
      ctx.imageSmoothingEnabled = false;
    },
  };
}
