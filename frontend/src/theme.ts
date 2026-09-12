/** theme.ts — the user's look-and-feel, in one place.
 *
 * Five preferences: dark/light, an accent color, Seymour's own color, the
 * layout orientation, and reduced motion. They persist in localStorage
 * (this is a one-person local app — the browser IS the profile), apply
 * instantly as CSS custom properties / body classes, and anything that
 * paints outside CSS (the avatar canvas) subscribes for changes.
 */

/** Everything the user can choose about the app's appearance. */
export interface Prefs {
  theme: "dark" | "light" | "system";       // color scheme
  accent: string;                           // a named entry of ACCENTS
  avatarHue: number;                        // Seymour's body color (0–360)
  bezel: string;                            // the avatar frame's color
  motion: "full" | "reduced";               // animation intensity
  // Seymour's reserved area: which side it lives on, or "off" to give
  // the whole width to the work area.
  avatarSide: "right" | "left" | "off";
  // Seymour's screen look: tint from the app theme, or the classic
  // Game Boy four-green LCD (a pure canvas-palette choice — no CSS).
  avatarScreen: "theme" | "gameboy";
  avatarPanelWidth: number;                 // the reserved column's width, px
  avatarFrameHeight: number;                // the frame's height inside it, px
}

/** The accent palette: name → hue (saturation/lightness live in CSS so
 *  dark and light themes can shade the same hue differently). */
export const ACCENTS: Record<string, number> = {
  gameboy: 90,      // the classic olive-green LCD
  sky: 205,         // calm blue
  mint: 160,        // fresh green-teal
  amber: 40,        // warm CRT amber
  berry: 340,       // pink-red
  violet: 265,      // purple
};

/** A few good starting colors for Seymour himself (hue values). */
export const AVATAR_HUES: Record<string, number> = {
  "classic blue": 215,
  "swamp green": 110,
  "toasty orange": 30,
  "bubblegum": 330,
  "grape": 270,
  "gameboy olive": 80,
};

/** Frame colors for the avatar window's bezel (name → hue). The classic
 *  console blue is the reference art's frame. */
export const BEZELS: Record<string, number> = {
  "console blue": 215,
  slate: 210,           // desaturated via CSS (see --bezel-s below)
  crimson: 355,
  sand: 40,
  evergreen: 150,
  grape: 270,
};

/** The defaults a fresh install wakes up with. */
const DEFAULTS: Prefs = {
  theme: "dark",
  accent: "gameboy",
  avatarHue: 215,          // the classic blue monster from the reference art
  bezel: "console blue",   // the frame from the reference art
  motion: "full",
  avatarSide: "right",     // the reserved column, where it always lived
  avatarScreen: "theme",   // match the app's colors by default
  avatarPanelWidth: 260,
  avatarFrameHeight: 190,
};

/** The localStorage key all of this lives under. */
const STORE_KEY = "seymour-prefs";

/** Subscribers told whenever prefs change (the avatar repaints itself). */
const listeners = new Set<(p: Prefs) => void>();

/** The live preferences (loaded once, mutated through save()). */
let current: Prefs = load();

/** Read prefs from localStorage, filling gaps with defaults. */
function load(): Prefs {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    // Spread over defaults: unknown/missing keys degrade gracefully.
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS };
  } catch {
    return { ...DEFAULTS };            // corrupted storage → defaults
  }
}

/** The current preferences (read-only copy). */
export function prefs(): Prefs {
  return { ...current };
}

/** Merge a partial change, persist it, apply it, and notify listeners. */
export function save(change: Partial<Prefs>): void {
  current = { ...current, ...change };
  try { localStorage.setItem(STORE_KEY, JSON.stringify(current)); } catch { /* private mode */ }
  apply();
  listeners.forEach((fn) => fn(current));
}

/** Subscribe to preference changes; returns the unsubscribe function. */
export function onPrefs(fn: (p: Prefs) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Resolve the "system" theme choice against what the OS says. */
export function resolvedTheme(): "dark" | "light" {
  if (current.theme !== "system") return current.theme;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Push the current preferences into the DOM: one data attribute for the
 *  scheme, CSS variables for the hues and panel sizes, and a body class
 *  for which side Seymour's reserved area lives on (or off). */
export function apply(): void {
  const root = document.documentElement;
  root.dataset.theme = resolvedTheme();
  root.style.setProperty("--accent-h", String(ACCENTS[current.accent] ?? 90));
  root.style.setProperty("--avatar-h", String(current.avatarHue));
  root.style.setProperty("--bezel-h", String(BEZELS[current.bezel] ?? 215));
  // The reserved area's geometry (clamped so it can't eat the app).
  root.style.setProperty("--companion-w",
    `${Math.min(420, Math.max(200, current.avatarPanelWidth))}px`);
  root.style.setProperty("--frame-h",
    `${Math.min(400, Math.max(120, current.avatarFrameHeight))}px`);
  document.body.classList.toggle("avatar-left", current.avatarSide === "left");
  document.body.classList.toggle("avatar-off", current.avatarSide === "off");
  document.body.classList.toggle("reduced-motion", current.motion === "reduced");
}

/** Boot-time hookup: apply now, and re-apply when the OS scheme changes
 *  (the only external input "system" depends on). */
export function initTheme(): void {
  apply();
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    apply();
    listeners.forEach((fn) => fn(current));   // avatar re-derives its palette
  });
}
