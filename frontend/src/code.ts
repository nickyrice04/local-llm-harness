/** code.ts — the Code workspace: see the files, see the work, edit, run.
 *
 * Why a PANE and not a view: switching views destroys the chat view and
 * aborts its stream, and the server treats that as Stop. A person who
 * opens the editor to watch a page being written must not cancel the
 * writing. So this mounts once into #code-pane beside #view, toggled by
 * the nav's "Code" button, and follows runs over the event bus (topic
 * "run": tool_progress / tool / tool_result / end), which the executor
 * publishes for exactly this reason.
 *
 * Four surfaces, one workspace: a file tree (GET /api/workspace/tree), a
 * CodeMirror editor with explicit Save that refuses to clobber a file
 * the model changed meanwhile (the content tag; 409 → merge, never
 * silent overwrite), a side panel with Preview (the same sandboxed
 * iframe the viewer uses), Output (run_command, the model's own sandbox),
 * Live (the tool call being written, in full) and Diff (what each edit
 * changed), all in the pixel theme.
 */

import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { bracketMatching, indentOnInput } from "@codemirror/language";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, drawSelection, highlightActiveLine, highlightActiveLineGutter,
         keymap, lineNumbers } from "@codemirror/view";

import { get, post } from "./api";
import { on } from "./bus";
import { openDocument } from "./docviewer";
import { el, mount } from "./dom";
import { icon } from "./icons";
import { seymourHighlight, seymourTheme } from "./code-theme";

interface TreeFile { path: string; size: number; kind: string }
interface OpenFile { path: string; name: string; tag: string | null; saved: string; state: EditorState; dirty: boolean }

/** The pane's root (mounted once) and whether it is showing. */
let root: HTMLElement | null = null;
let visible = false;
const open: OpenFile[] = [];
let active: string | null = null;
let files: TreeFile[] = [];
let expanded = new Set<string>();       // folders open in the tree
let side: "preview" | "output" | "live" | "diff" = "preview";
let previewPath: string | null = null;
let outputLog: { command: string; result: string }[] = [];
let live: { name: string; path: string | null; chars: number; seconds: number; text: string; status: string } | null = null;
let diffs: { tool: string; path: string; before: string; after: string; tag: string | null }[] = [];
const beforeByPath = new Map<string, string>();   // last content seen, for diffs

let view: EditorView | null = null;

// ---- Layout: resizable, persisted columns --------------------------------------
// The 2026-09-11 layout bug (the editor crushed to ~100 px in a wide
// window) is fixed in CSS by real floors and container queries; what
// lives here is the person's own say: the chat, tree and side-panel
// widths they dragged, and whether they hid the tree or the panel —
// persisted in localStorage like the avatar panel's width is.
interface Layout { chat: number | null; tree: number; side: number | null; treeHidden: boolean; sideHidden: boolean }
const LAYOUT_KEY = "seymour.code.layout";
const layout: Layout = { chat: null, tree: 180, side: null, treeHidden: false, sideHidden: false };
try {
  const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}");
  Object.assign(layout, saved);
} catch { /* first run, or private mode */ }
function saveLayout(): void {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* private mode */ }
}
/** Push the layout into CSS variables and classes (the CSS does the rest). */
function applyLayout(): void {
  if (!root) return;
  root.style.setProperty("--code-tree-w", `${Math.min(400, Math.max(120, layout.tree))}px`);
  if (layout.side) root.style.setProperty("--code-side-w", `${Math.max(260, layout.side)}px`);
  else root.style.removeProperty("--code-side-w");
  const main = document.getElementById("main");
  if (main) {
    if (layout.chat) main.style.setProperty("--chat-w", `${Math.max(320, layout.chat)}px`);
    else main.style.removeProperty("--chat-w");
  }
  const codeRoot = root.querySelector(".code-root");
  codeRoot?.classList.toggle("no-tree", layout.treeHidden);
  codeRoot?.classList.toggle("no-side", layout.sideHidden);
  const showTree = root.querySelector(".code-tree-show") as HTMLElement | null;
  if (showTree) showTree.hidden = !layout.treeHidden;
  const showSide = root.querySelector(".code-side-show") as HTMLElement | null;
  if (showSide) showSide.hidden = !layout.sideHidden;
}
/** Kept for the tree buttons' old name. */
function applyTree(): void { applyLayout(); }

/** A drag strip between two columns. `measure` turns the pointer's x into
 *  the new width of the column it controls; `commit` stores it. */
function resizer(kind: "chat" | "tree" | "side", measure: (x: number) => number, commit: (w: number) => void): HTMLElement {
  const strip = el("div", { className: `code-resize ${kind}`, title: "drag to resize" });
  strip.addEventListener("pointerdown", (down: PointerEvent) => {
    down.preventDefault();
    strip.classList.add("dragging");
    strip.setPointerCapture(down.pointerId);
    const move = (ev: PointerEvent) => { commit(measure(ev.clientX)); applyLayout(); };
    const up = () => {
      strip.classList.remove("dragging");
      strip.removeEventListener("pointermove", move);
      strip.removeEventListener("pointerup", up);
      saveLayout();
      fitCompanion();
    };
    strip.addEventListener("pointermove", move);
    strip.addEventListener("pointerup", up);
  });
  return strip;
}

/** The chat column and the editor both have floors (320 + 480 px, plus
 *  the strips). When the shell cannot hold them AND the avatar column,
 *  the avatar column steps aside — rather than the editor being crushed. */
const NEEDED_PX = 320 + 480 + 24;
function fitCompanion(): void {
  const body = document.body;
  if (!visible) { body.classList.remove("companion-collapsed"); return; }
  if (body.classList.contains("avatar-off")) return;         // nothing to collapse
  const sidebar = document.getElementById("sidebar");
  const companionW = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--companion-w")) || 260;
  const shell = body.clientWidth - (sidebar?.offsetWidth ?? 230);
  body.classList.toggle("companion-collapsed", shell - companionW < NEEDED_PX);
}
window.addEventListener("resize", () => { if (visible) fitCompanion(); });
const langConf = new Compartment();
const readOnlyConf = new Compartment();

function langFor(path: string): Extension {
  const ext = path.toLowerCase().split(".").pop() ?? "";
  if (ext === "html" || ext === "htm") return html();
  if (ext === "css") return css();
  if (ext === "js" || ext === "mjs" || ext === "ts") return javascript({ typescript: ext === "ts" });
  if (ext === "json") return json();
  if (ext === "py") return python();
  if (ext === "md") return markdown();
  return [];
}

function baseExtensions(): Extension[] {
  return [
    lineNumbers(), highlightActiveLineGutter(), history(), drawSelection(),
    indentOnInput(), bracketMatching(), highlightActiveLine(), highlightSelectionMatches(),
    keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab,
               { key: "Mod-s", run: () => { void save(); return true; } }]),
    seymourTheme, seymourHighlight,
    EditorView.updateListener.of((update) => {
      if (!update.docChanged) return;
      const file = open.find((f) => f.path === active);
      if (!file) return;
      file.state = update.state;
      const wasDirty = file.dirty;
      file.dirty = update.state.doc.toString() !== file.saved;
      if (wasDirty !== file.dirty) renderTabs();
    }),
  ];
}

// ---- data ------------------------------------------------------------------

async function loadTree(): Promise<void> {
  try {
    const body = await get<{ files: TreeFile[] }>("/api/workspace/tree");
    files = body.files;
  } catch { files = []; }
  renderTree();
}

export async function openFile(path: string): Promise<void> {
  let file = open.find((f) => f.path === path);
  if (!file) {
    try {
      const body = await get<{ content: string; tag: string }>(`/api/workspace/read?path=${encodeURIComponent(path)}`);
      file = { path, name: path.split("/").pop() ?? path, tag: body.tag, saved: body.content, dirty: false,
               state: EditorState.create({ doc: body.content, extensions: [...baseExtensions(), langConf.of(langFor(path)), readOnlyConf.of([])] }) };
      beforeByPath.set(path, body.content);
      open.push(file);
    } catch (error: any) {
      note(error?.message ?? `could not open ${path}`);
      return;
    }
  }
  active = path;
  if (/\.html?$/i.test(path)) { previewPath = path; }
  renderTabs(); renderEditor(); renderSide(); renderTree();
  show(true);
}

async function save(): Promise<void> {
  const file = open.find((f) => f.path === active);
  if (!file || !view) return;
  const content = view.state.doc.toString();
  try {
    const body = await post<{ tag: string }>("/api/workspace/write",
      { path: file.path, content, expected_tag: file.tag });
    file.saved = content; file.tag = body.tag; file.dirty = false;
    beforeByPath.set(file.path, content);
    note(`saved ${file.path} #${body.tag}`);
    if (previewPath === file.path) renderSide();
  } catch (error: any) {
    // 409: the model changed the file since it was opened. Show, don't clobber.
    const detail = error?.detail ?? error?.body?.detail;
    if (detail && typeof detail === "object" && "current" in detail) {
      note(`${file.path} changed on disk (#${detail.current_tag}) — reload it or save again to overwrite`);
      file.tag = null;                 // a second Save overwrites knowingly
    } else {
      note(error?.message ?? "save failed");
    }
  }
  renderTabs();
}

async function reloadFromDisk(path: string, tag: string | null): Promise<void> {
  const file = open.find((f) => f.path === path);
  if (!file) return;
  try {
    const body = await get<{ content: string; tag: string }>(`/api/workspace/read?path=${encodeURIComponent(path)}`);
    const before = file.saved;
    if (before !== body.content) diffs.unshift({ tool: "seymour", path, before, after: body.content, tag: body.tag });
    if (!file.dirty) {
      file.saved = body.content; file.tag = body.tag;
      file.state = EditorState.create({ doc: body.content, extensions: [...baseExtensions(), langConf.of(langFor(path)), readOnlyConf.of([])] });
      if (active === path && view) view.setState(file.state);
      note(`${path} updated by Seymour #${body.tag}`);
    } else {
      file.tag = body.tag;
      note(`Seymour changed ${path} (#${body.tag}) while you have unsaved edits — see Diff`);
    }
  } catch { /* deleted or unreadable: the tree refresh shows it */ }
  beforeByPath.set(path, (open.find((f) => f.path === path)?.saved) ?? "");
  void tag;
  renderTabs(); renderSide();
}

let noteEl: HTMLElement | null = null;
function note(text: string): void {
  if (noteEl) { noteEl.textContent = text; noteEl.hidden = !text; }
}

// ---- rendering -------------------------------------------------------------

let treeEl: HTMLElement, tabsEl: HTMLElement, editorEl: HTMLElement, sideEl: HTMLElement, sideTabsEl: HTMLElement;

function renderTree(): void {
  if (!treeEl) return;
  const dirs = new Set<string>();
  for (const f of files) {
    const parts = f.path.split("/");
    for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
  }
  const rows: HTMLElement[] = [];
  const shown = (path: string): boolean => {
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    return !parent || (expanded.has(parent) && shown(parent));
  };
  const entries = [...[...dirs].map((d) => ({ path: d, dir: true })), ...files.map((f) => ({ path: f.path, dir: false }))]
    .sort((a, b) => a.path.localeCompare(b.path));
  for (const e of entries) {
    if (!shown(e.path)) continue;
    const depth = e.path.split("/").length - 1;
    const name = e.path.split("/").pop() ?? e.path;
    rows.push(el("div", {
      className: `code-node ${e.dir ? "dir" : "file"}${e.path === active ? " open" : ""}${e.dir && expanded.has(e.path) ? " expanded" : ""}`,
      style: `--depth:${depth}`, title: e.path,
      onclick: () => {
        if (e.dir) { expanded.has(e.path) ? expanded.delete(e.path) : expanded.add(e.path); renderTree(); }
        else void openFile(e.path);
      },
    }, e.dir ? (expanded.has(e.path) ? "▾ " : "▸ ") : "", name));
  }
  mount(treeEl, el("div.code-tree-head", {}, el("h3", {}, "workspace"),
                   el("button", { onclick: () => void loadTree(), title: "refresh" }, "↻"),
                   el("button", { onclick: () => { layout.treeHidden = true; saveLayout(); applyTree(); }, title: "hide the tree" }, "«")),
        el("div.code-tree-list", {}, ...(rows.length ? rows : [el("p.muted", {}, "empty workspace")])));
}

function renderTabs(): void {
  if (!tabsEl) return;
  const current = open.find((f) => f.path === active);
  mount(tabsEl,
    el("button.code-tree-show", { hidden: !layout.treeHidden, title: "show the workspace tree",
                                  onclick: () => { layout.treeHidden = false; saveLayout(); applyTree(); } }, "»"),
    ...open.map((f) => el("button", {
      className: `code-tab${f.path === active ? " active" : ""}${f.dirty ? " dirty" : ""}`,
      title: f.path,
      onclick: () => { active = f.path; renderTabs(); renderEditor(); renderTree(); },
    }, f.name + (f.dirty ? " •" : ""),
       el("span.code-tab-x", { onclick: (ev: Event) => { ev.stopPropagation(); closeFile(f.path); } }, "×"))),
    el("span.grow"),
    current ? el("span.badge", { title: "the content tag the model sees in [path#TAG]" }, `#${current.tag ?? "----"}`) : null,
    current ? el("span.badge", { className: `badge ${current.dirty ? "running" : "done"}` }, current.dirty ? "modified" : "saved") : null,
    current ? el("button.primary", { onclick: () => void save(), title: "Cmd/Ctrl+S" }, "Save") : null,
    current && /\.html?$/i.test(current.path)
      ? el("button", { onclick: () => { previewPath = current.path; side = "preview"; renderSide(); } }, "Preview") : null,
    el("button.code-side-show", { hidden: !layout.sideHidden, title: "show the side panel (preview, output, live, diff)",
                                  onclick: () => { layout.sideHidden = false; saveLayout(); applyLayout(); } }, "panel ‹"),
  );
}

function closeFile(path: string): void {
  const idx = open.findIndex((f) => f.path === path);
  const file = open[idx];
  if (idx === -1 || !file) return;
  if (file.dirty && !confirm(`${path} has unsaved changes. Close anyway?`)) return;
  open.splice(idx, 1);
  if (active === path) active = open[Math.max(0, idx - 1)]?.path ?? null;
  renderTabs(); renderEditor(); renderTree();
}

function renderEditor(): void {
  if (!editorEl) return;
  const file = open.find((f) => f.path === active);
  if (!file) {
    view?.destroy(); view = null;
    mount(editorEl, el("div.code-empty", {},
      el("p.muted", {}, "Open a file from the workspace, or watch Live while Seymour writes one."),
    ));
    return;
  }
  if (!view) {
    editorEl.replaceChildren();
    view = new EditorView({ state: file.state, parent: editorEl });
  } else if (view.state !== file.state) {
    view.setState(file.state);
  }
}

function renderSide(): void {
  if (!sideEl || !sideTabsEl) return;
  mount(sideTabsEl, ...(["preview", "output", "live", "diff"] as const).map((name) =>
    el("button", { className: `choice${side === name ? " active" : ""}`,
                   onclick: () => { side = name; renderSide(); } },
       name === "live" && live && live.status === "writing" ? "live ●" : name)),
    el("span.grow"),
    el("button", { onclick: () => { layout.sideHidden = true; saveLayout(); applyLayout(); }, title: "hide the side panel" }, "»"));
  if (side === "preview") {
    if (!previewPath) { mount(sideEl, el("p.muted", {}, "Open an .html file to preview it here (sandboxed, no network).")); return; }
    // No sandbox ATTRIBUTE: the server serves every workspace page under a
    // CSP `sandbox allow-scripts …; default-src 'none'`, which is the same
    // confinement — and measured in the desktop app's browser pane, a
    // frame carrying the attribute as well is blocked outright.
    const frame = el("iframe.code-preview") as HTMLIFrameElement;
    frame.src = `/api/workspace/file?path=${encodeURIComponent(previewPath)}&v=${Date.now()}`;
    const p = previewPath;
    mount(sideEl,
      el("div.row", {}, el("span.grow.muted", {}, p),
         el("button", { onclick: () => renderSide() }, "reload"),
         el("button", { onclick: () => openDocument({ title: p, fileUrl: `/api/workspace/file?path=${encodeURIComponent(p)}`, meta: "runs sandboxed" }) }, "open full"),
         el("button", { onclick: () => void checkPage(p), title: "run check_page on this file" }, "check")),
      frame);
  } else if (side === "output") {
    const input = el("input", { placeholder: "a command, run in the model's sandbox (e.g. python -m pytest -q)", autocomplete: "off" }) as HTMLInputElement;
    input.style.flex = "1";
    const run = async () => {
      const command = input.value.trim(); if (!command) return;
      input.value = "";
      outputLog.unshift({ command, result: "running…" }); renderSide();
      try {
        const body = await post<{ result: string }>("/api/workspace/run", { command });
        outputLog[0] = { command, result: body.result };
      } catch (error: any) { outputLog[0] = { command, result: error?.message ?? "failed" }; }
      renderSide();
    };
    const form = el("form.row", { onsubmit: (ev: Event) => { ev.preventDefault(); void run(); } }, input, el("button.primary", {}, "Run"));
    mount(sideEl, form, ...outputLog.slice(0, 6).map((o) =>
      el("div.code-output", {}, el("div.muted", {}, `$ ${o.command}`), el("pre", {}, o.result))));
  } else if (side === "live") {
    if (!live) { mount(sideEl, el("p.muted", {}, "Nothing is being written right now. When a run writes or edits a file, the call appears here as it streams.")); return; }
    const verb = live.name === "write_file" ? "writing" : live.name === "append_file" ? "appending to"
               : live.name === "run_command" ? "preparing a command" : live.name ? `calling ${live.name}` : "composing";
    mount(sideEl,
      el("div.row", {}, el("span.badge", { className: `badge ${live.status === "writing" ? "running" : live.status === "cancelled" ? "cancelled" : "done"}` }, live.status),
         el("span.grow", {}, `${verb}${live.path ? " " + live.path : ""}`),
         el("span.muted", {}, `${live.chars.toLocaleString()} chars · ${live.seconds}s`),
         live.path && live.status !== "writing" ? el("button", { onclick: () => void openFile(live!.path!) }, "open in editor") : null),
      el("pre.live-code.tall", {}, el("code", {}, live.text)));
    const pre = sideEl.querySelector("pre"); if (pre) pre.scrollTop = 1e9;
  } else {
    if (!diffs.length) { mount(sideEl, el("p.muted", {}, "No edits yet in this session. Each file Seymour changes shows up here as a diff.")); return; }
    mount(sideEl, ...diffs.slice(0, 8).map((d) => {
      const lines = unifiedDiff(d.before, d.after);
      return el("div.code-diff", {},
        el("div.row", {}, el("strong", {}, d.path), el("span.muted", {}, d.tool + (d.tag ? ` #${d.tag}` : "")),
           el("span.grow"), el("button", { onclick: () => void openFile(d.path) }, "open")),
        el("pre.diff", {}, ...lines.map((l) => el("div", { className: `diff-line ${l[0] === "+" ? "add" : l[0] === "-" ? "del" : l[0] === "@" ? "hunk" : "ctx"}` }, l))));
    }));
  }
}

/** A small line diff (LCS), enough for the panel; big files show the head. */
function unifiedDiff(a: string, b: string): string[] {
  const A = a.split("\n"), B = b.split("\n");
  if (A.length * B.length > 4_000_000) return ["@@ files too large to diff here @@", ...B.slice(0, 40).map((l) => "+" + l)];
  // A flat LCS table (typed Uint32Array: no undefined cells for the checker).
  const W = B.length + 1;
  const dp = new Uint32Array((A.length + 1) * W);
  const at = (i: number, j: number) => dp[i * W + j] ?? 0;
  for (let i = A.length - 1; i >= 0; i--) for (let j = B.length - 1; j >= 0; j--)
    dp[i * W + j] = A[i] === B[j] ? at(i + 1, j + 1) + 1 : Math.max(at(i + 1, j), at(i, j + 1));
  const out: string[] = []; let i = 0, j = 0;
  while (i < A.length && j < B.length) {
    if (A[i] === B[j]) { out.push(" " + A[i]); i++; j++; }
    else if (at(i + 1, j) >= at(i, j + 1)) { out.push("-" + A[i]); i++; }
    else { out.push("+" + B[j]); j++; }
  }
  while (i < A.length) out.push("-" + A[i++]);
  while (j < B.length) out.push("+" + B[j++]);
  // Keep only changed lines with 2 lines of context, hunk markers between gaps.
  const keep = new Set<number>();
  out.forEach((l, k) => { if (l[0] !== " ") for (let c = k - 2; c <= k + 2; c++) keep.add(c); });
  const result: string[] = []; let last = -2;
  out.forEach((l, k) => { if (!keep.has(k)) return; if (k !== last + 1) result.push(`@@ line ${k + 1} @@`); result.push(l); last = k; });
  return result.slice(0, 400);
}

async function checkPage(path: string): Promise<void> {
  outputLog.unshift({ command: `check_page ${path}`, result: "checking in this tab…" }); side = "output"; renderSide();
  try {
    const body = await post<{ result: string }>("/api/workspace/run-tool", { tool: "check_page", args: { path } });
    outputLog[0] = { command: `check_page ${path}`, result: body.result };
  } catch (error: any) { outputLog[0] = { command: `check_page ${path}`, result: error?.message ?? "failed" }; }
  renderSide();
}

// ---- mounting + the run feed --------------------------------------------------

export function isVisible(): boolean { return visible; }

export function show(on_: boolean): void {
  if (!root) return;
  visible = on_;
  root.hidden = !visible;
  document.getElementById("main")?.classList.toggle("with-code", visible);
  document.querySelector<HTMLButtonElement>('.nav-btn[data-view="code"]')?.classList.toggle("active", visible);
  if (visible && !files.length) void loadTree();
  if (visible) renderEditor();
  applyLayout();
  fitCompanion();
}

export function toggle(): void { show(!visible); }

export function initCodePane(container: HTMLElement): void {
  root = container;
  treeEl = el("aside.code-tree");
  tabsEl = el("div.code-tabs");
  editorEl = el("div.code-editor");
  noteEl = el("div.code-note.muted", { hidden: true });
  sideTabsEl = el("div.code-side-tabs.pill-row");
  sideEl = el("div.code-side-body");
  const mainEl = el("section.code-main", {}, tabsEl, editorEl, noteEl);
  const sideSection = el("section.code-side", {}, sideTabsEl, sideEl);
  const viewEl = () => document.getElementById("view");
  mount(container, el("div.code-root", {},
    // chat | tree | editor | side, with a drag strip at each seam.
    resizer("chat", (x) => x - (viewEl()?.getBoundingClientRect().left ?? 0), (w) => { layout.chat = Math.max(320, w); }),
    treeEl,
    resizer("tree", (x) => x - treeEl.getBoundingClientRect().left, (w) => { layout.tree = Math.min(400, Math.max(120, w)); }),
    mainEl,
    resizer("side", (x) => sideSection.getBoundingClientRect().right - x, (w) => { layout.side = Math.max(260, w); }),
    sideSection,
  ));
  applyLayout();
  renderTree(); renderTabs(); renderEditor(); renderSide();
  show(false);

  // Follow the runs: the executor publishes these for this pane.
  on("run", (e) => {
    const d = e.data as any;
    if (e.type === "tool_progress") {
      live = { name: d.name, path: d.path ?? null, chars: d.chars ?? 0, seconds: d.seconds ?? 0,
               text: d.tail ?? "", status: "writing" };
      if (side === "live" && visible) renderSide(); else if (visible) renderSide();
    } else if (e.type === "tool") {
      if (live && live.status === "writing") { live.status = "executing"; }
      if (visible) renderSide();
    } else if (e.type === "tool_result") {
      if (live) { live.status = d.ok ? "done" : "failed"; if (d.path) live.path = d.path; }
      if (d.path && ["write_file", "append_file", "edit_lines", "replace_in_file"].includes(d.tool)) {
        // The file changed on disk: reload open tabs (or warn), record the diff, refresh the preview.
        if (open.some((f) => f.path === d.path)) void reloadFromDisk(d.path, d.tag ?? null);
        else void recordDiffFor(d.path, d.tool, d.tag ?? null);
        if (!files.some((f) => f.path === d.path)) void loadTree();
        if (previewPath === d.path && side === "preview" && visible) renderSide();
        if (/\.html?$/i.test(d.path) && !previewPath) previewPath = d.path;
      }
      if (visible) renderSide();
    } else if (e.type === "end") {
      if (live && live.status === "writing") live.status = d.status === "cancelled" ? "cancelled" : "done";
      if (visible) renderSide();
    }
  });
}

async function recordDiffFor(path: string, tool: string, tag: string | null): Promise<void> {
  try {
    const body = await get<{ content: string; tag: string }>(`/api/workspace/read?path=${encodeURIComponent(path)}`);
    const before = beforeByPath.get(path) ?? "";
    diffs.unshift({ tool, path, before, after: body.content, tag: tag ?? body.tag });
    beforeByPath.set(path, body.content);
  } catch { /* gone */ }
  if (visible) renderSide();
}
