/** memory.ts — the reviewable memory store.
 *
 * Full transparency by design: every fact Seymour holds is listed here
 * with its provenance (you typed it / the agent saved it / distilled from
 * chat), and the search box runs the EXACT retrieval chat uses — so you
 * can see precisely what would be injected for any question.
 */

import { get, post, del } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";

/** Provenance → friendly wording. */
const SOURCES: Record<string, string> = {
  user: "added by you",
  agent: "saved by the agent",
  chat: "distilled from chat",
};

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()

  /** Redraw the whole view (optionally with search results on top). */
  async function refresh(searchResults?: { content: string; score: number }[]): Promise<void> {
    const memories = await get<{
      id: number; content: string; kind: string; source: string;
      pinned: boolean; uses: number; created_at: string;
    }[]>("/api/memories");

    // ---- Add + search controls ------------------------------------------
    const newFact = el("input", { placeholder: "Teach Seymour a fact to remember…" });
    const addForm = el("form.composer", {
      onsubmit: async (event: Event) => {
        event.preventDefault();
        if (!newFact.value.trim()) return;
        await post("/api/memories", { content: newFact.value });
        newFact.value = "";
        refresh();
      },
    }, newFact, el("button.primary", {}, "Remember"));

    const query = el("input", { placeholder: "Test retrieval: what would chat inject for…?" });
    const searchForm = el("form.composer", {
      onsubmit: async (event: Event) => {
        event.preventDefault();
        if (!query.value.trim()) return;
        const hits = await get<{ content: string; score: number }[]>(
          `/api/memories/search?q=${encodeURIComponent(query.value)}`);
        refresh(hits);
      },
    }, query, el("button", {}, "Test retrieval"));

    // ---- Search results (when a test ran) -------------------------------
    const resultsCard = searchResults ? el("div.card", {},
      el("h3", {}, "What retrieval returns"),
      searchResults.length
        ? el("div", {}, ...searchResults.map((hit) =>
            el("div.row", {},
              el("span.grow", {}, hit.content),
              el("span.muted", {}, `score ${hit.score}`))))
        : el("p.muted", {}, "Nothing passes the relevance gates for that query."),
    ) : null;

    // ---- The store itself ------------------------------------------------
    // Pinned first (the backend orders it that way), each row carrying
    // its flags: category chip, pin state, and the honest uses counter.
    const rows = memories.map((memory) => el("div.card", {},
      el("div.row", {},
        el("span.grow", {}, memory.content),
        memory.pinned ? el("span.badge.done", {}, "pinned") : null,
        el("span.badge", {}, memory.kind),
        memory.uses > 0
          ? el("span.muted", { title: "times injected into a chat" },
               `${memory.uses}×`) : null,
        el("span.muted", {}, SOURCES[memory.source] ?? memory.source),
        el("button", {
          title: memory.pinned
            ? "Unpin — back to retrieval only"
            : "Pin — identity/contact pins ride in every chat",
          onclick: async () => {
            await post(`/api/memories/${memory.id}/pin`,
                       { pinned: !memory.pinned });
            refresh();
          },
        }, memory.pinned ? "Unpin" : "Pin"),
        el("button.danger", {
          onclick: async () => { await del(`/api/memories/${memory.id}`); refresh(); },
        }, "Forget"),
      ),
    ));

    mount(container,
      el("h2", {}, "Memory"),
      el("p.muted", {}, "Everything Seymour remembers between conversations, "
        + "with where each fact came from. Retrieval is hybrid: meaning "
        + "(vectors) + wording (keywords) + freshness."),
      addForm,
      searchForm,
      resultsCard,
      ...(rows.length ? rows : [el("p.muted", {}, "No memories yet. Chat with "
        + "Seymour, or teach it a fact above.")]),
    );
  }

  // Background additions (extractor, agent) update the view — only while
  // it is actually the one showing (destroy() tears this down).
  subs.push(on("memory", () => { refresh(); }));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
