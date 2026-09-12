/** research.ts — deep research runs (Tier 2: started by you, watched
 *  loosely). Live phase updates arrive over the bus; the report body is
 *  fetched on each phase change so the evolving draft is visible.
 */

import { get, post } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import { icon } from "../icons";
import { renderMarkdown } from "../md";
import type { ViewHandle } from "../main";
import { show as showView } from "../main";
import { presetMode, presetSession } from "./chat";
import { openDocument, DocSource } from "../docviewer";

/** The run currently on screen — survives view switches on purpose. */
let jobId: string | null = null;

/** Human phrasing for the pipeline's phases. */
const PHASES: Record<string, string> = {
  planning: "breaking the question into sub-questions",
  searching: "searching the web",
  reading: "reading pages",
  writing: "updating the draft report",
  deciding: "deciding whether to keep going",
  finalizing: "polishing the final report",
};

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()
  let refreshSeq = 0;                          // discard superseded refreshes

  /** Render one run as a grid TILE: the question as a readable title,
   *  live phase or source count, and compact controls. An open report
   *  expands the tile across the whole grid row. */
  function runTile(run: {
    id: string; question: string; status: string; phase: string;
    round: number; sources: number; result_path: string; session_id: string;
    session_exists?: boolean; archived?: boolean; report?: string;
    source_records?: { url: string; image?: string }[];
  }): HTMLElement {
    const tile = el("div.org-tile",
      { className: `org-tile${jobId === run.id ? " open" : ""}` },
      el("div.org-title", {
        title: run.question,
        onclick: () => { jobId = jobId === run.id ? null : run.id; refresh(); },
        style: "cursor: pointer",
      }, run.question),
      el("span.badge", { className: `badge ${run.status === "running" ? "running" : run.status}` },
        run.status),
      run.status === "running"
        ? el("div.org-meta", {}, `Round ${run.round} — ${PHASES[run.phase] ?? run.phase}…`)
        : el("div.org-meta", { title: run.result_path }, `${run.sources} sources`
            + (run.result_path ? ` · saved to ${run.result_path}` : "")),
      el("div.org-actions", {},
        run.status === "running"
          ? el("button.danger", {
              onclick: async () => { await post(`/api/research/${run.id}/cancel`); refresh(); },
            }, "Stop (keeps the draft)")
          : null,
        // The dedicated document viewer: TOC, stats, export (a report
        // is a document, not a tile expansion — the expansion below
        // stays as the quick in-place peek).
        run.status !== "running"
          ? el("button.primary", {
              onclick: async () => {
                // Live runs are fetched fresh (evolving sources included);
                // archived rows carry everything inline. Old sidecars
                // stored sources as bare strings — normalize either way.
                let report = run.report ?? "";
                let raw: unknown[] = run.source_records ?? [];
                if (!run.archived) {
                  const job = await get<{ report: string; sources: unknown[] }>(
                    `/api/research/${run.id}`);
                  report = job.report;
                  raw = job.sources ?? [];
                }
                if (!report) return;     // nothing to view (empty run)
                const sources: DocSource[] = raw.map((s) =>
                  typeof s === "string" ? { url: s } : (s as DocSource));
                openDocument({
                  title: run.question,
                  markdown: report,
                  meta: `${run.status} · ${run.sources} sources`,
                  sources,
                });
              },
            }, "View report")
          : null,
        el("button", {
          onclick: () => { jobId = jobId === run.id ? null : run.id; refresh(); },
        }, jobId === run.id ? "Hide peek" : "Peek"),
        // Every run is a special kind of chat: jump back to its origin —
        // unless that conversation was deleted, in which case the run's
        // report stays readable here and the control says so (bug 4.3).
        run.session_id
          ? (run.session_exists !== false
              ? el("button", {
                  onclick: () => { presetSession(run.session_id); showView("chat"); },
                }, "Open chat")
              : el("button", { disabled: true, title: "the conversation "
                  + "this run came from was deleted; its report remains "
                  + "here" }, "chat deleted"))
          : null,
      ),
    );
    // The selected run expands with its full (evolving) report. Archived
    // runs (read back from workspace sidecars after a restart) carry the
    // report inline; live runs are fetched fresh for the evolving draft.
    if (jobId === run.id) {
      const report = el("div.report", {}, "loading…");
      // Model output renders as MARKDOWN through md.ts's sanitizing
      // gate — reports are exactly the structured documents 4.5 is for.
      if (run.archived) {
        if (run.report) renderMarkdown(report, run.report);
        else report.textContent = "(no report saved)";
      } else {
        get<{ report: string }>(`/api/research/${run.id}`).then((job) => {
          if (job.report) renderMarkdown(report, job.report);
          else report.textContent = "(no report yet)";
        });
      }
      tile.append(report);
    }
    return tile;
  }

  /** Redraw the whole view: a GRID of runs, "start new" as the first
   *  cell (the organizer pattern — the run itself lives in its chat). */
  async function refresh(): Promise<void> {
    // Events fire refreshes in bursts and the GETs can resolve out of
    // order — only the NEWEST refresh may mount, or a stale "running"
    // snapshot could overwrite the finished grid for good.
    const seq = ++refreshSeq;
    const newTile = el("button.org-tile.new", {
      type: "button",
      onclick: () => { presetMode("research"); showView("chat"); },
    });
    newTile.append(icon("plus"), el("span", {}, "Start new research"));

    // Every run this session, newest first (the organizer's list).
    const runs = await get<{
      id: string; question: string; status: string; phase: string;
      round: number; sources: number; result_path: string; session_id: string;
    }[]>("/api/research");
    if (seq !== refreshSeq) return;            // a newer refresh owns the mount

    mount(container,
      el("h2", {}, "Deep research"),
      el("p.muted", {}, "Multi-round search-read-synthesize runs are "
        + "organized here. They yield to your chat and outrank the "
        + "background seat — start one and keep chatting. Each run is a "
        + "conversation: its progress and report live there."),
      el("div.org-grid", {}, newTile, ...runs.map(runTile)),
    );
  }

  // Any run's phase change or completion redraws the organizer.
  subs.push(on("research", () => refresh()));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
