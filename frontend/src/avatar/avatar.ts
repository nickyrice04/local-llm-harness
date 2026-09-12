/** avatar.ts — the state machine that gives Seymour a face.
 *
 * THE RULE (the reason this project is named after Papert): the avatar is
 * driven by REAL state transitions from the backend's event bus — never by
 * a timer pretending to be activity. The frame TOGGLE is on a timer (that's
 * animation); the STATE never is (that's honesty). If Seymour is sipping
 * tea, it is because the agent truly finished something recently.
 *
 * Derivation, most-urgent first:
 *   engine down/loading  → sleeping
 *   task blocked on user → blocked
 *   agent preempted/serial-yield → waiting
 *   agent has a running task     → working
 *   idle                         → tea (task done RECENTLY — it expires) / idle
 */

import { get } from "../api";
import { on, type BusEvent } from "../bus";
import { onPrefs, prefs, resolvedTheme, ACCENTS } from "../theme";
import { drawScene, type SceneInput } from "./scene";

/** What Seymour can be doing, as far as the face is concerned. */
export type AvatarState = SceneInput["state"];

/** How long a finished task keeps Seymour in his tea break. After this the
 *  break "expires" back to idle — 'recently done' must actually mean
 *  recently, and a cancelled task must not read as a completed one. */
const TEA_BREAK_MS = 5 * 60 * 1000;

/** The quips: encouraging lines per state, rotated on ENTRY, never on a
 *  timer (a quip change without a state change would be a small lie). */
const QUIPS: Record<AvatarState, string[]> = {
  working:  ["Tackling the next big task!", "On it — watch me go.",
             "Making progress, bit by bit."],
  tea:      ["All caught up — tea time.", "Sipping tea until something needs me."],
  idle:     ["Waiting for you!", "Give me a task — I love tasks.",
             "Three eyes, ready to see more."],
  waiting:  ["You first — I'll keep working after.", "Yielding to you. Your chat wins."],
  blocked:  ["I need your input to continue!", "Quick question for you —"],
  sleeping: ["zzz… (no model loaded yet)", "zzz…"],
  play:     ["Sandcastles while I wait!"],   // decorative only (empty chat)
};

/** Human-readable state labels for the line under the canvas. */
const LABELS: Record<AvatarState, string> = {
  working: "working on your task",
  tea: "task done — on break",
  idle: "idle — no task yet",
  waiting: "yielding to you",
  blocked: "needs your input",
  sleeping: "engine loading / offline",
  play: "playing in the sand",              // decorative only (empty chat)
};

export class Avatar {
  private ctx: CanvasRenderingContext2D;
  private state: AvatarState = "sleeping";     // truthful default: not ready yet
  private frame = 0;                           // which animation frame shows
  private onBattery = false;                   // adds the battery badge
  // The facts the state derives from (updated only by real events):
  private engineReady = false;
  private taskRunning = false;
  private taskBlocked = false;
  private doneAt = 0;                          // ms timestamp of last completion
  private yieldingUntil = 0;                   // ms timestamp; waiting state

  constructor(
    private canvas: HTMLCanvasElement,
    private stateEl: HTMLElement,              // the label line
    private quipEl: HTMLElement,               // the speech bubble
  ) {
    this.ctx = canvas.getContext("2d")!;
    this.subscribe();
    // The ANIMATION clock: flips frames only. State changes come from
    // events — but note derive() runs here too, because two states decay
    // by TIME (tea expires; 'waiting' falls back to working): the clock
    // may notice an expiry, never invent activity.
    setInterval(() => {
      this.frame = 1 - this.frame;
      this.derive();
      this.paint();
    }, 600);
    // Theme changes repaint with the new palette.
    onPrefs(() => this.paint());
    // The canvas' logical resolution follows the FRAME's live shape (the
    // user resizes the window freely): observe it and refit.
    const frame = document.getElementById("avatar-frame");
    if (frame) new ResizeObserver(() => this.fitCanvas(frame)).observe(frame);
    if (frame) this.fitCanvas(frame);
    // First paint, including the label and quip for the starting state.
    this.stateEl.textContent = LABELS[this.state];
    this.quipEl.textContent = QUIPS[this.state][0] ?? "";
    this.paint();
    // Seed the facts from the status endpoint: events only report
    // TRANSITIONS, and a page opened after startup would otherwise show
    // "sleeping" forever on an engine that is already up.
    this.seed();
  }

  /** Match the canvas' logical resolution to the frame's current shape.
   *  Width is fixed at the art's native detail level; height follows the
   *  frame's aspect (clamped to shapes the scenes compose well in), so a
   *  user-resized window letterboxes minimally without distorting. */
  private fitCanvas(frame: HTMLElement): void {
    const box = frame.getBoundingClientRect();
    if (box.width < 40 || box.height < 40) return;   // not laid out yet
    const w = 256;                                   // native art width
    // The clamp is wide (0.45–2.2): the scenes COVER any aspect (they
    // crop and edge-extend), so the canvas can follow the frame's true
    // shape and the CSS contain-fit never letterboxes in practice.
    const h = Math.round(w * Math.min(2.2, Math.max(0.45, box.height / box.width)));
    // Re-size only on real change — setting canvas.width clears the bitmap.
    if (this.canvas.width !== w || Math.abs(this.canvas.height - h) > 6) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.paint();
    }
  }

  /** One-time catch-up with reality for a freshly-opened page. */
  private async seed(): Promise<void> {
    try {
      const status = await get<{ capabilities: unknown }>("/api/status");
      this.engineReady = status.capabilities != null;
      const agent = await get<{ tasks: { status: string }[] }>("/api/agent");
      this.taskRunning = agent.tasks.some((t) => t.status === "running");
      this.taskBlocked = agent.tasks.some((t) => t.status === "blocked");
      this.derive();
    } catch { /* backend not up yet — events will catch us up */ }
  }

  /** Wire the real transitions. Each handler updates a FACT, then
   *  re-derives the state — the derivation lives in one place. */
  private subscribe(): void {
    on("engine", (e) => {
      if (["ready", "handshake_done", "swapped"].includes(e.type)) this.engineReady = true;
      if (["loading", "swapping", "stopped", "swap_failed"].includes(e.type)) this.engineReady = false;
      this.derive();
    });
    on("agent", (e) => this.onAgentEvent(e));
    on("connection", () => { this.engineReady = false; this.derive(); });
    // Battery is a badge, not a state — the agent keeps working, slower.
    on("agent", (e) => {
      if (e.type === "throttled") { this.onBattery = true; this.paint(); }
    });
  }

  /** Agent lifecycle facts. */
  private onAgentEvent(e: BusEvent): void {
    switch (e.type) {
      case "task_started":
      case "task_resumed":
        this.taskRunning = true; this.taskBlocked = false;
        this.doneAt = 0; break;                // a new job ends any break
      case "task_done":
        this.taskRunning = false; this.doneAt = Date.now(); break;
      case "task_paused":
      case "idle":
        this.taskRunning = false; this.taskBlocked = false; break;
      case "task_cancelled":
        // Cancelled ≠ done: no tea break for abandoned work.
        this.taskRunning = false; this.taskBlocked = false;
        this.doneAt = 0; break;
      case "task_blocked":
        this.taskBlocked = true; this.taskRunning = false; break;
      case "preempted":
        // A real yield just happened; show "waiting" until the agent's
        // next step event proves it's back in.
        this.yieldingUntil = Date.now() + 8000; break;
      case "step":
        this.yieldingUntil = 0; break;         // back to visible work
      default:
        return;
    }
    this.derive();
  }

  /** THE derivation: facts → one state, priority ordered (see header). */
  private derive(): void {
    let next: AvatarState;
    if (!this.engineReady) next = "sleeping";
    else if (this.taskBlocked) next = "blocked";
    else if (this.taskRunning && Date.now() < this.yieldingUntil) next = "waiting";
    else if (this.taskRunning) next = "working";
    else if (this.doneAt && Date.now() - this.doneAt < TEA_BREAK_MS) next = "tea";
    else next = "idle";
    if (next !== this.state) {
      this.state = next;
      // A new quip on every genuine transition (and only then).
      const options = QUIPS[next];
      this.quipEl.textContent = options[Math.floor(Math.random() * options.length)] ?? "";
      this.stateEl.textContent = LABELS[next];
      this.paint();
    }
  }

  /** Paint the current state's current frame onto the canvas. */
  private paint(): void {
    // Blink logic: the closed-eye frame appears only occasionally (1 in
    // ~4 animation ticks) so idling reads calm, not twitchy.
    const blink = this.frame === 1 && Math.random() < 0.28;
    drawScene(this.ctx, {
      state: this.state,
      frame: prefs().motion === "reduced" ? 0 : this.frame,
      blink,
      onBattery: this.onBattery,
      theme: resolvedTheme(),
      avatarHue: prefs().avatarHue, screen: prefs().avatarScreen,
      accentHue: ACCENTS[prefs().accent] ?? 90,
    });
  }
}
