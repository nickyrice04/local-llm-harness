/** gallery.ts — every image that passes through Seymour, browsable.
 *
 * Uploaded chat images land here automatically (generated images will
 * too, someday). Favorite, rename, delete — the user owns the store,
 * same rule as memory.
 */

import { get, del } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";

/** One gallery item from GET /api/gallery. */
interface Item {
  id: string; name: string; source: string; favorite: boolean;
  url: string; created_at: string;
}

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()

  /** PATCH one field and redraw. */
  async function patch(id: string, body: object): Promise<void> {
    await fetch(`/api/gallery/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    refresh();
  }

  /** One tile: the image, its name, and the hover controls. */
  function tile(item: Item): HTMLElement {
    const img = el("img") as HTMLImageElement;
    img.src = item.url;
    img.loading = "lazy";                      // tiles off-screen wait
    img.alt = item.name;
    return el("div.tile", {},
      img,
      el("div.tile-bar", {},
        el("span.grow", { title: item.name }, item.name || "(unnamed)"),
        // Favorite: a filled star when on, outline when off.
        el("button", {
          title: item.favorite ? "Unfavorite" : "Favorite",
          onclick: () => patch(item.id, { favorite: !item.favorite }),
        }, item.favorite ? "★" : "☆"),
        el("button", {
          title: "Rename",
          onclick: () => {
            const name = prompt("Name this image:", item.name);
            if (name !== null) patch(item.id, { name });
          },
        }, "✎"),
        el("button.del", {
          title: "Delete",
          onclick: async () => {
            await del(`/api/gallery/${item.id}`);
            refresh();
          },
        }, "×"),
      ),
    );
  }

  /** Fetch and redraw the grid (favorites first, then newest). */
  async function refresh(): Promise<void> {
    const items = await get<Item[]>("/api/gallery");
    items.sort((a, b) => Number(b.favorite) - Number(a.favorite));
    mount(container,
      el("h2", {}, "Gallery"),
      el("p.muted", {}, "Every image that passes through Seymour — chat "
        + "uploads land here automatically. All local, like everything else."),
      items.length
        ? el("div.gallery-grid", {}, ...items.map(tile))
        : el("p.muted", {}, "No images yet — attach one in chat and it "
            + "appears here."),
    );
  }

  // New uploads land live while the tab is open.
  subs.push(on("gallery", () => refresh()));

  refresh();
  return { destroy: () => subs.forEach((u) => u()) };
}
