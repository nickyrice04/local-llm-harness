/** main.ts — boot: theme, bus, avatar, and the view LIFECYCLE.
 *
 * There is no framework and no router — a click swaps which view owns
 * #view. The one rule every view must follow (learned the hard way from
 * review findings): a view that is not showing must not touch the DOM.
 * That is enforced structurally: show() DESTROYS the outgoing view —
 * views return a handle whose destroy() unsubscribes their bus handlers
 * and aborts their in-flight streams — so a background event can never
 * hijack the screen from a view that isn't there anymore.
 */

import { connectProbe } from "./probe";
import { initCodePane, toggle as toggleCode } from "./code";
import { connect } from "./bus";
import { Avatar } from "./avatar/avatar";
import { initAvatarWindow } from "./avatar/window";
import { initTheme } from "./theme";
import { icon } from "./icons";
import { maybeShowTour } from "./onboarding";
import { initSessionsRail } from "./sessions";
import * as chat from "./views/chat";
import * as agent from "./views/agent";
import * as primary from "./views/primary";
import * as research from "./views/research";
import * as models from "./views/models";
import * as runs from "./views/runs";
import * as memory from "./views/memory";
import * as gallery from "./views/gallery";
import * as soul from "./views/soul";
import * as settings from "./views/settings";

/** What every view hands back: how to tear it down. */
export interface ViewHandle {
  destroy(): void;
}

// ---- The always-on pieces --------------------------------------------------
// The user's saved look, applied before anything renders (no flash).
initTheme();
// The event stream (reconnects itself forever).
connect();
connectProbe();                 // check_page's browser end (probe.ts)
// The brand mark and the window's title-bar eye: the PIXEL eye, so every
// piece of chrome speaks the same visual language.
document.getElementById("brand-eye")?.append(icon("eye"));
document.getElementById("aw-eye")?.append(icon("eye"));
// Seymour's floating window: drag, resize, remember, health vitals.
initAvatarWindow();
// Seymour himself, listening to the same events as everything else.
new Avatar(
  document.getElementById("avatar") as HTMLCanvasElement,
  document.getElementById("avatar-state")!,
  document.getElementById("avatar-quip")!,
);

// ---- Navigation ------------------------------------------------------------
const viewRoot = document.getElementById("view")!;
const sessionsBox = document.getElementById("sessions")!;

/** view name → its factory. Each returns a handle for teardown. */
const VIEWS: Record<string, () => ViewHandle> = {
  chat: () => chat.show(viewRoot),
  // The trace tab (replaced the old Agent organizer: with the mode
  // collapse there are no "agent tasks", only runs that used tools).
  runs: () => runs.show(viewRoot),
  agent: () => agent.show(viewRoot),
  primary: () => primary.show(viewRoot),
  research: () => research.show(viewRoot),
  models: () => models.show(viewRoot),
  memory: () => memory.show(viewRoot),
  gallery: () => gallery.show(viewRoot),
  soul: () => soul.show(viewRoot),
  settings: () => settings.show(viewRoot),
};

/** The live view's teardown handle (null before the first show). */
let current: ViewHandle | null = null;

/** The live view's NAME (the sessions rail asks, so deleting the open
 *  conversation from another tab doesn't yank the user to Chat). */
let currentName = "";

export function activeView(): string {
  return currentName;
}

/** Activate a view: destroy the old one FIRST, then render the new.
 *  The sessions rail stays put on EVERY view — new conversations and
 *  history are reachable from any tab (it lives outside the view root). */
export function show(name: string): void {
  current?.destroy();
  currentName = name in VIEWS ? name : "chat";
  current = (VIEWS[name] ?? VIEWS.chat!)();
  document.querySelectorAll<HTMLButtonElement>(".nav-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
}

// Build the nav buttons: a pixel icon + a label each (the icon set lives
// in icons.ts; emoji would fight the retro look). The order tells the
// product story: talk → discrete work → runs → the continuous seat →
// everything that supports them.
const NAV: [string, string][] = [
  ["chat", "Chat"], ["runs", "Runs"], ["research", "Research"],
  ["primary", "Primary"], ["models", "Models"], ["memory", "Memory"],
  ["gallery", "Gallery"], ["soul", "Soul"], ["settings", "Settings"],
];
const navBox = document.getElementById("nav")!;
for (const [name, label] of NAV) {
  const button = document.createElement("button");
  button.className = "nav-btn";
  button.dataset.view = name;
  // Runs reuses the wrench (tool work is what a traced run mostly is).
  button.append(icon(name === "runs" ? "agent" : name), label);
  button.onclick = () => show(name);
  navBox.append(button);
  if (name === "runs") {
    // Code is a PANE, not a view: toggling it never calls show(), so the
    // chat (and any run it is streaming) stays exactly where it is.
    const code = document.createElement("button");
    code.className = "nav-btn";
    code.dataset.view = "code";
    code.append(icon("code"), "Code");
    code.onclick = () => toggleCode();
    navBox.append(code);
  }
}
initCodePane(document.getElementById("code-pane")!);

// The conversation rail: every conversation, on every view (it mounts
// once and lives for the app's lifetime — see sessions.ts).
initSessionsRail(sessionsBox);

// Land on chat, and offer the tour on a first launch.
show("chat");
maybeShowTour(document.getElementById("overlay-root")!);
