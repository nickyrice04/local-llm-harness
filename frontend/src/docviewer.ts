/** docviewer.ts — the full-screen document viewer.
 *
 * Reports (and later: any md/PDF the harness produces) deserve a READING
 * experience, not a tile: this opens over the whole app like its own
 * window — modern typography, its own light/dark switch independent of
 * the app theme, a navigable outline, source cards with images, export —
 * and an X (or Escape) drops you straight back into Seymour.
 *
 * Deliberately NOT the app's retro look: the chrome around a document is
 * the handheld; the document itself is for reading. Model output still
 * renders only through md.ts's sanitizing gate; source thumbnails are
 * plain <img> nodes whose src is a validated http(s) string from the
 * research pipeline's own records.
 */

import { el } from "./dom";
import { renderMarkdown } from "./md";

export interface DocSource { url: string; image?: string }

export interface DocInput {
  title: string;
  markdown?: string;             // markdown content (rendered via md.ts)
  fileUrl?: string;              // alternative: an embeddable file (pdf)
  meta?: string;                 // "done · 6 sources" — the stats line
  sources?: DocSource[];         // source cards (image + domain)
}

/** The viewer's own light/dark choice, remembered across documents. */
const MODE_KEY = "seymour-docviewer-mode";

let overlay: HTMLElement | null = null;
let keyHandler: ((event: KeyboardEvent) => void) | null = null;

export function closeDocument(): void {
  overlay?.remove();
  overlay = null;
  document.body.classList.remove("docv-printing");
  if (keyHandler) {
    window.removeEventListener("keydown", keyHandler);
    keyHandler = null;
  }
}

/** Turn heading text into a stable in-page anchor id. */
function slug(text: string, index: number): string {
  return "dv-" + index + "-"
    + text.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40);
}

export function openDocument(doc: DocInput): void {
  closeDocument();                       // one document at a time

  const mode = localStorage.getItem(MODE_KEY) ?? "dark";
  const root = el("div.docv", {});
  root.dataset.mode = mode;

  // ---- The body ----------------------------------------------------------
  const body = el("div.docv-body");
  if (doc.markdown) {
    renderMarkdown(body, doc.markdown);
  } else if (doc.fileUrl && /\.html?(\?|$)/i.test(doc.fileUrl.split("path=").pop() ?? doc.fileUrl)) {
    // An HTML APP the agent produced: it may run its own scripts, but in
    // a sandbox — no network, no access to Seymour's origin, no top-level
    // navigation. Full height, like a preview pane.
    const frame = document.createElement("iframe");
    // Sandboxed by the server's CSP on the page itself (no attribute: a
    // frame with both was blocked in the desktop app's browser pane).
    frame.src = doc.fileUrl;
    frame.className = "docv-embed";
    frame.title = doc.title;
    body.append(frame);
  } else if (doc.fileUrl) {
    // A file the browser can display itself (PDF): full-height embed.
    const embed = document.createElement("embed");
    embed.src = doc.fileUrl;
    embed.className = "docv-embed";
    body.append(embed);
  }

  // ---- The outline, from the rendered headings ---------------------------
  const toc = el("nav.docv-toc");
  body.querySelectorAll("h1, h2, h3").forEach((heading, index) => {
    const id = slug(heading.textContent ?? "", index);
    heading.id = id;
    toc.append(el("a", {
      className: `docv-toc-${heading.tagName.toLowerCase()}`,
      onclick: () => document.getElementById(id)
        ?.scrollIntoView({ block: "start", behavior: "smooth" }),
    }, heading.textContent ?? ""));
  });

  // ---- Source cards (the Odysseus look: thumbnail + domain) --------------
  let sourcesBlock: HTMLElement | null = null;
  const sources = (doc.sources ?? []).filter((s) => s.url);
  if (sources.length) {
    const grid = el("div.docv-sources");
    for (const source of sources) {
      let domain = source.url;
      try { domain = new URL(source.url).hostname.replace(/^www\./, ""); }
      catch { /* keep the raw string */ }
      const card = el("a.docv-source", {
        title: source.url,
        onclick: () => window.open(source.url, "_blank", "noopener"),
      });
      if (source.image) {
        const img = document.createElement("img");
        img.src = source.image;          // validated http(s) at capture time
        img.loading = "lazy";
        img.onerror = () => img.remove();  // a dead thumbnail hides itself
        card.append(img);
      }
      card.append(el("span", {}, domain));
      grid.append(card);
    }
    sourcesBlock = el("div.docv-sources-wrap", {},
      el("h2.docv-sources-title", {}, "Sources"), grid);
  }

  // ---- The top bar -------------------------------------------------------
  const modeButton = el("button.docv-btn", {
    onclick: () => {
      const next = root.dataset.mode === "dark" ? "light" : "dark";
      root.dataset.mode = next;
      localStorage.setItem(MODE_KEY, next);
    },
    title: "the viewer's own light/dark — independent of the app theme",
  }, "◐");
  const bar = el("div.docv-bar", {},
    el("button.docv-btn.docv-close", { onclick: closeDocument,
      title: "back to Seymour (Esc)" }, "✕"),
    el("span.docv-bar-title", {}, doc.title),
    modeButton,
    doc.markdown ? el("button.docv-btn", {
      onclick: () => {
        // Client-side download: the markdown is already in hand.
        const blob = new Blob([doc.markdown ?? ""],
                              { type: "text/markdown" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = doc.title.toLowerCase()
          .replace(/[^a-z0-9]+/g, "-").slice(0, 60) + ".md";
        a.click();
        URL.revokeObjectURL(a.href);
      },
    }, "Download .md") : null,
    el("button.docv-btn", {
      onclick: () => {
        document.body.classList.add("docv-printing");
        window.print();
        document.body.classList.remove("docv-printing");
      },
    }, "Print / PDF"),
  );

  root.append(bar,
    el("div.docv-layout", {},
      toc,
      el("div.docv-main", {},
        el("h1.docv-title", {}, doc.title),
        doc.meta ? el("p.docv-meta", {}, doc.meta) : null,
        body,
        sourcesBlock,
      )));
  overlay = root;
  document.body.append(root);

  keyHandler = (event: KeyboardEvent) => {
    if (event.key === "Escape") closeDocument();
  };
  window.addEventListener("keydown", keyHandler);
}
