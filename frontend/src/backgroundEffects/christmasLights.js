// Christmas lights: strings of coloured bulbs hanging in sagging curves across the top of the screen.
// The pattern moves between a slow twinkle, a chase along each string and a fade by colour.
import {festiveColors} from '../backgrounds.js';

const SPACING = 46;        // distance between bulbs along a string
const MODE_MS = 9000;      // how long one pattern lasts
const BLEND_MS = 1200;     // cross-fade between two patterns

// One string: anchor points across the width, with the wire sagging between each pair.
export function makeString(w, index, random) {
  const spans = Math.max(2, Math.ceil(w / 280));
  const anchors = Array.from({length: spans + 1}, (_, i) => ({x: (i / spans) * (w + 40) - 20, y: 10 + index * 38 + random() * 18}));
  const sags = Array.from({length: spans}, () => 22 + random() * 26);
  const bulbs = [];
  for (let x = SPACING * (.3 + random() * .5); x < w; x += SPACING) bulbs.push({x, colour: bulbs.length % 4, ph: random()});
  return {anchors, sags, bulbs, ph: random() * 6.283};
}

// Height of the wire at x, and how far down its span that point hangs (0 at an anchor, 1 mid-span).
export function wireAt(string, x) {
  const {anchors, sags} = string;
  let i = 0;
  while (i < anchors.length - 2 && x > anchors[i + 1].x) i++;
  const a = anchors[i], b = anchors[i + 1];
  const f = Math.min(1, Math.max(0, (x - a.x) / (b.x - a.x || 1)));
  const hang = 4 * f * (1 - f);
  return {y: a.y + (b.y - a.y) * f + sags[i] * hang, hang};
}

// Brightness (0..1) of one bulb in one pattern.
export function brightness(mode, time, index, colour, phase) {
  if (mode === 0) return .5 + .5 * Math.sin(time * .0028 + phase * 6.283);                       // twinkle
  if (mode === 1) { const p = (((index - time * .006) % 6) + 6) % 6; return p < 1 ? 1 - p * .35 : p < 2 ? .65 - (p - 1) * .5 : .15; }   // chase
  return .15 + .85 * Math.max(0, Math.sin(time * .0017 + colour * Math.PI / 2));                 // one colour after another
}

export function create(env) {
  const {ctx, drawGlow, clampCount, random} = env;
  const pal = festiveColors(env.light);
  const colours = [pal.red, pal.green, pal.gold, pal.blue];
  let strings = [];
  return {
    seed(w, h) {
      strings = Array.from({length: clampCount(h / 300, 2, 3)}, (_, i) => makeString(w, i, random));
    },
    draw(time, w, h) {
      ctx.clearRect(0, 0, w, h);
      const mode = Math.floor(time / MODE_MS) % 3, into = time % MODE_MS;
      const blend = into > MODE_MS - BLEND_MS ? (into - (MODE_MS - BLEND_MS)) / BLEND_MS : 0;
      for (const string of strings) {
        const sway = x => Math.sin(time * .0011 + x * .008 + string.ph) * 2.2;
        ctx.strokeStyle = pal.wire;
        ctx.globalAlpha = env.light ? .3 : .16;
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = -20; x <= w + 20; x += 12) {
          const at = wireAt(string, x), y = at.y + sway(x) * at.hang;
          if (x === -20) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
        string.bulbs.forEach((bulb, index) => {
          const at = wireAt(string, bulb.x), y = at.y + sway(bulb.x) * at.hang + 5, colour = colours[bulb.colour];
          let lit = brightness(mode, time, index, bulb.colour, bulb.ph);
          if (blend) lit += (brightness((mode + 1) % 3, time, index, bulb.colour, bulb.ph) - lit) * blend;
          if (!env.light) ctx.globalCompositeOperation = 'lighter';
          drawGlow(colour, 9 + 9 * lit, bulb.x, y, .18 + .62 * lit);
          ctx.globalCompositeOperation = 'source-over';
          ctx.globalAlpha = .45 + .55 * lit;
          ctx.fillStyle = colour;
          ctx.beginPath(); ctx.ellipse(bulb.x, y, 2.6, 3.6, 0, 0, 6.283); ctx.fill();
          ctx.globalAlpha = env.light ? .5 : .3;
          ctx.fillStyle = pal.wire;
          ctx.fillRect(bulb.x - 1.5, y - 6, 3, 2.5);       // the cap that holds the bulb on the wire
        });
      }
      ctx.globalAlpha = 1;
    },
  };
}
