/** toolcards.ts — every tool call as a card in the chat.
 *
 * The code card (codecard.ts, 2026-09-03) proved the shape: what a run
 * does belongs IN the thread, with its verdict, not only in the trace.
 * Until 2026-09-11 every other tool was a one-line "running grep…"
 * status that vanished on the next token. Now each call is a card:
 *
 *   terminal   run_command / jobs / git: the command, the exit code as a
 *              badge, the output scrollable (dsh's terminal card)
 *   diff       edit_lines / replace_in_file: the file, +N −M, the unified
 *              diff the SERVER computed (before/after on disk), click to
 *              open in the Code pane
 *   search     grep / glob / list_files / web_search: the query and the
 *              matches, collapsible
 *   result     everything else (read_file, fetch_page, use_skill, MCP…):
 *              one line, the result's head behind a toggle
 *
 * Plus the todo panel fed by todo_write — the plan, outside the context
 * window, visible at a glance.
 *
 * Model output is rendered with textContent only (never innerHTML).
 */

import { el } from "./dom";

export interface Diff { lines: string[]; added: number; removed: number; truncated: boolean }
export interface ToolResult {
  name: string; path: string | null; ok: boolean; tag: string | null;
  check: { kind: string; verdict: string; report: string } | null;
  head: string; chars?: number; spill?: string | null; diff?: Diff | null;
}
export interface Todo { id?: string; content: string; status: "pending" | "in_progress" | "done" | string }

const TERMINAL = new Set(["run_command", "run_in_background", "job_output", "job_kill", "shell",
                          "git_overview", "git_file_diff", "git_hunk"]);
const DIFF = new Set(["edit_lines", "replace_in_file"]);
const SEARCH = new Set(["grep", "glob", "list_files", "web_search"]);
/** Cards that start collapsed: their result is context for the model, noise for the person. */
const QUIET = new Set(["read_file", "read_structure", "use_skill", "fetch_page", "remember_fact", "check_page", "read_image"]);

export function kindOf(name: string): "terminal" | "diff" | "search" | "result" {
  if (TERMINAL.has(name)) return "terminal";
  if (DIFF.has(name)) return "diff";
  if (SEARCH.has(name)) return "search";
  return "result";
}

/** The card's title line: what was asked, in the tool's own terms. */
function titleOf(name: string, args: Record<string, any>, summary: string): string {
  const a = args ?? {};
  if (name === "run_command" || name === "run_in_background") return String(a.command ?? summary);
  if (name === "grep") return `grep ${JSON.stringify(String(a.pattern ?? ""))}${a.path ? " in " + a.path : ""}${a.glob ? " " + a.glob : ""}`;
  if (name === "glob") return `glob ${a.pattern ?? ""}${a.path ? " in " + a.path : ""}`;
  if (name === "list_files") return `list ${a.path || "."}${a.pattern ? " " + a.pattern : ""}`;
  if (name === "web_search") return `search: ${a.query ?? ""}`;
  if (name === "fetch_page") return String(a.url ?? summary);
  if (name === "use_skill") return `skill: ${a.name ?? ""}`;
  if (a.path) return `${name} ${a.path}`;
  return summary || name;
}

/** A fresh, running card. */
export function toolCard(name: string, args: Record<string, any>, summary: string): HTMLElement {
  const kind = kindOf(name);
  const state = el("span.badge.running", {}, "running");
  const title = el("span.card-title", { title: titleOf(name, args, summary) }, titleOf(name, args, summary));
  const meta = el("span.muted.card-meta", {}, "");
  const chevron = el("span.muted", {}, "▾");
  const body = el("div.card-body", { hidden: true });
  const head = el("div.card-head", {}, el("span.card-kind", {}, kind === "result" ? name : kind), title, state, meta,
                  el("span.grow"), chevron);
  const card = el("div.tool-card", { className: `tool-card ${kind} ${name}` }, head, body);
  head.onclick = () => { body.hidden = !body.hidden; chevron.textContent = body.hidden ? "▸" : "▾"; };
  (card as any).__parts = { name, args, kind, state, title, meta, body, chevron, head };
  return card;
}

/** Parse run_command's leading facts out of the result's head. */
function exitOf(head: string): { label: string; ok: boolean } | null {
  const m = /^\[exit code (\d+)(?: \(([\d.]+s)\))?\]/.exec(head);
  if (m) return { label: `exit ${m[1]}${m[2] ? " · " + m[2] : ""}`, ok: m[1] === "0" };
  if (/^\[TIMED OUT/.test(head)) return { label: "timed out", ok: false };
  if (/^\[killed/.test(head)) return { label: "killed", ok: false };
  if (/^\[job /.test(head)) return { label: head.slice(1, head.indexOf("]")).slice(0, 40), ok: true };
  return null;
}

/** The tool ran: settle the card into its final state. */
export function toolCardSettle(card: HTMLElement, r: ToolResult, openInCode: (path: string) => void): void {
  const p = (card as any).__parts;
  const kind: string = p.kind;
  const body: HTMLElement = p.body;
  const state: HTMLElement = p.state;
  const head = r.head ?? "";
  body.replaceChildren();
  // The verdict badge: the tool's own facts first (an exit code IS the verdict of a command).
  const exit = kind === "terminal" ? exitOf(head) : null;
  if (!r.ok) { state.className = "badge failed"; state.textContent = "error"; }
  else if (exit) { state.className = `badge ${exit.ok ? "exit-ok" : "exit-bad"}`; state.textContent = exit.label; }
  else if (r.check && r.check.verdict === "FIX NEEDED") { state.className = "badge failed"; state.textContent = `${r.check.kind}: FIX NEEDED`; state.title = r.check.report; }
  else if (r.check && r.check.verdict === "PASS") { state.className = "badge done"; state.textContent = `${r.check.kind}: PASS`; state.title = r.check.report; }
  else { state.className = "badge done"; state.textContent = "done"; }
  const size = r.chars ? `${r.chars.toLocaleString()} chars` : "";
  p.meta.textContent = [size, r.spill ? "spilled to " + r.spill : ""].filter(Boolean).join(" · ");

  if (kind === "diff" && r.diff) {
    // +N −M in the head; the unified diff in the body, coloured per line.
    const stat = el("span.diff-stat", {}, el("span.add", {}, `+${r.diff.added}`), " ", el("span.del", {}, `−${r.diff.removed}`));
    p.meta.replaceChildren(stat, r.tag ? el("span.muted", {}, ` · #${r.tag}`) : "");
    body.append(el("pre", {}, ...r.diff.lines.slice(2).map((line) =>
      el("div", { className: `diff-line ${line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "ctx"}` }, line))));
    if (r.diff.truncated) body.append(el("div.card-more", {}, "diff shortened — open the file for the rest"));
    if (r.path) {
      const path = r.path;
      p.head.append(el("button", { onclick: (ev: Event) => { ev.stopPropagation(); openInCode(path); } }, "open"));
    }
  } else if (kind === "diff") {
    // No change on disk (a refused edit, a no-op): the result says why.
    body.append(el("pre", {}, head));
  } else if (kind === "terminal") {
    // The output as a terminal shows it, the leading fact line stripped (it is the badge).
    const output = head.replace(/^\[[^\]]*\]\s*\n?/, "");
    body.append(el("pre", {}, output || "(no output)"));
    if (head.length >= 2000 || r.spill) body.append(el("div.card-more", {}, r.spill ? `full output in ${r.spill}` : "output shortened here — the trace has it all"));
  } else {
    body.append(el("pre", {}, head || "(empty result)"));
    if ((r.chars ?? 0) > head.length) body.append(el("div.card-more", {}, `showing the first ${head.length.toLocaleString()} of ${(r.chars ?? 0).toLocaleString()} chars — the trace has it all`));
  }
  // Open state: errors and terminals and diffs and searches show; quiet tools stay folded.
  const open = !r.ok || !(QUIET.has(p.name));
  body.hidden = !open;
  p.chevron.textContent = open ? "▾" : "▸";
  // A red check report (a page that fails) rides above the body, as the code card does.
  if (r.check && r.check.verdict === "FIX NEEDED") body.prepend(el("pre.card-report", {}, r.check.report));
}

// ---- The todo / plan panel -------------------------------------------------

export function todoPanel(): HTMLElement {
  const list = el("ul.todo-list");
  const meta = el("span.muted.card-meta", {}, "");
  const panel = el("div.todo-panel", {},
    el("div.card-head", {}, el("span.card-kind", {}, "plan"), el("span.grow"), meta), list);
  (panel as any).__parts = { list, meta };
  return panel;
}

const MARKS: Record<string, string> = { pending: "[ ]", in_progress: "[>]", done: "[x]" };

export function todoUpdate(panel: HTMLElement, todos: Todo[]): void {
  const p = (panel as any).__parts;
  p.list.replaceChildren(...todos.map((t) =>
    el("li", { className: `todo-item ${t.status}` }, el("span.todo-mark", {}, MARKS[t.status] ?? "[ ]"), el("span", {}, t.content))));
  const done = todos.filter((t) => t.status === "done").length;
  p.meta.textContent = `${done}/${todos.length} done`;
}
