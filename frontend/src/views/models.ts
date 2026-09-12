/** models.ts — the brains cupboard: local models, HF search, downloads.
 *
 * Downloading follows the two-step the backend enforces: pick a repo from
 * search, then pick ONE .gguf file (the quant) from that repo. Progress
 * streams over the bus; the partial blob survives cancels for resume.
 */

import { get, post } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";

/** GET /api/models/engine: what was ASKED for, and what was MEASURED. */
interface EngineInfo {
  settings: {
    mtp: string; mtp_draft_n: number; n_slots: number; ctx_size: number;
    mlx_server: string; mlx_decode_concurrency: number; mlx_prompt_concurrency: number;
    mlx_prompt_cache_gb: number; mlx_draft_tokens: number;
  };
  measured: {
    total_slots: number; concurrent: boolean; measured_speedup: number;
    measured_on_battery?: boolean;
    mtp_enabled: boolean; mtp_acceptance: number; context_per_slot: number;
  } | null;
}

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()
  /** Which format the "Get more" search looks for. Follows the loaded
   *  model's backend by default (an MLX brain wants MLX downloads); the
   *  pills switch it. */
  let searchBackend: "gguf" | "mlx" = "gguf";
  let searchBackendChosen = false;
  /** The live download bar's fill element (updated from bus events). */
  let barFill: HTMLElement | null = null;
  let barLabel: HTMLElement | null = null;
  /** True while a load/unload is in flight — buttons disable, and the
   *  notice line narrates (loads take minutes; silence reads as broken). */
  let engineBusy = false;
  /** The one-line status/error notice above the list. */
  let notice = "";
  let noticeKind = "";                         // "" | "error"
  /** True while an unload is DRAINING (waiting for live work to finish);
   *  the UI offers "Cancel unload" instead of pretending it's done. */
  let draining = false;
  /** The busy-engine unload dialog, when open (removed on destroy). */
  let unloadScrim: HTMLElement | null = null;

  /** The hardware-fit verdict as a colored chip (cookbook-style): will
   *  this model actually run well on THIS machine? */
  function fitChip(fit: { level: string; required_gb: number; budget_gb: number } | null): HTMLElement | null {
    if (!fit) return null;
    const words: Record<string, string> = {
      perfect: "fits great", good: "fits", tight: "tight fit", too_big: "too big",
    };
    return el("span", {
      className: `badge fit-${fit.level}`,
      title: `needs ~${fit.required_gb} GB of this machine's ${fit.budget_gb} GB budget`,
    }, words[fit.level] ?? fit.level);
  }

  /** Redraw everything (local list + search area + download state). */
  async function refresh(): Promise<void> {
    const [local, download, engine] = await Promise.all([
      get<{ name: string; path: string; size: string; active: boolean; mtp: boolean;
            backend: "gguf" | "mlx"; drafter?: string | null; quant?: string;
            context_max?: number; vision?: boolean; notes?: string[];
            fit: { level: string; required_gb: number; budget_gb: number } }[]>("/api/models"),
      get<{ active: boolean; repo?: string; file?: string }>("/api/models/download/status"),
      get<EngineInfo>("/api/models/engine"),
    ]);

    /** Run one engine action with busy-state + notice bookkeeping. */
    const engineAction = async (label: string, action: () => Promise<unknown>) => {
      engineBusy = true;
      notice = label; noticeKind = "";
      refresh();                               // repaint disabled buttons now
      try {
        await action();
        notice = "";                           // bus events narrate from here
      } catch (error: any) {
        notice = error?.message ?? "engine action failed";
        noticeKind = "error";
      } finally {
        engineBusy = false;
        refresh();
      }
    };

    /** The unload flow (4.2): ask first. An idle engine unloads on the
     *  spot; a busy one answers with its live work, and the DIALOG makes
     *  the user choose — finish it, stop it, or keep the model. Unload
     *  never silently kills work and never lies about being done. */
    const requestUnload = async (name: string) => {
      try {
        const result = await post<{
          unloaded: boolean; busy?: boolean; draining?: boolean;
          active?: string[]; queued?: string[];
        }>("/api/models/unload", { mode: "check" });
        if (result.busy) {
          openUnloadDialog(name, [...(result.active ?? []),
                                  ...(result.queued ?? [])]);
        } else {
          refresh();                   // idle: it unloaded right there
        }
      } catch (error: any) {
        notice = error?.message ?? "unload failed";
        noticeKind = "error";
        refresh();
      }
    };

    const openUnloadDialog = (name: string, work: string[]) => {
      unloadScrim?.remove();
      const scrim = el("div.dialog-scrim", {
        onclick: (event: Event) => {
          if (event.target === scrim) { scrim.remove(); unloadScrim = null; }
        },
      });
      const listing = work.slice(0, 6).join(", ")
        + (work.length > 6 ? ` (+${work.length - 6} more)` : "");
      scrim.append(el("div.card.dialog", {},
        el("h3", {}, "Work is still running"),
        el("p", {}, `${name} is busy right now: ${listing}. How should the `
          + "unload happen?"),
        el("div.dialog-actions", {},
          el("button.primary", {
            type: "button",
            onclick: () => {
              scrim.remove(); unloadScrim = null;
              engineAction("Unloading once current work finishes…",
                () => post("/api/models/unload", { mode: "drain" }));
            },
          }, "Drain — finish current work, then unload"),
          el("button.danger", {
            type: "button",
            onclick: () => {
              scrim.remove(); unloadScrim = null;
              engineAction("Stopping the work, then unloading…",
                () => post("/api/models/unload", { mode: "cancel" }));
            },
          }, "Stop the work and unload now"),
          el("button", {
            type: "button",
            onclick: () => { scrim.remove(); unloadScrim = null; },
          }, "Keep the model loaded"),
        )));
      unloadScrim = scrim;
      document.body.append(scrim);
    };

    /** The engine card: the options that shape the NEXT launch, next to
     *  what the running engine actually measured. Keeping request and
     *  reality side by side is the whole honesty story — a setting is a
     *  request; only the handshake's numbers are facts. */
    function engineCard(info: EngineInfo, drafterAvailable = false): HTMLElement {
      const s = info.settings;
      const m = info.measured;
      const save = (change: Record<string, unknown>) =>
        engineAction("Saved — applies on the next load.",
                     () => post("/api/models/engine", change));

      const rows: (HTMLElement | null)[] = [];
      // What the running engine measured, in its own words.
      if (m) {
        rows.push(el("div.row", {},
          el("span.badge", {
            className: `badge ${m.concurrent ? "done" : "failed"}`,
            title: m.measured_on_battery
              ? "measured by the handshake ON BATTERY: the GPU was duty-"
                + "cycled, so the speedup ratio is not meaningful — the "
                + "verdict (requests overlap or not) still is. Re-measure on AC."
              : "measured by the startup handshake, not configured",
          }, (m.concurrent ? "concurrent" : "serial")
             + (m.measured_on_battery ? " · measured on battery"
                                      : ` · ${m.measured_speedup}×`)),
          el("span.muted", {}, `${m.total_slots} engine slots`),
          m.mtp_enabled
            ? el("span.badge.done", { title: "self-speculative decoding is "
                 + "running; accepted draft tokens are nearly free" },
                 `MTP ${Math.round(m.mtp_acceptance * 100)}% accepted`)
            : null,
          el("button", {
            disabled: engineBusy,
            title: "re-run the handshake against the warm engine. The "
                   + "concurrency probe decides concurrent-vs-serial, and a "
                   + "cold or contended machine can measure low once.",
            onclick: () => engineAction("Re-measuring the engine…",
              () => post("/api/models/remeasure")),
          }, "Re-measure"),
        ));
        if (!m.concurrent && m.total_slots > 1) {
          // The exact trap worth naming: the engine HAS slots, but the
          // measurement said they don't overlap, so Seymour is using one.
          rows.push(el("p.notice", {}, `This engine has ${m.total_slots} `
            + "slots, but they measured as not overlapping, so Seymour is "
            + "using one. If the machine was busy or the model had just "
            + "loaded, Re-measure on a warm engine."));
        }
      }

      // The options themselves — each applies on the next load.
      const mtpPills = (["auto", "off"] as const).map((mode) =>
        el("button", {
          className: `choice${s.mtp === mode ? " active" : ""}`,
          disabled: engineBusy,
          onclick: () => save({ mtp: mode }),
        }, mode === "auto" ? "auto (use MTP when present)" : "off"));

      const numberField = (label: string, key: string, value: number,
                           hint: string) => {
        const input = el("input", { value: String(value), title: hint,
                                    autocomplete: "off" }) as HTMLInputElement;
        input.type = "number";
        input.style.width = "110px";
        return el("div.row", {},
          el("span.grow", {}, label),
          input,
          el("button", {
            disabled: engineBusy,
            onclick: () => save({ [key]: Number(input.value) }),
          }, "Save"));
      };

      // The MLX server choice: measured default ("auto"), or a person's
      // explicit call. Each option says what it trades.
      const serverPills = (["auto", "mlx-lm", "mlx-vlm"] as const).map((mode) =>
        el("button", {
          className: `choice${s.mlx_server === mode ? " active" : ""}`,
          disabled: engineBusy,
          title: mode === "auto"
            ? "measured default: mlx-lm, unless MTP is on and a drafter exists"
            : mode === "mlx-lm"
              ? "batching + prompt cache: 3 streams at ~17 tok/s each with full "
                + "overlap, late arrivals answer in ~0.5 s (measured 2026-09-03)"
              : "MTP drafting: ~27-32 tok/s solo, but batch-at-a-time — a request "
                + "arriving mid-batch waits (~6.7 s first token, measured)",
          onclick: () => save({ mlx_server: mode }),
        }, mode));

      return el("div.card", {},
        el("h3", {}, "Engine"),
        ...rows.filter((r): r is HTMLElement => r !== null),
        el("div.row", {}, el("span.grow", {}, "Multi-token prediction"),
           ...mtpPills),
        // The trade MTP makes on MLX, said where the choice is made (the
        // numbers are this machine's, measured 2026-09-03 on AC): the
        // drafter lives in mlx-vlm, which serves one batch at a time.
        (s.mtp !== "off" || s.mlx_server === "mlx-vlm" || drafterAvailable)
          ? el("div.notice", { title: "measured: mlx-vlm + drafter 27-32 tok/s solo "
                 + "vs mlx-lm 17 tok/s; three streams 29 vs 48 tok/s aggregate; "
                 + "a request arriving mid-stream waits 6.7 s vs 0.6 s" },
               "MTP on MLX trades concurrency for single-stream speed: one reply "
               + "at a time runs ~1.7× faster, but the drafter's server (mlx-vlm) "
               + "serves one batch at a time — a chat sent while the agent is "
               + "streaming waits for it, and several streams share less speed than "
               + "mlx-lm's batching gives them. Keep \"auto\" + mlx-lm for a person "
               + "and an agent working together; choose MTP for solo sessions. "
               + "On llama.cpp (.gguf) MTP has no such cost — it drafts inside the "
               + "same slots.")
          : null,
        numberField("Context size (total)", "ctx_size", s.ctx_size,
                    "the KV budget: --ctx-size for llama.cpp, the per-request "
                    + "window cap for MLX"),
        el("h4", {}, "llama.cpp (.gguf models)"),
        numberField("Draft tokens per step", "mtp_draft_n", s.mtp_draft_n,
                    "how many tokens the MTP head proposes at once"),
        numberField("Slots (concurrent sequences)", "n_slots", s.n_slots,
                    "--parallel: how many generations can run at once"),
        el("h4", {}, "MLX (checkpoint folders)"),
        el("div.row", {}, el("span.grow", {}, "Server"), ...serverPills),
        numberField("Decode concurrency", "mlx_decode_concurrency", s.mlx_decode_concurrency,
                    "mlx-lm: sequences decoded together per step"),
        numberField("Prompt concurrency", "mlx_prompt_concurrency", s.mlx_prompt_concurrency,
                    "mlx-lm: prompts prefilled together per step"),
        numberField("Prompt cache (GB)", "mlx_prompt_cache_gb", s.mlx_prompt_cache_gb,
                    "mlx-lm: memory kept for reusable KV prefixes"),
        numberField("MTP draft tokens", "mlx_draft_tokens", s.mlx_draft_tokens,
                    "mlx-vlm: tokens the drafter proposes per step"),
        el("p.muted", {}, "Engine options apply the next time a model is "
          + "loaded — a server's launch flags can't change under a running "
          + "process. The model's kind picks the engine: a .gguf file "
          + "launches llama.cpp, a checkpoint folder launches MLX."),
      );
    }

    // ---- Local models ----------------------------------------------------
    const localCards = local.map((model) => el("div.card", {},
      el("div.row", {},
        el("h3.grow", {}, model.name),
        // Which engine will serve it — decided by the weights' kind.
        el("span.badge", { className: `badge backend-${model.backend}`,
          title: model.backend === "mlx"
            ? "a checkpoint folder: served by the MLX engine"
            : "a .gguf file: served by llama.cpp" },
          model.backend === "mlx" ? "MLX" : "GGUF"),
        el("span.muted", {}, model.size),
        fitChip(model.fit),
        // MTP: for a .gguf, the file carries draft heads; for MLX, a
        // compatible drafter checkpoint sits next to the weights.
        model.mtp ? el("span.badge", { className: "badge mtp",
          title: model.drafter
            ? `drafter checkpoint found: ${model.drafter} — Seymour can run `
              + "MTP speculative decoding with it"
            : "ships multi-token-prediction draft heads — Seymour "
              + "enables self-speculative decoding for it" }, "MTP") : null,
        model.active ? el("span.badge.done", {}, "loaded") : null,
        model.active
          ? el("button.danger", {
              disabled: engineBusy || draining,
              onclick: () => requestUnload(model.name),
            }, "Unload")
          : el("button.primary", {
              disabled: engineBusy,
              onclick: () => engineAction(
                // Honest expectation-setting: a large model takes minutes.
                `Loading ${model.name} — weights + handshake can take a few `
                + "minutes for large models…",
                () => post("/api/models/activate", { path: model.path })),
            }, "Load"),
      ),
      // The facts an MLX folder states about itself (from its config).
      model.backend === "mlx" ? el("p.muted", {},
        [model.quant, model.context_max ? `${Math.round(model.context_max / 1024)}k context max` : "",
         model.vision ? "vision tower present" : "", ...(model.notes ?? [])]
          .filter(Boolean).join(" · ")) : null,
    ));

    // ---- Download progress (when one is running) -------------------------
    let downloadCard: HTMLElement | null = null;
    if (download.active) {
      barFill = el("div");
      barLabel = el("span.muted", {}, `downloading ${download.file}…`);
      downloadCard = el("div.card", {},
        el("div.row", {}, el("strong.grow", {}, download.repo ?? ""), barLabel),
        el("div.bar", {}, barFill),
        el("button.danger", {
          onclick: async () => { await post("/api/models/download/cancel"); refresh(); },
        }, "Cancel (resumable)"),
      );
    }

    // ---- Hugging Face search --------------------------------------------
    // The search follows the loaded brain's format unless a pill was
    // pressed: someone running MLX wants MLX downloads, and vice versa.
    const loaded = local.find((m) => m.active);
    if (!searchBackendChosen && loaded) searchBackend = loaded.backend;
    const backendPills = (["gguf", "mlx"] as const).map((b) =>
      el("button", {
        type: "button",
        className: `choice${searchBackend === b ? " active" : ""}`,
        title: b === "gguf" ? "single-file quants for llama.cpp"
                            : "checkpoint folders for the MLX engine (Apple Silicon)",
        onclick: () => { searchBackend = b; searchBackendChosen = true; refresh(); },
      }, b === "gguf" ? "GGUF" : "MLX"));
    const query = el("input", { placeholder: searchBackend === "mlx"
      ? "Search Hugging Face for MLX models (e.g. mlx-community Qwen3.8)…"
      : "Search Hugging Face for GGUF models…" });
    const results = el("div");
    const search = el("form.composer", {
      onsubmit: async (event: Event) => {
        event.preventDefault();
        if (!query.value.trim()) return;
        results.replaceChildren(el("p.muted", {}, "searching…"));
        const repos = await get<{ repo: string; downloads: number; drafter: boolean }[]>(
          `/api/models/search?q=${encodeURIComponent(query.value)}&backend=${searchBackend}`);
        if (!repos.length) {
          results.replaceChildren(el("p.muted", {},
            `no ${searchBackend.toUpperCase()} repos found`));
          return;
        }
        results.replaceChildren(...repos.map((r) => el("div.card", {},
          el("div.row", {},
            el("strong.grow", {}, r.repo),
            r.drafter ? el("span.badge.mtp", { title: "an MTP drafter: a companion "
              + "for its base model, not a model to load on its own" }, "drafter") : null,
            el("span.muted", {}, `${r.downloads.toLocaleString()} downloads`),
            el("button", { onclick: () => showFiles(r.repo, results) },
               searchBackend === "mlx" ? "Details" : "Files"),
          ),
        )));
      },
    }, el("div.row", {}, ...backendPills), query, el("button.primary", {}, "Search"));

    mount(container,
      el("h2", {}, "Models"),
      el("p.muted", {}, "Any GGUF file or MLX checkpoint folder can be Seymour's "
        + "brain: the kind of weights picks the engine (llama.cpp or MLX), and "
        + "the handshake re-measures every capability on swap — nothing is "
        + "assumed from a model's name."),
      // The loop-model rule, measured on this machine (NOTES.md, 2026-09-11):
      // a 3B-active MoE decodes 3-4× faster than a dense 27B, and a
      // 300-line file written token by token is the whole wall-clock gap.
      el("p.muted", {}, "Default for the agent loop: the MoE (Qwen3.6-35B-A3B on llama.cpp, "
        + "57–91 tok/s measured) — every tool round and every file write is decode-bound, and a "
        + "dense 27B at 17–32 tok/s makes the same task 3–4× slower. Keep the dense vision "
        + "model for read_image and page screenshots; loading both at once is on the roadmap."),
      // The engine status/error line (load progress, refusals, failures).
      notice ? el("p", { className: noticeKind === "error" ? "notice error" : "notice" },
                  notice) : null,
      // A draining unload narrates itself and stays cancellable — the
      // third choice remains available for as long as the wait lasts.
      draining ? el("div.row", {},
        el("p.notice.grow", {}, "Unloading — waiting for running work to "
          + "finish. New work is paused until then."),
        el("button", {
          onclick: async () => { await post("/api/models/unload/cancel"); refresh(); },
        }, "Cancel unload")) : null,
      downloadCard,
      engineCard(engine, local.some((m) => !!m.drafter)),
      el("h3", {}, "On this machine"),
      ...(localCards.length ? localCards
          : [el("p.muted", {}, "No models yet — search below to download one.")]),
      el("h3", {}, "Get more"),
      search,
      results,
    );
  }

  /** Expand a repo into its downloadable units. GGUF: one row per quant
   *  file (the quant picker). MLX: one row for the whole repo, plus an
   *  offer to fetch the matching MTP drafter. Every row carries its
   *  hardware-fit verdict BEFORE downloading — the whole point: a 30 GB
   *  mistake prevented by a chip. */
  async function showFiles(repo: string, into: HTMLElement): Promise<void> {
    const files = await get<{ file: string; size: string; drafter_repo?: string | null;
      is_drafter?: boolean;
      fit: { level: string; required_gb: number; budget_gb: number } | null }[]>(
      `/api/models/files?repo=${encodeURIComponent(repo)}&backend=${searchBackend}`);
    const startDownload = async (target: string, file: string) => {
      await post("/api/models/download", { repo: target, file });
      refresh();                                // shows the progress card
    };
    into.replaceChildren(...files.map((f) => el("div.card", {},
      el("div.row", {},
        el("span.grow", {}, f.file || `${repo} (whole checkpoint folder)`),
        el("span.muted", {}, f.size),
        fitChip(f.fit),
        el("button.primary", {
          onclick: () => startDownload(repo, f.file),
        }, f.is_drafter ? "Download drafter" : "Download"),
      ),
      f.drafter_repo ? el("div.row", {},
        el("span.muted.grow", {}, `MTP drafter available: ${f.drafter_repo} — fetch it `
          + "after the model to enable speculative decoding."),
        el("button", { onclick: () => startDownload(f.drafter_repo!, "") },
           "Download drafter"),
      ) : null,
    )));
  }

  // Download progress lands here from the bus (throttled server-side to
  // whole percents, so direct DOM writes are cheap). Torn down on switch.
  subs.push(on("download", (e) => {
    if (e.type === "progress" && barFill && barLabel) {
      barFill.style.width = `${e.data.percent}%`;
      const gb = (n: number) => (n / 1e9).toFixed(1);
      barLabel.textContent =
        `${e.data.percent}% · ${gb(e.data.done_bytes)} / ${gb(e.data.total_bytes)} GB`;
    } else if (["done", "failed", "cancelled"].includes(e.type)) {
      refresh();                                // final state: redraw list
    }
  }));
  // Engine lifecycle: narrate the slow parts and refresh on every
  // transition (the "loaded" flag reflects the RUNNING engine now).
  subs.push(on("engine", (e) => {
    if (e.type === "swapping") { notice = `Loading ${e.data.model}…`; noticeKind = ""; }
    if (e.type === "ready" || e.type === "swapped") { notice = ""; }
    if (e.type === "unloaded") { notice = ""; draining = false; }
    if (e.type === "draining") { draining = true; }
    if (e.type === "drain_cancelled") { draining = false; }
    if (e.type === "swap_failed") {
      notice = `Load failed: ${e.data.error ?? "see the log"}`;
      noticeKind = "error";
    }
    if (["swapping", "swapped", "swap_failed", "ready", "unloaded",
         "draining", "drain_cancelled"].includes(e.type)) {
      refresh();
    }
  }));

  refresh();
  return {
    destroy: () => {
      unloadScrim?.remove();           // a body-level overlay must not outlive us
      subs.forEach((u) => u());
    },
  };
}
