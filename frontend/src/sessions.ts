/** sessions.ts — the conversation rail: EVERY conversation, on every view.
 *
 * One list under the "New conversation" button: plain chats, agent tasks,
 * research runs — because "deep research is a chat, just a special kind
 * of chat" (the OOP framing: one base class, many subclasses). Each row
 * reads  [title] … • [kind symbol] • [cycle animation while live] — the
 * separators keep the symbol from crowding the title, and the cycle only
 * exists while something the conversation started actually holds an
 * engine slot (the scheduler's real ledger, never a timer).
 *
 * Mounted ONCE at boot (main.ts) and never destroyed: unlike views, the
 * rail is on every screen, so its bus subscriptions live for the app's
 * lifetime — which is also why starting new work or opening history is
 * possible from ANY tab, not just Chat.
 */

import { del, get } from "./api";
import { on } from "./bus";
import { el, mount } from "./dom";
import { icon } from "./icons";
import { activeView, show as showView } from "./main";
import { currentSessionId, presetNew, presetSession } from "./views/chat";

/** The rail's container (#sessions), remembered at init. */
let box: HTMLElement | null = null;

/** Wire the rail up (called once from main.ts). */
export function initSessionsRail(container: HTMLElement): void {
  box = container;
  // Live title updates (the backend names new conversations async).
  on("chat", (event) => {
    if (event.type === "title") refreshSessions();
  });
  // The cycle animations follow the scheduler's real ledger: refresh when
  // slots are granted/released (debounced — grants can burst).
  let timer: number | undefined;
  on("scheduler", (event) => {
    if (!["granted", "released", "abandoned"].includes(event.type)) return;
    clearTimeout(timer);
    timer = setTimeout(refreshSessions, 400) as unknown as number;
  });
  refreshSessions();
}

/** Redraw the whole rail (cheap: one bounded GET, tiny DOM). */
export async function refreshSessions(): Promise<void> {
  if (!box) return;
  const sessions = await get<{
    id: string; title: string; kind: string; active: boolean;
  }[]>("/api/sessions");
  const current = currentSessionId();

  const newButton = el("button.new-chat", {
    type: "button",
    onclick: () => { presetNew(); showView("chat"); },
  });
  newButton.append(icon("plus"), document.createTextNode("New conversation"));

  const items = sessions.map((s) => {
    const item = el("div.session-item", {
      className: `session-item${s.id === current ? " active" : ""}`,
      // The whole row opens the conversation (jumping to Chat if the
      // user clicked from an organizer tab).
      onclick: () => { presetSession(s.id); showView("chat"); },
    });
    // [title] — ellipsized; when nothing is running it simply gets more
    // of the row (the cycle's spot only exists while it's true).
    item.append(el("span.sess-title", { title: s.title }, s.title));
    // • [kind symbol] — the same pixel icon as the matching tab.
    item.append(el("span.sess-sep", {}, "•"));
    const kindIcon = icon(
      s.kind === "research" ? "research"
        : s.kind === "agent" ? "agent"
        : s.kind === "primary" ? "primary" : "chat");
    kindIcon.classList.add("kind-icon");
    item.append(kindIcon);
    // • [cycle] — only while live in a slot (stepped rotation keeps the
    // pixel look; see .live-cycle in styles.css).
    if (s.active) {
      item.append(el("span.sess-sep", {}, "•"));
      const live = el("span.live-cycle", { title: "running right now" });
      live.append(icon("cycle"));
      item.append(live);
    }
    item.append(el("button.del", {
      type: "button",
      onclick: async (event: Event) => {
        event.stopPropagation();       // deleting must not also OPEN it
        try {
          // The backend cancels the conversation's running work FIRST,
          // awaited — and refuses the delete if that fails (4.1). A
          // refusal surfaces; it never silently half-deletes.
          await del(`/api/sessions/${s.id}`);
        } catch (error: any) {
          alert(error?.message ?? "could not delete the conversation");
          return;
        }
        if (currentSessionId() === s.id) {
          presetNew();
          // Only remount chat if the user is LOOKING at chat — deleting
          // from another tab must not yank them away from it.
          if (activeView() === "chat") showView("chat");
        }
        refreshSessions();
      },
    }, "×"));
    return item;
  });
  mount(box, newButton, ...items);
}
