// Background effects that live in their own files. BackgroundCanvas draws an effect from here when its
// id has an entry, and from its own built-in functions otherwise.
//
// Each module exports `create(env)` and returns {seed(w, h), draw(time, w, h, dtf)}:
//   seed  lays the effect out for a canvas of w × h CSS pixels (first run and after a real resize);
//   draw  paints one frame. `time` is the effect's own clock in ms, `dtf` the frame factor (1 at 60 fps).
// `env` is what the built-in effects share: ctx, t (theme), colors, light, still, noise, snap, mix,
// drawGlow, fade, layer (a picture painted once and copied every frame), ox / oy (pointer parallax),
// pointer, clampCount, random. A module creates no canvas itself, so it can run in a test.
import {create as dots} from './dotGrid.js';
import {create as rain} from './pixelRain.js';
import {create as northern} from './northernLights.js';
import {create as snowfall} from './snowfall.js';
import {create as xmasLights} from './christmasLights.js';
import {create as silentNight} from './silentNight.js';
import {create as holidayGlow} from './holidayGlow.js';

export const EFFECT_MODULES = {dots, rain, northern, snowfall, xmasLights, silentNight, holidayGlow};
