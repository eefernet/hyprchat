// Pixel Rain: drops of square pixels falling in aligned columns at three depths. Far drops are small,
// dim and slow; near ones are larger, brighter and faster. A drop is a bright head with a tail that
// fades out upward, and it leaves a tiny splash where it meets the bottom edge.

// depth, block size, slowest and fastest speed, brightness of the head, shortest and longest tail
const LAYERS = [
  {z: .3, size: 2, slow: .8, fast: 1.5, alpha: .34, tailMin: 4, tailMax: 8},
  {z: .6, size: 3, slow: 1.5, fast: 2.6, alpha: .58, tailMin: 6, tailMax: 12},
  {z: 1, size: 4, slow: 2.6, fast: 4.2, alpha: .9, tailMin: 9, tailMax: 16},
];
const COLUMN = 12;      // drops line up on columns this far apart

export function makeDrop(w, h, colours, random, index, top = false) {
  const pick = random(), layer = pick < .46 ? LAYERS[0] : pick < .82 ? LAYERS[1] : LAYERS[2];
  const tail = layer.tailMin + Math.floor(random() * (layer.tailMax - layer.tailMin + 1));
  return {
    x: Math.floor(random() * (w / COLUMN + 1)) * COLUMN, y: top ? -random() * h * .4 : random() * h, layer,
    v: layer.slow + random() * (layer.fast - layer.slow), tail, colour: colours[index % colours.length], a: layer.alpha * (.7 + random() * .3),
  };
}

export function create(env) {
  const {ctx, colors, drawGlow, ox, oy, clampCount, random} = env;
  let drops = [], splashes = [];
  return {
    seed(w, h) {
      drops = Array.from({length: clampCount(w * h / 4600, 60, 420)}, (_, i) => makeDrop(w, h, colors, random, i));
      splashes = [];
    },
    draw(time, w, h, dtf) {
      ctx.clearRect(0, 0, w, h);
      for (let i = 0; i < drops.length; i++) {
        const d = drops[i], size = d.layer.size, pitch = size + 2;
        d.y += d.v * dtf;
        if (d.y - d.tail * pitch > h) {
          if (d.layer.z > .5 && splashes.length < 60) splashes.push({x: d.x, colour: d.colour, a: d.a, life: 1, size, z: d.layer.z});
          Object.assign(d, makeDrop(w, h, colors, random, i, true));
          continue;
        }
        const x = d.x + ox(d.layer.z), head = Math.floor(d.y / pitch) * pitch + oy(d.layer.z);   // falls a block at a time
        ctx.fillStyle = d.colour;
        for (let k = 0; k < d.tail; k++) {
          const y = head - k * pitch;
          if (y < -size || y > h) continue;
          const fall = 1 - k / d.tail;
          ctx.globalAlpha = k === 0 ? Math.min(1, d.a * 1.5) : d.a * fall * fall * .8;
          ctx.fillRect(x, y, size, size);
        }
        if (d.layer.z === 1 && head < h) {
          ctx.globalCompositeOperation = 'lighter';
          drawGlow(d.colour, size * 4, x + size / 2, head + size / 2, d.a * .7);
          ctx.globalCompositeOperation = 'source-over';
        }
      }
      // Splashes: two pixels thrown up and outward from where a drop landed.
      for (let i = splashes.length - 1; i >= 0; i--) {
        const s = splashes[i];
        s.life -= .055 * dtf;
        if (s.life <= 0) { splashes.splice(i, 1); continue; }
        const out = (1 - s.life) * 9, up = Math.sin(s.life * Math.PI) * 6, x = s.x + ox(s.z);
        ctx.fillStyle = s.colour;
        ctx.globalAlpha = s.a * s.life;
        ctx.fillRect(Math.round(x - out), Math.round(h - 2 - up), 2, 2);
        ctx.fillRect(Math.round(x + s.size + out - 2), Math.round(h - 2 - up), 2, 2);
      }
      ctx.globalAlpha = 1;
    },
  };
}
