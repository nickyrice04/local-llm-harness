/** primary.ts — the PRIMARY agent: Seymour's continuous background seat.
 *
 * One dedicated worker at Tier 3, designed for open-ended, long-horizon
 * work ("keep an eye on new job postings") — often there is no end until
 * the user stops it. More jobs queue behind the seat. Discrete "do this
 * now" tasks live in the Agent tab; this is the marathon runner.
 *
 * The centerpiece is the JOURNAL: the agent's visible work log, streamed
 * live over the event bus. Nothing here is decorative — every line was
 * persisted by the loop as it happened (auditable honesty).
 */

import { get, post } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";

/** Task shapes from GET /api/agent. */
interface Task {
  id: string; goal: string; status: string;
  notes: string; result: string; created_at: string;
}

/** Which task's journal is open — module-level so it survives switches. */
let openTaskId: string | null = null;

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()

  /** One journal line, colored by kind via CSS classes. Lines carry the
   *  backend step id (when known) so the snapshot merge can dedup. */
  function journalLine(kind: string, content: string,
                       stepId?: number): HTMLElement {
    const line = el("div", { className: `k-${kind}` }, `[${kind}] ${content}`);
    if (stepId !== undefined) line.dataset.stepId = String(stepId);
    return line;
  }

  /** Render one task TILE for the organizer grid. An open journal
   *  expands the tile across the whole grid row (CSS .org-tile.open). */
  function taskTile(task: Task): HTMLElement {
    const controls: HTMLElement[] = [];
    if (task.status === "running") {
      controls.push(el("button", { onclick: () => act(task.id, "pause") }, "Pause"));
    }
    if (task.status === "paused" || task.status === "blocked") {
      controls.push(el("button.primary", { onclick: () => act(task.id, "resume") }, "Resume"));
    }
    if (["running", "paused", "blocked", "queued"].includes(task.status)) {
      controls.push(el("button.danger", { onclick: () => act(task.id, "cancel") }, "Cancel"));
    }
    controls.push(el("button", {
      onclick: () => { openTaskId = openTaskId === task.id ? null : task.id; refresh(); },
    }, openTaskId === task.id ? "Hide journal" : "Journal"));

    const tile = el("div.org-tile",
      { className: `org-tile${openTaskId === task.id ? " open" : ""}` },
      el("div.org-title", { title: task.goal }, task.goal.split("\n")[0] ?? ""),
      el("span.badge", { className: `badge ${task.status}` }, task.status),
      task.status === "blocked" ? blockedPrompt(task) : null,
      task.result ? el("div.org-meta", { title: task.result }, task.result) : null,
      el("div.org-actions", {}, ...controls),
    );

    // The live journal, when open. Live lines that land while the
    // snapshot fetch is in flight are preserved — minus any the
    // snapshot already contains (dedup by step id).
    if (openTaskId === task.id) {
      const journal = el("div.journal", { id: `journal-${task.id}` });
      get<{ id: number; kind: string; content: string }[]>(`/api/agent/tasks/${task.id}/journal`)
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
          await post(`/api/agent/tasks/${task.id}/respond`, { answer: answer.value });
          refresh();
        },
      }, "Answer & continue"),
    );
  }

  /** POST one lifecycle action then re-render. */
  async function act(taskId: string, action: string): Promise<void> {
    await post(`/api/agent/tasks/${taskId}/${action}`);
    refresh();
  }

  /** Fetch the overview and redraw the whole panel: a GRID of tiles,
   *  with the new-goal composer as the grid's first cell. */
  async function refresh(): Promise<void> {
    const overview = await get<{ tasks: Task[]; on_battery: boolean }>("/api/agent");
    const goal = el("input", {
      placeholder: "Give Seymour continuous work…",
    });
    const newTile = el("div.org-tile.new-form", {},
      el("form.composer", {
        onsubmit: async (event: Event) => {
          event.preventDefault();
          if (!goal.value.trim()) return;
          await post("/api/agent/tasks", { goal: goal.value });
          goal.value = "";
          refresh();
        },
      }, goal, el("button.primary", {}, "Start")),
      el("p.muted", {}, "e.g. \"keep a running summary of new research "
        + "files in the workspace\""),
    );

    mount(container,
      el("h2", {}, "Primary agent"),
      el("p.muted", {}, "Seymour's continuous background seat: one dedicated "
        + "worker at the lowest priority with a guaranteed floor. Built for "
        + "long-horizon, often open-ended work — your chat always stays "
        + "fast, and this seat always keeps moving. Long tasks checkpoint "
        + "their notes, so nothing is lost across restarts."),
      overview.on_battery
        ? el("p.muted", {}, "On battery — pacing itself.") : null,
      el("div.org-grid", {}, newTile, ...overview.tasks.map(taskTile)),
    );
  }

  // Live updates: journal lines append in place; lifecycle changes redraw.
  subs.push(on("agent", (e) => {
    if (e.type === "step" && e.data.task_id === openTaskId) {
      const journal = document.getElementById(`journal-${openTaskId}`);
      if (journal) {
        journal.append(journalLine(e.data.kind, e.data.content, e.data.step_id));
        journal.scrollTop = journal.scrollHeight;
      }
    } else if (e.type.startsWith("task_") || e.type === "idle") {
      refresh();
    }
  }));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
