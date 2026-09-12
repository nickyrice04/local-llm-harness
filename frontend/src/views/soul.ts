/** soul.ts — the character editor.
 *
 * The soul is one Markdown file on the user's disk; this view is just a
 * textarea over it. The one non-obvious note surfaces in the helper text:
 * the soul is the STABLE prefix of every prompt, so keeping it short and
 * static is what keeps the prompt cache effective.
 */

import { get, put } from "../api";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";

export function show(container: HTMLElement): ViewHandle {
  // Async fill-in: render the shell immediately, load the text after.
  const editor = el("textarea", { rows: 22, className: "soul-editor" });
  const saved = el("span.muted", {});
  get<{ text: string }>("/api/soul").then((soul) => { editor.value = soul.text; });

  mount(container,
    el("h2", {}, "Seymour's soul"),
    el("p.muted", {}, "This file IS Seymour's character — it leads every "
      + "prompt, chat and agent alike. Keep it short: every word is paid on "
      + "every request, and keeping it stable is what lets the prompt cache "
      + "skip re-reading it each turn."),
    editor,
    el("div.row", {},
      el("button.primary", {
        onclick: async () => {
          await put("/api/soul", { text: editor.value });
          saved.textContent = "Saved — takes effect on the next request.";
          setTimeout(() => (saved.textContent = ""), 4000);
        },
      }, "Save soul"),
      saved,
    ),
  );

  // Nothing subscribed, nothing streaming — teardown is a no-op.
  return { destroy() { /* no subscriptions to release */ } };
}
