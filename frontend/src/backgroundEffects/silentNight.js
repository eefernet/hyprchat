// Silent Night: a row of pine silhouettes along the bottom, one bright star, a scatter of twinkling
// stars and light snow. The trees are painted once and copied every frame.
import {festiveColors} from '../backgrounds.js';
import {makeFlake, drawFlakes} from './snowfall.js';

// A pine as stacked triangles, widest at the bottom.
function pine(g, x, base, height, width) {
  const tiers = 4;
  g.beginPath();
  for (let i = 0; i < tiers; i++) {
    const top = base - height + (height * .78) * (i / tiers), bottom = base - height + height * (.42 + .58 * ((i + 1) / tiers));
    const half = width * (.34 + .66 * ((i + 1) / tiers)) / 2;
    g.moveTo(x, top); g.lineTo(x + half, bottom); g.lineTo(x - half, bottom); g.closePath();
  }
  g.rect(x - width * .05, base - height * .04, width * .1, height * .04);
  g.fill();
}

export function create(env) {
  const {ctx, drawGlow, layer, noise, clampCount, random} = env;
  const pal = festiveColors(env.light);
  let stars = [], flakes = [], trees = [];
  return {
    seed(w, h) {
      stars = Array.from({length: clampCount(w * h / 22000, 30, 90)}, () => ({x: random() * w, y: random() * h * .62, a: .18 + random() * .4, tw: random() * 6.283, sp: .0008 + random() * .0016, s: random() > .82 ? 2 : 1}));
      flakes = Array.from({length: clampCount(w * h / 16000, 30, 120)}, () => makeFlake(w, h, random));
      // Two rows: a far, paler one and a nearer, taller one. Fixed here so a repaint gives the same wood.
      trees = [];
      for (let x = -20; x < w + 40; x += 30 + random() * 40) trees.push({x, row: 0, height: .34 + random() * .3, spread: .4 + random() * .14});
      for (let x = -30; x < w + 60; x += 70 + random() * 90) trees.push({x, row: 1, height: .55 + random() * .45, spread: .42 + random() * .16});
    },
    draw(time, w, h, dtf) {
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = pal.star;
      for (const star of stars) {
        ctx.globalAlpha = star.a * (.55 + .45 * Math.sin(time * star.sp + star.tw));
        ctx.fillRect(star.x, star.y, star.s, star.s);
      }
      // The bright star: a soft glow and four thin rays that slowly breathe.
      const sx = w * .74, sy = h * .2, pulse = .78 + .22 * Math.sin(time * .0012);
      if (!env.light) ctx.globalCompositeOperation = 'lighter';
      drawGlow(pal.star, 30, sx, sy, .34 * pulse);
      drawGlow(pal.star, 9, sx, sy, .9);
      ctx.globalCompositeOperation = 'source-over';
      ctx.strokeStyle = pal.star; ctx.lineWidth = 1;
      for (const [length, alpha, turn] of [[30 * pulse, .55, 0], [15 * pulse, .3, Math.PI / 4]]) {
        ctx.globalAlpha = alpha;
        ctx.beginPath();
        for (let k = 0; k < 2; k++) {
          const angle = turn + k * Math.PI / 2, cx = Math.cos(angle) * length, cy = Math.sin(angle) * length;
          ctx.moveTo(sx - cx, sy - cy); ctx.lineTo(sx + cx, sy + cy);
        }
        ctx.stroke();
      }
      const woodH = Math.min(230, h * .34);
      const wood = layer(`pines:${Math.round(w)}x${Math.round(h)}`, w, woodH, (g, lw, lh) => {
        g.fillStyle = pal.pine;
        for (const row of [0, 1]) {
          g.globalAlpha = env.light ? (row ? .34 : .18) : (row ? .2 : .1);
          for (const tree of trees) if (tree.row === row) pine(g, tree.x, lh - (row ? 0 : lh * .06), lh * tree.height, lh * tree.height * tree.spread);
        }
        // Snow on the ground in front of the trees.
        const grd = g.createLinearGradient(0, lh * .82, 0, lh);
        grd.addColorStop(0, pal.snow + '00'); grd.addColorStop(1, pal.snow + (env.light ? '50' : '30'));
        g.globalAlpha = 1; g.fillStyle = grd;
        g.beginPath(); g.moveTo(0, lh);
        for (let x = 0; x <= lw + 20; x += 20) g.lineTo(x, lh * (.86 + .08 * noise(x * .005, 2.3)));
        g.lineTo(lw, lh); g.closePath(); g.fill();
      });
      ctx.globalAlpha = 1;
      ctx.drawImage(wood.img, 0, h - woodH, w, woodH);
      drawFlakes(env, flakes, pal.snow, time, w, h, dtf, .75);
    },
  };
}
