/** md.ts — the ONE gate between model markdown and the DOM.
 *
 * Model output is untrusted, and since round 1 the rule has been
 * textContent-only. Rendering real markdown (4.5) relaxes that rule in
 * exactly one place — this module — with a hard pipeline:
 *
 *     raw text → marked (GFM) → DOMPurify allowlist → innerHTML
 *
 * The innerHTML call below is THE ONLY ONE in the app, and nothing may
 * reach it except DOMPurify's output. Everything is bundled locally
 * (marked, dompurify, highlight.js core + a few languages) — the app
 * still makes zero network requests beyond its own backend.
 *
 * Streaming: callers re-render the whole (small) message on a throttle
 * rather than freezing prefixes — with no frozen region there is
 * nothing to corrupt, and a half-received table simply shows as plain
 * text until its second row arrives, then snaps into a table. The one
 * construct that would look BROKEN mid-stream is an unterminated code
 * fence (everything after it would render as code-colored soup), so in
 * streaming mode an odd fence count gets a temporary closer.
 */

import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import { marked } from "marked";

// The languages Seymour's own world actually produces; anything else
// renders as plain (uncolored) code rather than guessing.
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("css", css);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("html", xml);

// GFM (tables, strikethrough, task lists); breaks:true because chat
// prose treats a single newline as a line break, not a paragraph join.
marked.setOptions({ gfm: true, breaks: true });

/** What sanitized output may contain — structure, never behavior.
 *  No style, no event handlers, no iframes/svg/math (mutation-XSS
 *  vectors), no images (a chat with a local model has no business
 *  hot-linking remote images). */
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    "p", "br", "hr", "strong", "em", "del", "code", "pre", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "input",            // input: GFM task-list checkboxes
    "table", "thead", "tbody", "tr", "th", "td",
    "blockquote", "a",
  ],
  ALLOWED_ATTR: ["href", "class", "type", "checked", "disabled", "start", "align"],
};

// Links: http(s) and in-page anchors only (javascript:/data: die here),
// opening in a new tab so a click never navigates the app away.
// Task-list checkboxes render but stay inert.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    const href = node.getAttribute("href") ?? "";
    if (!/^(https?:|#)/i.test(href)) {
      node.removeAttribute("href");
      return;
    }
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
  if (node.tagName === "INPUT") {
    if (node.getAttribute("type") !== "checkbox") node.remove();
    else node.setAttribute("disabled", "");
  }
});

/** Render markdown into `target`, replacing its content.
 *
 *  `streaming: true` marks a partial message: an unterminated fence is
 *  temporarily closed so streamed code renders as code. The final call
 *  (stream end / history load) uses streaming: false and is canonical.
 */
export function renderMarkdown(target: HTMLElement, raw: string,
                               streaming = false): void {
  let text = raw;
  if (streaming) {
    const fences = (text.match(/^\s*```/gm) ?? []).length;
    if (fences % 2 === 1) text += "\n```";
  }
  const html = marked.parse(text, { async: false }) as string;
  // .md switches the container from pre-wrap (plain-text mode) to
  // normal flow — the markup owns the whitespace now.
  target.classList.add("md");
  // THE one innerHTML in the app; DOMPurify's output only.
  target.innerHTML = DOMPurify.sanitize(html, PURIFY_CONFIG);
  // Colorize code blocks after sanitization (hljs emits only escaped
  // text inside span.hljs-* — safe by construction).
  target.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block as HTMLElement);
  });
}
