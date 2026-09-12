/** probe.ts — the browser end of check_page.
 *
 * The backend cannot run a web page; this tab can. When a run calls
 * check_page, the server publishes a "workspace/probe" event naming the
 * page. This module loads it in a hidden, sandboxed iframe (no
 * same-origin access, no network — the same CSP the viewer uses), the
 * injected shim inside the page posts its report to us via postMessage,
 * and we hand it back to POST /api/workspace/probe/<id>. The person's
 * own browser becomes the model's test runner, with zero installs.
 */

import { on } from "./bus";

/** Probes in flight, by id: the iframe and the cleanup timer. */
const live = new Map<string, { frame: HTMLIFrameElement; timer: number; reported: boolean }>();

async function deliver(id: string, report: unknown): Promise<void> {
  const entry = live.get(id);
  if (!entry || entry.reported) return;
  entry.reported = true;
  try {
    await fetch(`/api/workspace/probe/${encodeURIComponent(id)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report),
    });
  } catch { /* the tool times out and says so */ }
  finish(id);
}

function finish(id: string): void {
  const entry = live.get(id);
  if (!entry) return;
  clearTimeout(entry.timer);
  (entry.frame as HTMLIFrameElement & { __label?: HTMLElement }).__label?.remove();
  entry.frame.remove();
  live.delete(id);
}

/** Wire the listener once, at startup (main.ts). */
export function connectProbe(): void {
  window.addEventListener("message", (event: MessageEvent) => {
    // The sandboxed page has an opaque origin ("null"); the id in the
    // message is the only credential, and it is unguessable and short-lived.
    const data = event.data;
    if (!data || typeof data !== "object" || typeof data.seymourProbe !== "string") return;
    const id = data.seymourProbe as string;
    const entry = live.get(id);
    if (!entry) return;
    // Identity is the SOURCE window (the frame we created), not the origin.
    if (event.source !== entry.frame.contentWindow) return;
    // "done" is the full observation; "load" is the early copy kept in
    // case the page never reaches "done" (a busy loop, a crash).
    if (data.kind === "done") void deliver(id, data);
    else if (data.kind === "load") {
      const entry = live.get(id)!;
      clearTimeout(entry.timer);
      entry.timer = window.setTimeout(() => void deliver(id, data),
                                      ((data.seconds as number) ?? 3) * 1000 + 3000);
    }
  });
  on("workspace", (e) => {
    if (e.type !== "probe") return;
    const { id, url, seconds, path } = e.data as { id: string; url: string; seconds: number; path: string };
    if (!id || !url || live.has(id)) return;
    const frame = document.createElement("iframe");
    // allow-scripts only: the page runs, but cannot touch this origin
    // or the network. postMessage to the parent is the one channel.
    // Sandboxing comes from the SERVER's CSP on the probed page (sandbox
    // allow-scripts; default-src 'none'), not from an iframe attribute:
    // measured in this browser, a frame carrying BOTH the attribute and
    // the CSP sandbox ran the page but no postMessage ever arrived, while
    // the header alone let the shim report. The probe route also drops
    // allow-modals so an alert() cannot stall the observation.
    frame.title = `checking ${path}`;
    // A small VISIBLE tile, not a hidden frame: browsers throttle
    // requestAnimationFrame in hidden/offscreen frames, which would make
    // every animated page look dead — and a person watching Seymour
    // check a page is exactly the honesty this tool exists for.
    frame.style.cssText = "position:fixed;right:14px;bottom:14px;width:360px;height:225px;"
      + "border:2px solid var(--line);border-radius:8px;background:#fff;z-index:60;"
      + "box-shadow:3px 3px 0 var(--line);";
    const label = document.createElement("div");
    label.className = "probe-label";
    label.textContent = `checking ${path} · ${seconds}s`;
    label.style.cssText = "position:fixed;right:14px;bottom:243px;z-index:61;font-family:PixelHead,monospace;"
      + "font-size:8px;padding:4px 8px;background:var(--panel);border:2px solid var(--line);border-radius:6px;color:var(--ink);";
    frame.src = url;
    document.body.append(frame, label);
    frame.addEventListener("remove", () => label.remove());
    const cleanup = () => label.remove();
    (frame as HTMLIFrameElement & { __label?: HTMLElement }).__label = label;
    void cleanup;
    // If the page never posts (no <head>? blocked script?), report that.
    const timer = window.setTimeout(() => void deliver(id, {
      seymourProbe: id, kind: "timeout", errors: ["the page never reported within the window — usually a script that blocks the thread before load, or the probe frame was blocked; the file itself may be fine (open it in Preview)"],
      elements: 0, canvases: 0, ids: 0, rafCalls: 0, missingIds: [], elapsed: (seconds + 6) * 1000, title: "", bodyText: "",
    }), (seconds + 6) * 1000);
    live.set(id, { frame, timer, reported: false });
  });
}
