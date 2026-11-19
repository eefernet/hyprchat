import test from 'node:test';
import assert from 'node:assert/strict';
import {EFFECT_MODULES} from './index.js';
import {CANVAS_EFFECTS} from '../backgrounds.js';
import {bandAt, GAP} from './dotGrid.js';
import {makeString, wireAt, brightness} from './christmasLights.js';
import {makeDrop} from './pixelRain.js';
import {makeFlake} from './snowfall.js';

// A stand-in for a 2D canvas context: it draws nothing, counts the calls and refuses any number that
// is not finite (a NaN coordinate draws nothing in a browser and says nothing, which is how an effect
// silently goes blank).
function recorder() {
  const calls = {};
  const check = (name, args) => { for (const a of args) if (typeof a === 'number' && !Number.isFinite(a)) throw new Error(`${name} got ${a}`); };
  const gradient = () => ({addColorStop(stop, colour) { check('addColorStop', [stop]); assert.match(colour, /^(#[0-9a-f]{8}|rgba?\(.+\))$/i); }});
  const target = {
    createLinearGradient(...args) { check('createLinearGradient', args); return gradient(); },
    createRadialGradient(...args) { check('createRadialGradient', args); return gradient(); },
  };
  let alpha = 1;
  return {
    calls,
    ctx: new Proxy(target, {
      get(obj, key) {
        if (key in obj) return obj[key];
        if (key === 'globalAlpha') return alpha;
        return (...args) => { check(String(key), args); calls[key] = (calls[key] || 0) + 1; };
      },
      set(obj, key, value) {
        if (key === 'globalAlpha') { if (!Number.isFinite(value)) throw new Error(`globalAlpha set to ${value}`); alpha = value; return true; }
        if (typeof value === 'number' && !Number.isFinite(value)) throw new Error(`${String(key)} set to ${value}`);
        return true;
      },
    }),
  };
}

// The same pseudo-random numbers on every run.
function seeded(seed = 7) {
  let state = seed;
  return () => { state = (state * 1664525 + 1013904223) % 4294967296; return state / 4294967296; };
}

const noise = (x, y) => (Math.sin(x * 12.9898 + y * 78.233) * .5 + .5);
const theme = {acc: '#9cdef2', acc2: '#c9a0ff', warm: '#e06c75', f1: '#88c0d0', text: '#cdeff7', bg: '#282c34'};

function environment({light = false, still = false} = {}) {
  const {ctx, calls} = recorder();
  const layers = new Map();
  const env = {
    ctx, t: theme, colors: [theme.acc, theme.warm, theme.f1, theme.acc2], light, still, noise, random: seeded(),
    snap: (v, s = 4) => Math.round(v / s) * s,
    mix: (a) => a,
    drawGlow(colour, r, x, y, alpha) { assert.match(colour, /^#[0-9a-f]{6}$/i); for (const v of [r, x, y, alpha]) assert.ok(Number.isFinite(v), `drawGlow got ${v}`); calls.drawGlow = (calls.drawGlow || 0) + 1; },
    fade() {},
    layer(key, w, h, paint) {
      assert.ok(w > 0 && h > 0 && Number.isFinite(w) && Number.isFinite(h), `layer ${key} is ${w} × ${h}`);
      if (!layers.has(key)) { paint(ctx, w, h); layers.set(key, {img: {}, w, h}); }
      return layers.get(key);
    },
    ox: z => z * 3, oy: z => z * -2,
    pointer: {x: 300, y: 200, seen: true},
    clampCount: (n, min, max) => Math.min(max, Math.max(min, Math.floor(n))),
    view: {w: 1440, h: 900, dpr: 2},
  };
  return {env, calls, layers};
}

const SIZES = [[1440, 900], [375, 812], [124, 64]];

for (const [id, create] of Object.entries(EFFECT_MODULES)) {
  test(`${id}: 600 frames at every size, dark and light, without a bad number`, () => {
    for (const light of [false, true]) for (const [w, h] of SIZES) {
      const {env, calls, layers} = environment({light});
      const effect = create(env);
      effect.seed(w, h);
      for (let frame = 0; frame < 600; frame++) effect.draw(4000 + frame * 16.667, w, h, 1);
      // A frame after a long pause and a slow frame must not break it either.
      effect.draw(9e6, w, h, 3);
      effect.draw(9e6 + 16.667, w, h, .2);
      const drawn = Object.entries(calls).filter(([name]) => /^(fill|stroke|fillRect|strokeRect|drawImage|drawGlow|fillText)$/.test(name)).reduce((sum, [, n]) => sum + n, 0);
      assert.ok(drawn > 600, `${id} drew only ${drawn} things in 600 frames at ${w}×${h}`);
      assert.ok(layers.size <= 12, `${id} keeps ${layers.size} painted layers: one per frame would leak memory`);
    }
  });

  test(`${id}: a still picture draws something`, () => {
    const {env, calls} = environment({still: true});
    const effect = create(env);
    effect.seed(1440, 900);
    effect.draw(4000, 1440, 900, 1);
    assert.ok(Object.keys(calls).length > 0);
  });
}

test('every effect with its own file is a canvas effect', () => {
  for (const id of Object.keys(EFFECT_MODULES)) assert.ok(CANVAS_EFFECTS.includes(id), id);
});

test('dot grid: the band crosses the whole grid, then rests', () => {
  assert.ok(bandAt(0, 1440, 900) < 0);
  assert.ok(bandAt(5599, 1440, 900) > 1440 + 900);
  assert.equal(bandAt(6000, 1440, 900), null);
  assert.equal(bandAt(9000, 1440, 900), bandAt(0, 1440, 900));
  assert.equal(GAP, 24);
});

test('christmas lights: the wire hangs between its anchors and bulbs stay in range', () => {
  const string = makeString(1440, 1, seeded(3));
  assert.ok(string.bulbs.length > 20);
  for (const bulb of string.bulbs) assert.ok(bulb.x >= 0 && bulb.x < 1440 && bulb.colour >= 0 && bulb.colour < 4);
  const a = string.anchors[0], b = string.anchors[1], mid = (a.x + b.x) / 2;
  assert.ok(Math.abs(wireAt(string, a.x).y - a.y) < 1e-9);
  assert.ok(wireAt(string, mid).y > Math.min(a.y, b.y), 'the wire sags below its higher anchor');
  assert.ok(Math.abs(wireAt(string, mid).hang - 1) < 1e-9);
  for (const x of [-500, 0, 700, 1440, 5000]) assert.ok(Number.isFinite(wireAt(string, x).y));
  for (const mode of [0, 1, 2]) for (let i = 0; i < 40; i++) {
    const value = brightness(mode, i * 733, i, i % 4, (i * .37) % 1);
    assert.ok(value >= 0 && value <= 1, `mode ${mode} gave ${value}`);
  }
});

test('rain drops sit on columns and new drops start above the screen', () => {
  const random = seeded(5), colours = ['#111111', '#222222'];
  for (let i = 0; i < 50; i++) {
    const drop = makeDrop(1440, 900, colours, random, i, true);
    assert.equal(drop.x % 12, 0);
    assert.ok(drop.y <= 0 && drop.v > 0 && drop.tail >= 3);
  }
});

test('snowflakes: near flakes are bigger and faster than far ones', () => {
  const random = seeded(9), flakes = Array.from({length: 400}, () => makeFlake(1440, 900, random));
  const mean = (z, key) => { const list = flakes.filter(f => f.z === z); return list.reduce((s, f) => s + f[key], 0) / list.length; };
  assert.ok(mean(1, 'r') > mean(.3, 'r') && mean(1, 'v') > mean(.3, 'v'));
  assert.ok(flakes.filter(f => f.z === .3).length > flakes.filter(f => f.z === 1).length);
});
