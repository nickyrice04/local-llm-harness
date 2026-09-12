/** runs.ts — the TRACE tab: every run, and everything it did.
 *
 * This is the feature Nick called the important one: a run is not a
 * status word, it is a full record. Pick one and you see the actual
 * sequence — each model call, each tool call with its arguments, each
 * result with its duration, each repair, the outcome — read straight
 * from the run's append-only event log. Nothing here is recomputed or
 * summarised into vagueness: if the log doesn't say it, this doesn't
 * show it.
 *
 * (It replaces the old Agent tab, whose "agent tasks" concept the mode
 * collapse removed: tool use is something a run DOES, not a kind of
 * conversation.)
 */

import { get } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import { icon } from "../icons";
import type { ViewHandle } from "../main";
import { show as showView } from "../main";
import { presetSession } from "./chat";

/** One row in the index. */
interface RunRow {
  id: string; session_id: string; session_exists: boolean;
  preset: string; status: string; created_at: string;
  tool_calls: number; model_calls: number; events: number;
  stats: Record<string, any>;
}

/** One event in a run's log. */
interface RunEvent {
  seq: number; type: string; at: string; data: Record<string, any>;
}

/** Which run is expanded — module-level so it survives view switches. */
let openRunId: string | null = null;
/** Filters: by tool name and by status ("" = all). */
let filterText = "";
let filterStatus = "";

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];
  let refreshSeq = 0;                     // discard superseded refreshes

  /** Human-readable one-liner for an event, by type. The data keys come
   *  from run_executor's event() calls — this is the reading end of the
   *  same contract. */
  function eventLine(event: RunEvent): HTMLElement {
    const d = event.data;
    const parts: (HTMLElement | string)[] = [];
    let summary = "";
    switch (event.type) {
      case "run_start":
        summary = `${d.preset} · scope ${d.tool_scope} · thinking `
          + `${d.thinking ? "on" : "off"} · budget ${d.max_tool_calls} tools`;
        break;
      case "model_call":
        summary = `round ${d.round} · ${d.messages} messages · `
          + `max ${d.max_tokens} tokens`;
        break;
      case "model_result": {
        const stats = d.stats ?? {};
        summary = `round ${d.round} · ${d.seconds}s · `
          + `${d.visible_chars} visible chars`
          + (d.withheld ? " · withheld (tool call)" : "")
          + (stats.decode_tps ? ` · ${stats.decode_tps} tok/s` : "")
          // The prompt cache, measured: what the server reused vs prefilled.
          + (stats.cache_hit !== undefined && stats.cache_hit !== null
              ? ` · cache ${Math.round(stats.cache_hit * 100)}% (${stats.cached_tokens} reused, ${stats.prompt_tokens} prefilled)` : "");
        break;
      }
      case "tool_call":
        summary = `${d.tool}(${JSON.stringify(d.args ?? {})})`;
        break;
      case "tool_result":
        summary = `${d.tool} · ${d.seconds}s`
          + (d.is_error ? " · ERROR" : "")
          + (d.result_full_chars ? ` · ${d.result_full_chars} chars` : "");
        break;
      case "repair":
        summary = `invalid tool "${d.got}" → offered ${
          (d.available ?? []).join(", ")}`;
        break;
      case "note":
        summary = `${d.what}${d.detail ? ": " + d.detail : ""}`;
        break;
      case "context":
        // The context economy at work (spill / prune / compact): the
        // headline is traceability, so every decision is a line here
        // with its before/after size — never a silent optimizer.
        if (d.what === "spill") {
          summary = `spill · ${d.tool} · ${Number(d.chars).toLocaleString()} chars → ${d.path} `
            + `(${Number(d.inline_chars).toLocaleString()} inline)`;
        } else if (d.what === "prune") {
          summary = `prune · ${d.count} result(s) · ~${d.before_tokens} → ~${d.after_tokens} tokens `
            + `of ${d.budget_tokens}`;
        } else if (d.what === "compact") {
          summary = `compact (${d.method}) · ${d.replaced_messages} messages → summary · `
            + `~${d.before_tokens} → ~${d.after_tokens} tokens · last ${d.kept_recent_pairs} pairs verbatim`;
        } else {
          summary = `${d.what}${d.reason ? ": " + d.reason : ""}${d.error ? ": " + d.error : ""}`;
        }
        break;
      case "error":
        summary = `${d.kind}: ${d.message}`;
        break;
      case "run_end": {
        const c = d.context ?? {};
        summary = `${d.status} · ${d.tool_calls} tools · ${d.rounds} rounds `
          + `· ${d.seconds}s`
          + (c.peak_tokens ? ` · peak ~${c.peak_tokens} tokens` : "")
          + (c.spills ? ` · ${c.spills} spill(s)` : "")
          + (c.prunes ? ` · ${c.prunes} pruned` : "")
          + (c.compactions ? ` · ${c.compactions} compaction(s)` : "")
          + (d.cache_hit_mean !== undefined && d.cache_hit_mean !== null
              ? ` · cache hit mean ${Math.round(d.cache_hit_mean * 100)}% (min ${Math.round(d.cache_hit_min * 100)}%)` : "");
        break;
      }
      default:
        summary = JSON.stringify(d).slice(0, 160);
    }
    parts.push(el("span.ev-seq", {}, String(event.seq)));
    parts.push(el("span.ev-type", { className: `ev-type ev-${event.type}` },
                  event.type));
    parts.push(el("span.ev-summary", {}, summary));

    const line = el("div.ev-line", {}, ...parts);
    // The payload, on demand: arguments and results in full (bounded at
    // write time, with the true length noted when excerpted).
    const body = d.traceback ?? d.result ?? d.message ?? d.summary ?? d.actions ?? d.args;
    if (body !== undefined) {
      const detail = el("pre.ev-detail", { hidden: true },
        typeof body === "string" ? body : JSON.stringify(body, null, 2));
      line.onclick = () => { detail.hidden = !detail.hidden; };
      line.style.cursor = "pointer";
      return el("div", {}, line, detail);
    }
    return line;
  }

  /** Render one run card; expanded cards fetch and show their log. */
  function runCard(run: RunRow): HTMLElement {
    const when = new Date(run.created_at).toLocaleString();
    const card = el("div.card.run-card", {},
      el("div.row", {},
        el("span.badge", { className: `badge ${run.status}` }, run.status),
        el("strong.grow", {}, `${run.preset} · ${when}`),
        el("span.muted", {},
           `${run.model_calls} model · ${run.tool_calls} tools · `
           + `${run.stats?.seconds ?? "?"}s`),
      ),
      el("div.row", {},
        el("button", {
          onclick: () => {
            openRunId = openRunId === run.id ? null : run.id;
            refresh();
          },
        }, openRunId === run.id ? "Hide trace" : "Trace"),
        // Click through to where it happened (the conversation).
        run.session_exists
          ? el("button", {
              onclick: () => { presetSession(run.session_id); showView("chat"); },
            }, "Open chat")
          : el("button", { disabled: true, title: "that conversation was "
              + "deleted; this trace is retained" }, "chat deleted"),
        el("button", {
          onclick: () => { window.location.href = `/api/runs/${run.id}/export`; },
        }, "Export .jsonl"),
      ),
    );

    if (openRunId === run.id) {
      const log = el("div.run-log", {}, "loading…");
      get<{ events: RunEvent[] }>(`/api/runs/${run.id}`).then((full) => {
        const shown = full.events.filter((event) =>
          !filterText
          || JSON.stringify(event.data).toLowerCase()
               .includes(filterText.toLowerCase())
          || event.type.includes(filterText.toLowerCase()));
        log.replaceChildren(...shown.map(eventLine));
      });
      card.append(log);
    }
    return card;
  }

  async function refresh(): Promise<void> {
    const seq = ++refreshSeq;
    const runs = await get<RunRow[]>("/api/runs");
    if (seq !== refreshSeq) return;       // a newer refresh owns the mount

    const search = el("input", {
      placeholder: "filter events (tool name, text…)", value: filterText,
    }) as HTMLInputElement;
    search.oninput = () => { filterText = search.value; refresh(); };
    const statusPills = ["", "done", "failed", "cancelled"].map((s) =>
      el("button", {
        className: `choice mode-pill${filterStatus === s ? " active" : ""}`,
        onclick: () => { filterStatus = s; refresh(); },
      }, s || "all"));

    const visible = runs.filter((r) => !filterStatus || r.status === filterStatus);
    const header = el("div.row", {}, search, ...statusPills);
    header.prepend(icon("agent"));

    mount(container,
      el("h2", {}, "Runs"),
      el("p.muted", {}, "Every run, and everything it did. A run is one "
        + "execution spawned by one message: its policy, every model call, "
        + "every tool call with arguments and results, and how it ended — "
        + "read straight from the run's own event log. Click a line to see "
        + "its payload; export the whole trace as JSONL."),
      header,
      ...(visible.length ? visible.map(runCard)
          : [el("p.muted", {}, "No runs yet — send a message in Chat.")]),
    );
  }

  // A finishing run should appear without a manual reload.
  subs.push(on("scheduler", (e) => {
    if (e.type === "released") refresh();
  }));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
