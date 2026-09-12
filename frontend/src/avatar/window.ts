/** window.ts — Seymour's reserved area: geometry + the health field.
 *
 * The area is a fixed column of the app (right by default, left or off
 * in Settings) that always belongs to Seymour: the avatar frame up top
 * (vertically resizable in place), and the METRICS FIELD below — the
 * concurrency queue: every engine slot as a row with its occupant and
 * live tok/s, plus battery. Real numbers only, same as everything else.
 *
 * Geometry the user adjusts persists through theme.ts prefs: the panel's
 * width (drag the inner-edge strip) and the frame's height (native
 * vertical resize grip).
 */

import { get } from "../api";
import { on } from "../bus";
import { el } from "../dom";
import { prefs, save } from "../theme";

/** Wire the whole panel up (called once from main.ts). */
export function initAvatarWindow(): void {
  const drag = document.getElementById("companion-drag")!;
  const frame = document.getElementById("avatar-frame")!;
  const health = document.getElementById("aw-health")!;

  // ---- Width: drag the inner-edge strip ---------------------------------
  // Pointer events so mouse, trackpad and touch share one code path.
  let dragging = false;
  drag.addEventListener("pointerdown", (event) => {
    dragging = true;
    drag.setPointerCapture(event.pointerId);
  });
  drag.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    // The panel's width is the distance from the pointer to the app's
    // matching edge — which edge depends on which side Seymour lives on.
    const left = document.body.classList.contains("avatar-left");
    const width = left
      ? event.clientX - 230                  // panel starts after the sidebar
      : innerWidth - event.clientX;
    // save() clamps (200..420) and re-applies the CSS variable live.
    save({ avatarPanelWidth: Math.round(width) });
  });
  drag.addEventListener("pointerup", (event) => {
    dragging = false;
    drag.releasePointerCapture(event.pointerId);
  });

  // ---- Frame height: persist the native vertical resize ------------------
  // The grip changes height without JS; the observer notices and saves
  // (debounced — resize fires continuously while dragging).
  let saveTimer: number | undefined;
  new ResizeObserver(() => {
    const height = Math.round(frame.getBoundingClientRect().height);
    if (Math.abs(height - prefs().avatarFrameHeight) < 4) return;  // echo
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save({ avatarFrameHeight: height }),
                           400) as unknown as number;
  }).observe(frame);

  // ---- The metrics + health field ----------------------------------------
  const metrics = document.getElementById("aw-metrics")!;

  /** What KIND of work a stream label is: the honest operation name.
   *  Labels are minted at admission ("chat:4f2a", "research:9c01",
   *  "agent:77b2", "memory:extract", "chat:title", "verify:…"); the tier
   *  disambiguates the two agent kinds (Tier 2 = discrete task, Tier 3 =
   *  the primary seat). */
  function kindOf(label: string, tier: string): string {
    if (label.startsWith("research")) return "research";
    if (label.startsWith("agent")) {
      return tier === "background_agent" ? "primary" : "agent";
    }
    if (label === "memory:extract") return "memory";
    if (label === "chat:title") return "title";
    if (label.startsWith("verify")) return "check";
    return "chat";
  }

  /** One refresh: metrics block + a row per engine slot + the wait queue. */
  const refreshHealth = async () => {
    try {
      const status = await get<{
        capabilities: { model_id: string; total_slots: number;
                        measured_speedup: number; engine_name?: string } | null;
        scheduler: {
          mode?: string;
          draining?: boolean;
          slots_total?: number;
          active?: { label: string; tier: string; tokens: number;
                     tps: number; phase: string }[];
          queued?: { label: string; tier: string }[];
        };
        battery: { on_battery: boolean; percent: number | null };
        system: { ram_used_gb: number; ram_total_gb: number;
                  ram_percent: number; gpu_percent: number | null };
        mtp?: { available: boolean; enabled: boolean; acceptance: number;
                missed: boolean; external: boolean };
      }>("/api/status");

      // ---- The metrics block: model, gpu, memory ------------------------
      // (The slots/speedup story is told by the SLOTS rows themselves —
      // repeating it as a line was noise, not information.)
      const lines: HTMLElement[] = [];
      if (status.capabilities) {
        lines.push(el("div.metric-line", { title: "the loaded model" },
          status.capabilities.model_id));
        if (status.scheduler.draining) {
          // An unload in progress is a real state — say it, don't hide it.
          lines.push(el("div.metric-line.muted",
            { title: "unload requested: no new work is admitted; the "
                     + "engine stops when running work finishes" },
            "unloading — waiting for work"));
        }
      } else {
        lines.push(el("div.metric-line.muted", {}, "no model loaded"));
      }
      // Power source: on battery macOS duty-cycles GPU compute (measured
      // 2026-09-03: bursts of full speed, then seconds at ~2 tok/s). A
      // reply that stutters is the machine saving power, not a bug — and
      // every number measured meanwhile is a duty-cycle average.
      if (status.battery?.on_battery) {
        lines.push(el("div.metric-line.muted",
          { title: "the GPU is power-managed on battery: generation runs in "
                   + "bursts, and handshake numbers taken now are not "
                   + "representative — plug in to measure" },
          "on battery · gpu duty-cycled, replies come in bursts"));
      }
      // MTP: on = free speed on structured output; available-but-off =
      // speed sitting on the table, with the reason named.
      const mtp = status.mtp;
      if (mtp?.enabled) {
        lines.push(el("div.metric-line.muted",
          { title: "multi-token prediction: the model's own draft head "
                   + "proposes tokens the full model verifies in one pass" },
          `mtp on · ${Math.round(mtp.acceptance * 100)}% accepted`
          // On MLX the drafter's server serves one batch at a time; say it
          // next to the speed it buys, so a queued chat reads as the
          // trade that was chosen and not as a stall.
          + (status.capabilities?.engine_name?.startsWith("mlx-vlm")
             ? " · single-stream server, other requests wait" : "")));
      } else if (mtp?.missed) {
        lines.push(el("div.metric-line.muted",
          { title: mtp.external
              ? "these weights ship MTP draft heads, but this llama-server "
                + "was started without --spec-type draft-mtp. Restart it "
                + "with that flag (or let Seymour launch the engine) for "
                + "markedly faster structured output."
              : "these weights ship MTP heads that aren't being used" },
          "mtp available (off)"));
      }
      if (status.system.gpu_percent != null) {
        // The driver's own "Device Utilization %" — the number Activity
        // Monitor's GPU history graphs.
        lines.push(el("div.metric-line.muted",
          { title: "GPU busy (the driver's Device Utilization)" },
          `gpu ${status.system.gpu_percent}%`));
      }
      if (status.system.ram_total_gb) {
        // Activity-Monitor-style Memory Used (App + Wired + Compressed) —
        // and on this machine unified memory IS the GPU's memory.
        lines.push(el("div.metric-line.muted",
          { title: "unified memory: the GPU and everything else share it "
                   + "(App + Wired + Compressed, like Activity Monitor)" },
          `memory ${status.system.ram_used_gb}/${status.system.ram_total_gb} GB `
          + `(${status.system.ram_percent}%)`));
      }
      metrics.replaceChildren(...lines);

      if (!status.capabilities) {
        health.replaceChildren(
          el("div.slot-row.idle", {}, el("span.slot-dot"), "no model loaded"));
        return;
      }

      // ---- The slots: the up-to-4 concurrent pipelines ------------------
      const total = status.scheduler.slots_total ?? 0;
      const active = status.scheduler.active ?? [];
      const rows: HTMLElement[] = [];
      for (let i = 0; i < total; i++) {
        const stream = active[i];
        if (stream) {
          // Kind + phase + speed: "research · decode · 41 t/s".
          const kind = kindOf(stream.label, stream.tier);
          rows.push(el("div.slot-row.busy", {},
            el("span.slot-dot"),
            el("span.grow", { title: `${stream.label} (${stream.tier})` }, kind),
            el("span.phase", {}, stream.phase),
            el("span.tps", {},
               stream.phase === "decode" ? `${stream.tps} t/s` : "…")));
        } else {
          rows.push(el("div.slot-row.idle", {},
            el("span.slot-dot"), el("span.grow", {}, `slot ${i + 1}`),
            el("span", {}, "idle")));
        }
      }
      // ---- The wait queue: work that can't start until a slot frees -----
      const queued = status.scheduler.queued ?? [];
      if (queued.length) {
        rows.push(el("div.queue-sep", {}, "waiting"));
        for (const waiter of queued.slice(0, 6)) {
          rows.push(el("div.slot-row.queued", {},
            el("span.slot-dot"),
            el("span.grow", { title: waiter.label },
               kindOf(waiter.label, waiter.tier)),
            el("span", {}, "queued")));
        }
      }
      if (status.battery.on_battery) {
        rows.push(el("div.health-note", {},
          `battery ${status.battery.percent ?? "?"}% — agent pacing itself`));
      }
      health.replaceChildren(...rows);
    } catch {
      metrics.replaceChildren();
      health.replaceChildren(
        el("div.slot-row.idle", {}, el("span.slot-dot"), "offline"));
    }
  };
  // Real transitions trigger it, and a clock keeps the numbers moving
  // while a stream runs (its tps changes with no event to announce it).
  on("scheduler", refreshHealth);
  on("engine", refreshHealth);
  setInterval(refreshHealth, 2000);
  refreshHealth();
}
