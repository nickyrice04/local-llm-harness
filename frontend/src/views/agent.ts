/** agent.ts — the AGENT organizer: discrete chat-started tasks.
 *
 * This tab is where agentic work is ORGANIZED: everything that has run or
 * is running as a discrete Tier-2 job ("find the sheet, combine it,
 * highlight the figures") — visual confirmation that background work is
 * genuinely happening, with the auditable journal one click away.
 * Starting new work happens in Chat: the button below jumps there with
 * the Agent mode preselected. The continuous seat is the Primary tab.
 */

import { get, post } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import { icon } from "../icons";
import type { ViewHandle } from "../main";
import { show as showView } from "../main";
import { presetMode, presetSession } from "./chat";

/** Task shapes from GET /api/tasks. */
interface Task {
  id: string; goal: string; status: string;
  notes: string; result: string; session_id: string;
  session_exists: boolean; created_at: string;
}

/** Which task's journal is open — module-level so it survives switches. */
let openTaskId: string | null = null;

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()

  /** One journal line, colored by kind via CSS classes. Lines carry the
   *  backend step id (when known) so the snapshot merge can DEDUP: a
   *  step that lands live while the snapshot GET is in flight is also
   *  IN that snapshot (the loop persists before publishing). */
  function journalLine(kind: string, content: string,
                       stepId?: number): HTMLElement {
    const line = el("div", { className: `k-${kind}` }, `[${kind}] ${content}`);
    if (stepId !== undefined) line.dataset.stepId = String(stepId);
    return line;
  }

  /** Render one task TILE for the organizer grid: a readable title, the
   *  status at a glance, and compact controls. An open journal expands
   *  the tile across the whole grid row (CSS .org-tile.open). */
  function taskTile(task: Task): HTMLElement {
    const controls: HTMLElement[] = [];
    if (task.status === "running") {
      controls.push(el("button", { onclick: () => act(task.id, "pause") }, "Pause"));
    }
    if (task.status === "paused" || task.status === "blocked") {
      controls.push(el("button.primary", { onclick: () => act(task.id, "resume") }, "Resume"));
    }
    if (["running", "queued", "paused", "blocked"].includes(task.status)) {
      controls.push(el("button.danger", { onclick: () => act(task.id, "cancel") }, "Cancel"));
    }
    controls.push(el("button", {
      onclick: () => { openTaskId = openTaskId === task.id ? null : task.id; refresh(); },
    }, openTaskId === task.id ? "Hide journal" : "Journal"));
    // Every task is a special kind of chat: jump back to the one that
    // started it. A task whose conversation was DELETED keeps its row
    // (result + journal stay readable right here) but says so instead
    // of opening a blank ghost (bug 4.3).
    if (task.session_id) {
      controls.push(task.session_exists
        ? el("button", {
            onclick: () => { presetSession(task.session_id); showView("chat"); },
          }, "Open chat")
        : el("button", { disabled: true, title: "the conversation this "
            + "task came from was deleted; its result and journal remain "
            + "here" }, "chat deleted"));
    }

    const tile = el("div.org-tile",
      { className: `org-tile${openTaskId === task.id ? " open" : ""}` },
      // Only the goal's first line is the TITLE — a spun-off task's goal
      // carries its inherited conversation context below it, which
      // belongs in the tooltip, not the tile.
      el("div.org-title", { title: task.goal }, task.goal.split("\n")[0] ?? ""),
      el("span.badge", { className: `badge ${task.status}` }, task.status),
      task.status === "blocked" ? blockedPrompt(task) : null,
      task.result ? el("div.org-meta", { title: task.result }, task.result) : null,
      el("div.org-actions", {}, ...controls),
    );

    // The live journal, when open (live lines merged after the snapshot,
    // minus any the snapshot already contains — dedup by step id).
    if (openTaskId === task.id) {
      const journal = el("div.journal", { id: `journal-${task.id}` });
      get<{ id: number; kind: string; content: string }[]>(`/api/tasks/${task.id}/journal`)
        .then((steps) => {
          const lastId = steps.length ? steps[steps.length - 1]!.id : 0;
          const liveTail = [...journal.children].filter((c) =>
            Number((c as HTMLElement).dataset.stepId ?? Infinity) > lastId);
          journal.replaceChildren(
            ...steps.map((s) => journalLine(s.kind, s.content, s.id)),
            ...liveTail);
          journal.scrollTop = journal.scrollHeight;
        });
      tile.append(journal);
    }
    return tile;
  }

  /** The answer box for an ask_user question. */
  function blockedPrompt(task: Task): HTMLElement {
    const answer = el("input", { placeholder: "Your answer…" });
    return el("div.row", {},
      answer,
      el("button.primary", {
        onclick: async () => {
          if (!answer.value.trim()) return;
          await post(`/api/tasks/${task.id}/respond`, { answer: answer.value });
          refresh();
        },
      }, "Answer & continue"),
    );
  }

  /** POST one lifecycle action then re-render. */
  async function act(taskId: string, action: string): Promise<void> {
    await post(`/api/tasks/${taskId}/${action}`);
    refresh();
  }

  /** Fetch the overview and redraw the whole panel: a GRID of tiles,
   *  with "start new" as the grid's first cell (the organizer pattern —
   *  the work itself lives in its conversation). */
  async function refresh(): Promise<void> {
    const overview = await get<{ tasks: Task[]; running: string[] }>("/api/tasks");
    const newTile = el("button.org-tile.new", {
      type: "button",
      onclick: () => { presetMode("agent"); showView("chat"); },
    });
    newTile.append(icon("plus"), el("span", {}, "Start a new agent task"));

    mount(container,
      el("h2", {}, "Agent tasks"),
      el("p.muted", {}, "Discrete jobs you started from chat — they plan, "
        + "use tools, and check results at foreground priority (they yield "
        + "to your live chat and outrank the background seat). Up to three "
        + "run at once; the rest queue. Each one is a conversation: its "
        + "progress and result live there."),
      el("div.org-grid", {}, newTile, ...overview.tasks.map(taskTile)),
    );
  }

  // Live updates on the discrete-task topic; step lines append in place.
  subs.push(on("tasks", (e) => {
    if (e.type === "step" && e.data.task_id === openTaskId) {
      const journal = document.getElementById(`journal-${openTaskId}`);
      if (journal) {
        journal.append(journalLine(e.data.kind, e.data.content, e.data.step_id));
        journal.scrollTop = journal.scrollHeight;
      }
    } else {
      refresh();                               // lifecycle change: redraw
    }
  }));
  // The loop journals on the shared "agent" step topic too — relay those
  // for open discrete journals (journal() publishes topic "agent").
  subs.push(on("agent", (e) => {
    if (e.type === "step" && e.data.task_id === openTaskId) {
      const journal = document.getElementById(`journal-${openTaskId}`);
      if (journal) {
        journal.append(journalLine(e.data.kind, e.data.content, e.data.step_id));
        journal.scrollTop = journal.scrollHeight;
      }
    }
  }));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
