/** scene.ts — Seymour's pixel scenes, extracted from Nick's reference art.
 *
 * The desk states blit DESK_SCENE and the idle/tea states blit TEA_SCENE
 * (scenes.ts — whole compositions extracted cell-for-cell from the framed
 * reference images), while the empty-chat greeting keeps the standing
 * sprite (sprite.ts) in its procedural sandbox. Every blit maps cells
 * through the live palette, so the art stays:
 *
 *   - tintable    (body and furniture follow the user's chosen hues),
 *   - full-screen (a scene COVERS the canvas: integer scale, face-anchored
 *                  crop, and edge rows replicate outward so no aspect
 *                  ever letterboxes),
 *   - animatable  (expressions are cell-space overdraws on the blit),
 *   - honest      (a state change is the only thing that changes the art).
 *
 * The canvas is small (256 logical pixels wide) and CSS scales it up with
 * image-rendering: pixelated — that scaling is what makes it look like
 * pixel art rather than bad vector art. `S` is the detail scale: fixed-
 * size decorations (bubbles, glyphs) draw their cells S pixels square so
 * they keep their PROPORTION of the scene at any resolution.
 */

import {
  SEYMOUR, SPRITE_W, SPRITE_H, HEAD_TOP,
  EYE_Y, EYE_XS, EYE_RX, EYE_RY, PUPIL_R, EYE_BAND, MOUTH_BOX,
} from "./sprite";
import { DESK_SCENE, TEA_SCENE, type Scene } from "./scenes";

/** Everything a scene needs to know to paint one frame.
 *  ("play" is the one DECORATIVE state — the empty-chat greeting. The
 *  status avatar never uses it; his states stay driven by real events.) */
export interface SceneInput {
  state: "sleeping" | "blocked" | "waiting" | "working" | "tea" | "idle" | "play";
  frame: number;                 // 0/1 — the animation clock's coin flip
  blink: boolean;                // eyes closed this frame? (avatar decides)
  onBattery: boolean;            // draw the battery badge?
  theme: "dark" | "light";       // palette direction
  avatarHue: number;             // Seymour's body color
  accentHue: number;             // scene furniture / screen tint
  // The screen's whole look: "theme" tints everything from the app's
  // colors (the default); "gameboy" is the classic DMG four-green LCD,
  // theme and hues ignored — nostalgia is a palette, not a filter.
  screen?: "theme" | "gameboy";
}

/** The derived palette for one paint. */
interface Palette {
  paper: string;                 // the "LCD" background
  dot: string;                   // the paper's dot-matrix texture
  faint: string;                 // furniture fill
  mid: string;                   // furniture shading / dark fills
  ink: string;                   // theme-contrast: furniture outlines and
                                 // floating glyphs (light in dark mode)
  line: string;                  // ALWAYS dark: the monster's outlines,
                                 // pupils, and anything drawn on white.
                                 // (His body stays light in both themes,
                                 // so his ink must stay dark in both —
                                 // a light pupil on a white eye is how a
                                 // monster loses his soul.)
  body: string;                  // Seymour's skin
  bodyDim: string;               // Seymour's freckles / shaded skin
  horn: string;                  // horns (kept warm like the reference art)
  white: string;                 // eye whites / teeth
  red: string;                   // open mouth interior
}

/** Build the palette from the theme + the user's two hue choices. */
export function palette(input: SceneInput): Palette {
  if (input.screen === "gameboy") {
    // The classic screen, SAMPLED from Nick's reference art (the tea
    // scene is drawn entirely in this ramp): warm olive paper, khaki
    // mids, deep olive ink — the handheld look those images actually
    // have, not the internet's oversaturated DMG hex codes. Both app
    // themes get the SAME palette: a green LCD has no dark mode.
    return {
      paper: "#c0bf7f", dot: "#b3b273", faint: "#777d4e", mid: "#545f39",
      ink: "#1e280c", line: "#1e280c",
      body: "#c0bf7f", bodyDim: "#a3a166", horn: "#c0bf7f",
      white: "#d6d194", red: "#a3a166",
    };
  }
  const a = input.accentHue;     // furniture follows the accent
  const b = input.avatarHue;     // the monster follows his own hue
  if (input.theme === "light") {
    // The reference look: pale warm screen, near-black ink.
    return {
      paper: `hsl(${a} 30% 86%)`,
      dot: `hsl(${a} 24% 81%)`,
      faint: `hsl(${a} 24% 78%)`,
      mid: `hsl(${a} 18% 60%)`,
      ink: `hsl(${a} 25% 15%)`,
      line: `hsl(${a} 25% 15%)`,    // in daylight, line and ink agree
      body: `hsl(${b} 38% 64%)`,
      bodyDim: `hsl(${b} 34% 50%)`,
      horn: "hsl(35 55% 68%)",
      white: `hsl(${a} 30% 94%)`,
      red: "hsl(0 55% 42%)",
    };
  }
  // Dark theme: a backlit screen at night — same ramp, inverted for the
  // ROOM (paper, furniture, glyphs), but never for the monster: his body
  // stays light, so his outlines stay dark.
  return {
    paper: `hsl(${a} 22% 13%)`,
    dot: `hsl(${a} 18% 17%)`,
    faint: `hsl(${a} 18% 22%)`,
    mid: `hsl(${a} 14% 42%)`,
    ink: `hsl(${a} 25% 78%)`,
    line: `hsl(${a} 30% 10%)`,
    body: `hsl(${b} 42% 58%)`,
    bodyDim: `hsl(${b} 36% 42%)`,
    horn: "hsl(35 45% 60%)",
    white: `hsl(${a} 25% 90%)`,
    red: "hsl(0 50% 48%)",
  };
}

// --------------------------------------------------------------------------
//  Tiny drawing kit — everything below paints in LOGICAL pixels.
// --------------------------------------------------------------------------

let g: CanvasRenderingContext2D;         // the context of the current paint
let S = 2;                               // the detail scale for this paint

/** One filled rectangle (the only primitive the canvas ever sees). */
function rect(x: number, y: number, w: number, h: number, c: string): void {
  g.fillStyle = c;
  g.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
}

/** A filled rounded blob (the furniture's whole aesthetic in one helper).
 *  Row spans shrink near the top/bottom edges via circle math. */
function blob(x: number, y: number, w: number, h: number, r: number, c: string): void {
  for (let dy = 0; dy < h; dy++) {
    // Distance into the top or bottom rounded band (0 in the middle).
    const band = dy < r ? r - dy : dy >= h - r ? dy - (h - r - 1) : 0;
    const inset = r - Math.floor(Math.sqrt(Math.max(0, r * r - band * band)));
    rect(x + inset, y + dy, w - inset * 2, 1, c);
  }
}

/** An outlined blob: silhouette, then the fill inset by the line width
 *  (the line thickens with the detail scale — chunky at every size). */
function outlined(x: number, y: number, w: number, h: number, r: number,
                  fill: string, edge: string): void {
  blob(x, y, w, h, r, edge);
  blob(x + S, y + S, w - 2 * S, h - 2 * S, Math.max(0, r - S), fill);
}

/** Draw a small text-grid at a position; each cell is S×S pixels. */
function glyph(x: number, y: number, rows: string[], c: string): void {
  rows.forEach((row, dy) => {
    for (let dx = 0; dx < row.length; dx++) {
      if (row[dx] === "#") rect(x + dx * S, y + dy * S, S, S, c);
    }
  });
}

/** Checkerboard dither inside a horizontal band — the reference art's
 *  soft shading, done the pixel-art way. */
function dither(x: number, y: number, w: number, h: number, c: string): void {
  for (let dy = 0; dy < h; dy++) {
    for (let dx = 0; dx < w; dx += 2) {
      // Offset odd rows by one for the classic checker.
      rect(x + dx + (dy % 2), y + dy, 1, 1, c);
    }
  }
}

/** The glyphs the scenes use. */
const QUESTION = ["####.", "...#.", "..#..", ".....", "..#.."];
const ZEE = ["####", "..#.", ".#..", "####"];

// --------------------------------------------------------------------------
//  Seymour himself
// --------------------------------------------------------------------------

/** How the monster varies between scenes/frames. */
interface Pose {
  eyes: "open" | "closed" | "down" | "wide";
  mouth: "smile" | "open" | "o" | "flat";
  brows: "neutral" | "raised";
}

/** The cell legend → the live palette (sprite.ts and scenes.ts share it).
 *  '.' returns null: transparent, the paper + dot texture shows through. */
function cellColor(c: string, p: Palette): string | null {
  switch (c) {
    case "k": return p.line;       // monster outlines, pupils, mouth cavity
    case "i": return p.ink;        // furniture outlines (theme-contrast)
    case "g": return p.ink;        // floating strokes (the steam)
    case "b": return p.body;       // skin
    case "s": return p.bodyDim;    // freckles, shading, brows
    case "w": return p.white;      // eye whites, teeth, the teacup
    case "o": return p.horn;       // horn fill
    case "d": return p.bodyDim;    // horn ridge stripes
    case "t": return p.red;        // the tongue
    case "f": return p.faint;      // furniture fill
    case "m": return p.mid;        // furniture shading
    default: return null;          // '.' — the room's wall
  }
}

// --------------------------------------------------------------------------
//  Scene blits (the extracted compositions from scenes.ts)
// --------------------------------------------------------------------------

/** Where a blitted scene landed: cell (i,j) → pixel (ox+i*k, oy+j*k). */
interface Blit {
  k: number;                     // integer pixels per cell
  ox: number;                    // blit origin, may be negative (cropped)
  oy: number;
}

/** Paint a whole scene so it COVERS the canvas: the smallest integer
 *  scale that fills both axes, centered horizontally, and vertically
 *  centered on the scene's FOCUS BAND (rows `f0..f1` — the stretch from
 *  his horns to his feet that must stay on screen when the aspect forces
 *  a crop; wall above and desk front below give way first). Sampling is
 *  CLAMPED at the grid edges, so a canvas larger than the scene repeats
 *  the border rows/cols outward — the wall and the desk just continue,
 *  and no aspect ever letterboxes. */
function blitScene(sc: Scene, f0: number, f1: number, p: Palette): Blit {
  const w = g.canvas.width, h = g.canvas.height;
  const k = Math.max(1, Math.round(Math.max(w / sc.w, h / sc.h)));
  const ox = Math.round((w - sc.w * k) / 2);     // compositions stay centered
  // Center the focus band; clamp so cropping never opens a gap.
  let oy = Math.round((h - (f1 - f0) * k) / 2) - f0 * k;
  if (sc.h * k >= h) oy = Math.min(0, Math.max(h - sc.h * k, oy));
  else oy = h - sc.h * k;                        // short: pin the desk down
  const ci0 = Math.floor((0 - ox) / k), ci1 = Math.floor((w - 1 - ox) / k);
  const cj0 = Math.floor((0 - oy) / k), cj1 = Math.floor((h - 1 - oy) / k);
  for (let cj = cj0; cj <= cj1; cj++) {
    const row = sc.rows[Math.min(sc.h - 1, Math.max(0, cj))]!;
    for (let ci = ci0; ci <= ci1; ) {
      // one fillRect per same-class RUN (clamped indices repeat edges)
      const c = row[Math.min(sc.w - 1, Math.max(0, ci))]!;
      let e = ci + 1;
      while (e <= ci1 && row[Math.min(sc.w - 1, Math.max(0, e))] === c) e++;
      const color = cellColor(c, p);
      if (color) rect(ox + ci * k, oy + cj * k, (e - ci) * k, k, color);
      ci = e;
    }
  }
  return { k, ox, oy };
}

/** Per-scene eye geometry for the overdraws (measured with the scenes). */
interface EyeGeom {
  rx: number;                    // outline half-width, in cells
  ry: number;                    // outline half-height (eyes sit tall)
  pr: number;                    // resting pupil radius
}

/** Redraw a blitted scene's face in a pose. The baked face IS the
 *  reference (open eyes, open smile); any other pose erases just the
 *  face cells and redraws them at the scene's measured anchors. */
function sceneFace(sc: Scene, bl: Blit, geom: EyeGeom, pose: Pose,
                   p: Palette): void {
  const cRect = (i: number, j: number, wc: number, hc: number, c: string) =>
    rect(bl.ox + i * bl.k, bl.oy + j * bl.k, wc * bl.k, hc * bl.k, c);
  const cEllipse = (ei: number, ej: number, rx: number, ry: number, c: string) => {
    for (let dj = -ry; dj <= ry; dj++) {
      const half = Math.floor(rx * Math.sqrt(Math.max(0, 1 - (dj / ry) ** 2)));
      cRect(ei - half, ej + dj, half * 2 + 1, 1, c);
    }
  };
  if (pose.eyes !== "open") {
    // Erase the whole eye band (brows above it survive), then redraw all
    // three eyes as a set — they share outlines.
    const x0 = sc.eyes[0]![0] - geom.rx - 1, x1 = sc.eyes[2]![0] + geom.rx + 1;
    const yTop = Math.min(...sc.eyes.map((e) => e[1])) - geom.ry;
    const yBot = Math.max(...sc.eyes.map((e) => e[1])) + geom.ry;
    cRect(x0, yTop, x1 - x0 + 1, yBot - yTop + 1, p.body);
    for (const [ex, ey] of sc.eyes) {
      if (pose.eyes === "closed") {
        // Sleeping/blinking: a thick, content arc with upturned ends.
        cRect(ex - geom.rx + 1, ey, geom.rx * 2 - 1, 2, p.line);
        cRect(ex - geom.rx, ey - 1, 1, 2, p.line);
        cRect(ex + geom.rx, ey - 1, 1, 2, p.line);
        continue;
      }
      cEllipse(ex, ey, geom.rx, geom.ry, p.line);           // outline ring
      cEllipse(ex, ey, geom.rx - 2, geom.ry - 2, p.white);  // the white
      const pj = pose.eyes === "down" ? ey + 2 : ey;        // glance down
      const pr = pose.eyes === "wide" ? geom.pr + 1 : geom.pr;
      cEllipse(ex, pj, pr, pr, p.line);
      cRect(ex - 1, pj - pr + 1, 1, 1, p.white);            // catch-light
    }
  }
  if (pose.mouth !== "open") {
    // Erase the mouth box, then the pose's mouth around its center.
    const [mx0, mx1, my0, my1] = sc.mouth;
    cRect(mx0, my0, mx1 - mx0 + 1, my1 - my0 + 1, p.body);
    const mc = Math.floor((mx0 + mx1) / 2);
    if (pose.mouth === "o") {
      cEllipse(mc, my0 + 5, 4, 4, p.line);                  // surprised ring
      cEllipse(mc, my0 + 5, 2, 2, p.red);
    } else if (pose.mouth === "flat") {
      cRect(mc - 8, my0 + 3, 16, 2, p.line);                // a resting line
    } else {
      // A closed smile: a shallow arc built from three dashes.
      cRect(mc - 9, my0 + 2, 3, 2, p.line);
      cRect(mc - 7, my0 + 3, 14, 2, p.line);
      cRect(mc + 6, my0 + 2, 3, 2, p.line);
    }
  }
}

/** Draw the monster: the extracted reference sprite, blitted at an
 *  INTEGER scale (pixel art scales by whole steps or it stops being pixel
 *  art). `cx` is his center; `top` his head-crown line (horns and tuft
 *  rise above it); `w` the target body width the scale is fit to. The
 *  sprite is the full STANDING pose — scenes that seat him at furniture
 *  simply draw the desk (or pass `cropBottom`) over his legs. */
function monster(cx: number, top: number, w: number,
                 pose: Pose, p: Palette, cropBottom?: number): void {
  const k = Math.max(1, Math.round(w / SPRITE_W));   // cells → pixels
  const x0 = Math.round(cx - (SPRITE_W * k) / 2);    // blit origin, left
  const y0 = Math.round(top - HEAD_TOP * k);         // …and top (crown on `top`)
  // How many sprite rows to draw: all of them, unless the scene buries
  // his lower half (the sand pit crops; desks just paint over him).
  const rows = cropBottom === undefined
    ? SPRITE_H
    : Math.max(0, Math.min(SPRITE_H, Math.ceil((cropBottom - y0) / k)));
  // The blit: one fillRect per same-class RUN, not per cell (a frame is
  // ~1500 rects this way instead of ~9000).
  for (let j = 0; j < rows; j++) {
    const row = SEYMOUR[j]!;
    for (let i = 0; i < SPRITE_W; ) {
      const c = row[i]!;
      let e = i + 1;                               // run end (exclusive)
      while (e < SPRITE_W && row[e] === c) e++;
      const color = cellColor(c, p);
      if (color) rect(x0 + i * k, y0 + j * k, (e - i) * k, k, color);
      i = e;
    }
  }

  // ---- Expression overdraws, in CELL coordinates -------------------------
  // The baked face IS the reference (open eyes with catch-lights, the big
  // open smile with tongue). Other poses erase just that face band and
  // redraw it — the anchors come from the same extraction as the sprite.
  const cRect = (i: number, j: number, wc: number, hc: number, c: string) =>
    rect(x0 + i * k, y0 + j * k, wc * k, hc * k, c);
  // An ellipse as row spans — the eyes are slightly taller than wide
  // (their shared outlines pinch them horizontally).
  const cEllipse = (ei: number, ej: number, rx: number, ry: number, c: string) => {
    for (let dj = -ry; dj <= ry; dj++) {
      const half = Math.floor(rx * Math.sqrt(Math.max(0, 1 - (dj / ry) ** 2)));
      cRect(ei - half, ej + dj, half * 2 + 1, 1, c);
    }
  };

  if (pose.eyes !== "open") {
    // Erase the whole eye band (the brows above it survive), then redraw
    // all three eyes in the pose — they share outlines, so they are
    // always drawn as a set.
    cRect(EYE_BAND[0], EYE_BAND[1],
          EYE_BAND[2] - EYE_BAND[0] + 1, EYE_BAND[3] - EYE_BAND[1] + 1, p.body);
    for (const ex of EYE_XS) {
      if (pose.eyes === "closed") {
        // Sleeping/blinking: a thick, content arc with upturned ends.
        cRect(ex - EYE_RX + 1, EYE_Y, EYE_RX * 2 - 1, 2, p.line);
        cRect(ex - EYE_RX, EYE_Y - 1, 1, 2, p.line);
        cRect(ex + EYE_RX, EYE_Y - 1, 1, 2, p.line);
        continue;
      }
      cEllipse(ex, EYE_Y, EYE_RX, EYE_RY, p.line);        // outline ring
      cEllipse(ex, EYE_Y, EYE_RX - 2, EYE_RY - 2, p.white); // the white
      // The pupil looks where the pose says (down = at the keyboard).
      const pj = pose.eyes === "down" ? EYE_Y + 2 : EYE_Y;
      const pr = pose.eyes === "wide" ? PUPIL_R + 1 : PUPIL_R;
      cEllipse(ex, pj, pr, pr, p.line);
      cRect(ex - 1, pj - pr + 1, 1, 1, p.white);          // the catch-light
    }
  }

  if (pose.mouth !== "open") {
    // Erase the mouth box (the cheek freckles sit outside it), then draw
    // the pose's mouth around the box's center column.
    cRect(MOUTH_BOX[0], MOUTH_BOX[1],
          MOUTH_BOX[2] - MOUTH_BOX[0] + 1, MOUTH_BOX[3] - MOUTH_BOX[1] + 1,
          p.body);
    const mc = Math.floor((MOUTH_BOX[0] + MOUTH_BOX[2]) / 2);
    const mt = MOUTH_BOX[1];
    if (pose.mouth === "o") {
      // A small surprised ring, dark rim around the tongue's red.
      cEllipse(mc, mt + 4, 4, 4, p.line);
      cEllipse(mc, mt + 4, 2, 2, p.red);
    } else if (pose.mouth === "flat") {
      cRect(mc - 8, mt + 2, 16, 2, p.line);               // a resting line
    } else {
      // A closed smile: a shallow arc built from three dashes.
      cRect(mc - 9, mt + 1, 3, 2, p.line);
      cRect(mc - 7, mt + 2, 14, 2, p.line);
      cRect(mc + 6, mt + 1, 3, 2, p.line);
    }
  }
}

/** A stubby outlined arm blob — scenes place these over the desk. */
function paw(x: number, y: number, w: number, h: number, p: Palette): void {
  outlined(x, y, w, h, Math.floor(Math.min(w, h) / 2), p.body, p.line);
  // Two knuckle nicks along the top edge (the reference paws have toes).
  rect(x + Math.floor(w / 3), y + S, S, S, p.line);
  rect(x + Math.floor((2 * w) / 3), y + S, S, S, p.line);
}

// --------------------------------------------------------------------------
//  Decorations (drawn over the blitted scenes)
// --------------------------------------------------------------------------

/** The battery badge (top-right corner) for battery-throttled states. */
function batteryBadge(w: number, p: Palette): void {
  outlined(w - 16 * S, 3 * S, 12 * S, 7 * S, S, p.paper, p.ink);
  rect(w - 4 * S, 5 * S, 2 * S, 3 * S, p.ink);                  // the nub
  rect(w - 14 * S, 5 * S, 4 * S, 3 * S, p.mid);                 // charge: low
}

// --------------------------------------------------------------------------
//  The scenes
// --------------------------------------------------------------------------

/** The scenes' focus bands (see blitScene) — horn tips to just past the
 *  desk edge for the desk scene; horn base to the soles of his feet for
 *  the tall tea scene. Measured from the extractions, like the anchors. */
const DESK_FOCUS: [number, number] = [12, 102];
const TEA_FOCUS: [number, number] = [36, 152];

/** Eye geometry per scene (outline half-size + pupil, in cells). */
const DESK_EYES: EyeGeom = { rx: 6, ry: 8, pr: 2 };
const TEA_EYES: EyeGeom = { rx: 5, ry: 6, pr: 2 };

/** Paint one full frame. The layout derives from the canvas size, so the
 *  same code composes wide, square and tall windows. */
export function drawScene(ctx: CanvasRenderingContext2D, input: SceneInput): void {
  g = ctx;
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  const p = palette(input);
  // The detail scale: fixed-size features double when the canvas gives
  // them room (256-wide gives them room; tiny canvases degrade gently).
  S = Math.min(w, h) >= 150 ? 2 : 1;

  // The screen background + a dot-matrix texture (every 3rd pixel both
  // ways gets a slightly darker dot — the handheld-LCD look). The
  // scenes' wall cells are transparent, so this shows through them.
  rect(0, 0, w, h, p.paper);
  for (let y = 0; y < h; y += 3) {
    for (let x = 0; x < w; x += 3) {
      rect(x, y, 1, 1, p.dot);
    }
  }

  const s = input.state;

  // ---- Loaded but not working: feet up, cup in paw (the reference's
  // tea scene serves BOTH idle and the after-task break — the same
  // well-earned recline either way).
  if (s === "idle" || s === "tea") {
    const bl = blitScene(TEA_SCENE, TEA_FOCUS[0], TEA_FOCUS[1], p);
    sceneFace(TEA_SCENE, bl, TEA_EYES, {
      eyes: input.blink ? "closed" : "open",
      mouth: "open", brows: "neutral",
    }, p);
    if (input.onBattery) batteryBadge(w, p);
    return;
  }

  // ---- Playing in the sand (the empty-chat greeting) ---------------------
  if (s === "play") {
    const portrait = h > w;
    const cx = Math.floor(w * (portrait ? 0.55 : 0.60));
    const bodyW = Math.floor(Math.min(w, h) * (portrait ? 0.52 : 0.56));
    // The sand: a warm strip along the bottom, dithered for texture.
    const sandY = Math.floor(h * 0.72);
    rect(0, sandY, w, 2 * S, p.ink);
    rect(0, sandY + 2 * S, w, h - sandY - 2 * S, p.horn);
    dither(0, sandY + 3 * S, w, h - sandY - 4 * S, p.bodyDim);
    // Seymour sits IN the sand: the blit is CROPPED at the sand line so
    // his lower half is buried under the surface, not standing on it.
    // The sink is fixed in SPRITE rows (surface at his upper belly, row
    // 70) so small canvases don't bury his mouth.
    const sk = Math.max(1, Math.round(bodyW / SPRITE_W));
    monster(cx, sandY - 59 * sk, bodyW, {
      eyes: input.blink ? "closed" : "open",
      mouth: "smile", brows: "neutral",
    }, p, sandY + 2 * S);
    paw(cx - Math.floor(bodyW * 0.42), sandY - 3 * S, 9 * S, 6 * S, p);
    paw(cx + Math.floor(bodyW * 0.20), sandY - 3 * S, 9 * S, 6 * S, p);
    // A sand mound to his left, with a shovel that wiggles as he digs.
    const moundX = Math.floor(w * 0.16);
    outlined(moundX, sandY - 8 * S, 20 * S, 10 * S, 4 * S, p.horn, p.ink);
    dither(moundX + 3 * S, sandY - 5 * S, 14 * S, 4 * S, p.bodyDim);
    const tilt = input.frame ? S : 0;          // the dig wiggle
    rect(moundX + 9 * S, sandY - 16 * S + tilt, S, 8 * S, p.ink);   // handle
    rect(moundX + 7 * S, sandY - 17 * S + tilt, 5 * S, 2 * S, p.mid);  // blade
    // A bucket to his right (upturned pail with a handle arc).
    const bx = Math.floor(w * 0.78);
    outlined(bx, sandY - 9 * S, 12 * S, 10 * S, 2 * S, p.mid, p.ink);
    rect(bx + 2 * S, sandY - 9 * S, 8 * S, S, p.ink);               // rim
    glyph(bx + 2 * S, sandY - 13 * S, [".##.", "#..#"], p.ink);     // handle
    return;
  }

  // ---- At the computer (working / waiting / blocked / sleeping) ----------
  // The reference desk scene, worn with the state's pose. The baked face
  // (eyes at us, open smile) is "working"'s own look — he types AND
  // beams at you, exactly as drawn.
  const pose: Pose =
    s === "working" ? { eyes: "open", mouth: "open", brows: "neutral" } :
    s === "sleeping" ? { eyes: "closed", mouth: "flat", brows: "neutral" } :
    s === "blocked" ? { eyes: "wide", mouth: "o", brows: "raised" } :
    /* waiting */     { eyes: "open", mouth: "flat", brows: "raised" };
  const bl = blitScene(DESK_SCENE, DESK_FOCUS[0], DESK_FOCUS[1], p);
  sceneFace(DESK_SCENE, bl,  DESK_EYES,
            s === "working" && input.blink ? { ...pose, eyes: "closed" } : pose,
            p);

  // ---- State dressings, anchored on the blitted head ----------------------
  // The head's right shoulder in canvas pixels (bubbles hang off it).
  const hx = bl.ox + (DESK_SCENE.eyes[2]![0] + 10) * bl.k;
  const hy = bl.oy + (DESK_SCENE.eyes[2]![1] - 14) * bl.k;
  if (s === "sleeping") {
    // A trail of z's floating up, stepping with the clock.
    const off = input.frame ? S : 0;
    glyph(hx, hy - off, ZEE, p.ink);
    glyph(hx + 6 * S, hy - 8 * S - off, ZEE, p.ink);
    glyph(hx + 12 * S, hy - 16 * S - off, ZEE, p.ink);
  }
  if (s === "blocked") {
    // A speech bubble with a bobbing question mark: "I need you!"
    const bob = input.frame ? S : 0;
    outlined(hx, hy - 10 * S - bob, 13 * S, 13 * S, 3 * S, p.white, p.line);
    glyph(hx + 2 * S, hy + 2 * S - bob, ["#."], p.line);    // bubble tail
    glyph(hx + 4 * S, hy - 7 * S - bob, QUESTION, p.line);
  }
  if (s === "waiting") {
    // A thought bubble with cycling dots: politely holding the door.
    outlined(hx, hy - 6 * S, 16 * S, 9 * S, 3 * S, p.white, p.line);
    const dots = input.frame ? 3 : 2;
    for (let i = 0; i < dots; i++) {
      rect(hx + (3 + i * 4) * S, hy - 2 * S, 2 * S, 2 * S, p.line);
    }
  }
  if (input.onBattery) batteryBadge(w, p);
}
