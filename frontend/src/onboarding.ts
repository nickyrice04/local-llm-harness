/** onboarding.ts — the first-launch tour, full-screen.
 *
 * A stepped, animated welcome: Seymour himself fills a framed screen at
 * the top of each card and ACTS OUT the step (typing for the agent step,
 * tea for the break step, sleeping for the "get a model" step), while the
 * text explains the idea. The final step ends, as requested, with
 * "Let's get started." Shown once (an app_state flag server-side),
 * dismissible at any point, and replayable from Settings.
 */

import { get, post } from "./api";
import { el } from "./dom";
import { drawScene, type SceneInput } from "./avatar/scene";
import { ACCENTS, AVATAR_HUES, prefs, resolvedTheme, save } from "./theme";

/** One step: a title, its body, and which scene Seymour performs. */
interface Step {
  title: string;
  body: string;
  scene: SceneInput["state"];
  custom?: () => HTMLElement;         // extra content (the style pickers)
}

const STEPS: Step[] = [
  {
    title: "Hi — I'm Seymour!",
    scene: "idle",
    body: "A local-first AI workspace: every model, every conversation, "
      + "every memory lives on THIS machine — nothing you type leaves it. "
      + "I'm named after Seymour Papert, and with three eyes… I see more.",
  },
  {
    title: "One brain, no taking turns",
    scene: "working",
    body: "My trick: a background agent and your chat share one loaded "
      + "model AT THE SAME TIME. Most local apps make the agent stop "
      + "whenever you speak — mine keeps working while your reply streams. "
      + "The banner up top always tells you which mode is live and why.",
  },
  {
    title: "Your chat always wins",
    scene: "waiting",
    body: "When every slot is busy, your question preempts my background "
      + "work — I checkpoint, step aside, and pick my task back up after. "
      + "And I'm guaranteed a floor of progress, so my work never starves "
      + "either. Fast for you, steady for me.",
  },
  {
    title: "Give me the big jobs",
    scene: "working",
    body: "The Agent tab is my desk: hand me a long-running task — "
      + "\"research 20 job openings\", \"summarize everything in the "
      + "workspace\" — and keep chatting while I type away. Every step I "
      + "take lands in a journal you can audit, I save checkpoints so "
      + "nothing is lost, and I ALWAYS ask before anything irreversible.",
  },
  {
    title: "Research, memory, soul",
    scene: "tea",
    body: "Deep research runs multi-round search-and-read pipelines while "
      + "you work. Memory is everything I remember between conversations — "
      + "reviewable, correctable, deletable. And the Soul tab is my "
      + "character file: edit it and I become who you write.",
  },
  {
    title: "Make me yours",
    scene: "idle",
    body: "Pick a theme, an accent, and my color — watch me repaint live. "
      + "You can change all of this later in Settings, including which "
      + "side my little area lives on (and its frame color).",
    custom: stylePickers,
  },
  {
    title: "First: give me a brain",
    scene: "sleeping",
    body: "I'm asleep until a model loads. Open the Models tab to use a "
      + "GGUF already on disk, or search Hugging Face and download one. "
      + "When it loads I run a startup handshake to MEASURE what the model "
      + "can really do — never assume. After that: chat, tasks, tea.",
  },
];

/** The style-picker block for the "make me yours" step. */
function stylePickers(): HTMLElement {
  /** A row of labelled hue swatches writing straight to theme.save(). */
  const swatchRow = (entries: Record<string, number>, chosen: number,
                     pick: (hue: number) => void) =>
    el("div.pill-row", {}, ...Object.entries(entries).map(([name, hue]) =>
      el("button", {
        className: `choice swatch${hue === chosen ? " active" : ""}`,
        title: name,
        style: `--swatch-h:${hue}`,
        onclick: (event: Event) => {
          pick(hue);
          // Re-mark the active swatch in place (no full redraw needed).
          const rowEl = (event.currentTarget as HTMLElement).parentElement!;
          rowEl.querySelectorAll(".choice").forEach((n) => n.classList.remove("active"));
          (event.currentTarget as HTMLElement).classList.add("active");
        },
      }, name)));

  const p = prefs();
  return el("div", {},
    el("div.pill-row", {}, ...(["dark", "light"] as const).map((theme) =>
      el("button", {
        className: `choice${p.theme === theme ? " active" : ""}`,
        onclick: (event: Event) => {
          save({ theme });
          const rowEl = (event.currentTarget as HTMLElement).parentElement!;
          rowEl.querySelectorAll(".choice").forEach((n) => n.classList.remove("active"));
          (event.currentTarget as HTMLElement).classList.add("active");
        },
      }, theme))),
    swatchRow(ACCENTS, ACCENTS[p.accent] ?? 90, (hue) => {
      const name = Object.entries(ACCENTS).find(([, h]) => h === hue)?.[0] ?? "gameboy";
      save({ accent: name });
    }),
    swatchRow(AVATAR_HUES, p.avatarHue, (avatarHue) => save({ avatarHue })),
  );
}

/** The overlay root, remembered so replayTour() can reuse it. */
let overlayRoot: HTMLElement | null = null;

/** Show the tour if this is the first launch. */
export async function maybeShowTour(root: HTMLElement): Promise<void> {
  overlayRoot = root;
  const state = await get<{ done: boolean }>("/api/onboarding");
  if (state.done) return;
  runTour(root);
}

/** Settings' "replay the tour" entry point. */
export function replayTour(): void {
  if (overlayRoot) runTour(overlayRoot);
}

/** The tour itself: one full-screen scrim, redrawn per step. */
function runTour(root: HTMLElement): void {
  let step = 0;
  let frame = 0;
  let ticker: number | null = null;    // the scene's animation clock

  /** Persist the flag and remove the overlay. */
  const finish = async () => {
    if (ticker !== null) clearInterval(ticker);
    root.replaceChildren();
    await post("/api/onboarding/done");
  };

  /** (Re)draw the current step, full-screen. */
  const draw = () => {
    const current = STEPS[step]!;
    const isLast = step === STEPS.length - 1;

    // Seymour's stage: a bezel-framed canvas acting out the step's scene.
    const canvas = el("canvas.tour-canvas") as unknown as HTMLCanvasElement;
    canvas.width = 168;
    canvas.height = 110;
    const paint = () => drawScene(canvas.getContext("2d")!, {
      state: current.scene,
      frame,
      blink: frame === 1 && Math.random() < 0.3,
      onBattery: false,
      theme: resolvedTheme(),
      avatarHue: prefs().avatarHue, screen: prefs().avatarScreen,
      accentHue: ACCENTS[prefs().accent] ?? 90,
    });
    // One clock per drawn step (the old one dies with each redraw).
    if (ticker !== null) clearInterval(ticker);
    ticker = setInterval(() => { frame = 1 - frame; paint(); }, 600) as unknown as number;
    paint();

    // The progress dots, current one highlighted.
    const dots = el("div.dots", {}, ...STEPS.map((_, index) =>
      el("span", { className: index === step ? "on" : "" })));

    root.replaceChildren(el("div.scrim", {},
      el("div.tour", {},
        el("div.tour-frame", {}, canvas),
        el("h2", {}, current.title),
        el("p", {}, current.body),
        current.custom ? current.custom() : null,
        dots,
        el("div.row.tour-nav", {},
          el("button", { onclick: finish }, "Skip"),
          el("div.grow", {}),
          step > 0 ? el("button", { onclick: () => { step--; draw(); } }, "Back") : null,
          el("button.primary", {
            onclick: () => { isLast ? finish() : (step++, draw()); },
          }, isLast ? "Let's get started" : "Next"),
        ),
      ),
    ));
  };

  draw();
}
