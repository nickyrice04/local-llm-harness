/** codecard.ts — the code, in the chat, with its verdict.
 *
 * Nick, 2026-09-03: "I was expecting the code previewer to be embedded
 * within the chat. I would want to view the code. I would also want
 * Seymour to be aware of what its code looks like and if it runs."
 *
 * One card per file a run writes. While the call streams, the card shows
 * the file growing (content deltas from tool_progress frames). When the
 * tool has run, it settles: the final file (fetched back from the
 * workspace, so append/edit rounds show the whole thing), a Code |
 * Preview switch (the preview is the same CSP-sandboxed page the viewer
 * opens), and the verdict of the check the harness ran — "checked in
 * your browser: 0 errors · animation running · PASS" or the red reason.
 */

import hljs from "highlight.js/lib/core";

import { openFile as openInCode } from "./code";
import { openDocument } from "./docviewer";
import { el } from "./dom";

interface Check { kind: string; verdict: string; report: string }
interface ToolResult { name: string; path: string | null; ok: boolean; tag: string | null; check: Check | null; head: string }

const FILE_TOOLS = new Set(["write_file", "append_file", "edit_lines", "replace_in_file"]);

function langOf(path: string | null): string | undefined {
  const ext = (path ?? "").toLowerCase().split(".").pop() ?? "";
  return ({ html: "xml", htm: "xml", js: "javascript", mjs: "javascript", ts: "typescript", py: "python",
            css: "css", json: "json", md: "markdown", sh: "bash" } as Record<string, string>)[ext];
}

function highlightInto(code: HTMLElement, text: string, path: string | null): void {
  const lang = langOf(path);
  code.textContent = text;                        // textContent first: never markup from the model
  if (!lang || text.length > 120_000) return;      // very large: plain text is honest and fast
  try {
    const out = hljs.highlight(text, { language: lang, ignoreIllegals: true });
    code.innerHTML = out.value;                    // hljs escapes; its output is our own markup
  } catch { /* plain text stays */ }
}

/** A fresh card for `path` (may be unknown while the call is still early). */
export function codeCard(path: string | null, tool: string): HTMLElement {
  const title = el("span.card-title", {}, path ?? "…");
  const state = el("span.badge.running", {}, verbFor(tool));
  const meta = el("span.muted.card-meta", {}, "");
  const code = el("code", {}, "");
  const pre = el("pre.card-code", {}, code);
  const body = el("div.card-body", {}, pre);
  const tabs = el("div.card-tabs");
  const actions = el("div.card-actions");
  const card = el("div.code-card", {},
    el("div.card-head", {}, title, state, meta, el("span.grow"), tabs, actions),
    body);
  (card as any).__parts = { title, state, meta, code, pre, body, tabs, actions, text: "", path, tool };
  return card;
}

function verbFor(tool: string): string {
  return tool === "write_file" ? "writing" : tool === "append_file" ? "appending"
       : tool === "edit_lines" || tool === "replace_in_file" ? "editing" : "working";
}

let highlightTimer: number | null = null;

/** A tool_progress frame: append the delta, keep the tail in view. */
export function cardProgress(card: HTMLElement, p: { path?: string | null; name?: string; chars: number; seconds: number;
                                                      delta?: string; reset?: boolean; tail?: string }): void {
  const parts = (card as any).__parts;
  if (p.path && !parts.path) { parts.path = p.path; parts.title.textContent = p.path; }
  if (p.reset) parts.text = p.delta ?? "";
  else parts.text += p.delta ?? "";
  parts.meta.textContent = `${p.chars.toLocaleString()} chars · ${p.seconds}s`;
  // Repaint the highlight ~4×/s at most; plain text between paints.
  if (highlightTimer === null) {
    highlightTimer = window.setTimeout(() => {
      highlightTimer = null;
      highlightInto(parts.code, parts.text, parts.path);
      parts.pre.scrollTop = 1e9;
    }, 250);
  }
}

/** The tool ran: fetch the file as it now is, show verdict, tabs, actions. */
export async function cardSettle(card: HTMLElement, r: ToolResult): Promise<void> {
  const parts = (card as any).__parts;
  if (r.path) { parts.path = r.path; parts.title.textContent = r.path; }
  parts.tool = r.name;
  // The file on disk is the truth (an append shows the whole file, not the part).
  let text = parts.text;
  let lines = 0;
  if (r.path) {
    try {
      const res = await fetch(`/api/workspace/read?path=${encodeURIComponent(r.path)}`);
      if (res.ok) { const body = await res.json(); text = body.content; lines = body.lines; }
    } catch { /* keep what streamed */ }
  }
  parts.text = text;
  highlightInto(parts.code, text, parts.path);
  parts.meta.textContent = `${lines ? lines + " lines · " : ""}${text.length.toLocaleString()} chars${r.tag ? " · #" + r.tag : ""}`;
  // The verdict, from the check the HARNESS ran (not the model's claim).
  const check = r.check;
  if (!r.ok) { parts.state.className = "badge failed"; parts.state.textContent = "failed"; parts.state.title = r.head; }
  else if (!check) { parts.state.className = "badge done"; parts.state.textContent = "written"; }
  else if (check.verdict === "PASS") { parts.state.className = "badge done"; parts.state.textContent = `${check.kind}: PASS`; parts.state.title = check.report; }
  else if (check.verdict === "FIX NEEDED") { parts.state.className = "badge failed"; parts.state.textContent = `${check.kind}: FIX NEEDED`; parts.state.title = check.report; }
  else { parts.state.className = "badge"; parts.state.textContent = `${check.kind}: not measured`; parts.state.title = check.report; }
  // The check's own words, under the head, when there is something to say.
  card.querySelector(".card-report")?.remove();
  if (check && check.verdict !== "PASS") {
    card.insertBefore(el("pre.card-report", {}, check.report), parts.body);
  } else if (check && check.verdict === "PASS") {
    const facts = check.report.split("\n").find((l) => l.startsWith("facts:")) ?? "";
    if (facts) card.insertBefore(el("div.card-report.muted", {}, "checked in your browser — " + facts.replace(/^facts:\s*/, "")), parts.body);
  }
  // Tabs + actions.
  const isHtml = /\.html?$/i.test(parts.path ?? "");
  const showCode = () => { parts.body.replaceChildren(parts.pre); setActive("code"); };
  const showPreview = () => {
    const frame = el("iframe.card-preview") as HTMLIFrameElement;   // sandboxed by the server's CSP
    frame.src = `/api/workspace/file?path=${encodeURIComponent(parts.path)}&v=${Date.now()}`;
    parts.body.replaceChildren(frame); setActive("preview");
  };
  const setActive = (which: string) => parts.tabs.querySelectorAll("button").forEach((b: HTMLButtonElement) =>
    b.classList.toggle("active", b.dataset.tab === which));
  const codeTab = el("button.choice.active", { onclick: showCode }, "code");
  codeTab.dataset.tab = "code";
  parts.tabs.replaceChildren(codeTab);
  if (isHtml) {
    const previewTab = el("button.choice", { onclick: showPreview }, "preview");
    previewTab.dataset.tab = "preview";
    parts.tabs.append(previewTab);
  }
  parts.actions.replaceChildren(
    isHtml ? el("button", { onclick: () => openDocument({ title: parts.path, fileUrl: `/api/workspace/file?path=${encodeURIComponent(parts.path)}`, meta: "runs sandboxed" }) }, "open") : null,
    el("button", { onclick: () => void openInCode(parts.path) }, "edit"),
    isHtml ? el("button", { onclick: () => void recheck(card) }, "re-check") : null,
  );
  if (isHtml && check && check.verdict === "PASS") showPreview();   // a working page shows itself
}

async function recheck(card: HTMLElement): Promise<void> {
  const parts = (card as any).__parts;
  parts.state.className = "badge running"; parts.state.textContent = "checking…";
  try {
    const res = await fetch("/api/workspace/run-tool", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: "check_page", args: { path: parts.path, seconds: 3 } }) });
    const body = await res.json();
    const report: string = body.result ?? "";
    const verdict = /verdict:\s*PASS/.test(report) ? "PASS" : /verdict:\s*FIX NEEDED/.test(report) ? "FIX NEEDED" : "not measured";
    await cardSettle(card, { name: parts.tool, path: parts.path, ok: true, tag: null, head: "",
                             check: { kind: "check_page", verdict, report } });
  } catch { parts.state.className = "badge failed"; parts.state.textContent = "check failed"; }
}

export { FILE_TOOLS };
