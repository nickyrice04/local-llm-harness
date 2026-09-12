/** settings.ts — the look-and-feel controls.
 *
 * Every control here writes through theme.save(), which applies the
 * change INSTANTLY (CSS variables + body classes) and persists it to
 * localStorage — there is no "apply" button because there is nothing to
 * wait for. The avatar preview repaints live via its prefs subscription.
 */

import { get, post } from "../api";
import { el, mount } from "../dom";
import type { ViewHandle } from "../main";
import { ACCENTS, AVATAR_HUES, BEZELS, prefs, save } from "../theme";
import { replayTour } from "../onboarding";

export function show(container: HTMLElement): ViewHandle {

  /** A row of choice pills; the current one is marked. `pick` saves. */
  function pills<T extends string>(
    options: T[], current: T, pick: (value: T) => void,
    label?: (value: T) => string,
  ): HTMLElement {
    return el("div.pill-row", {}, ...options.map((option) =>
      el("button", {
        className: `choice${option === current ? " active" : ""}`,
        onclick: () => { pick(option); refresh(); },
      }, label ? label(option) : option)));
  }

  /** Color swatch pills for hue-based choices (accent, avatar color). */
  function swatches(
    entries: Record<string, number>, currentHue: number,
    pick: (hue: number) => void,
  ): HTMLElement {
    return el("div.pill-row", {}, ...Object.entries(entries).map(([name, hue]) =>
      el("button", {
        className: `choice swatch${hue === currentHue ? " active" : ""}`,
        title: name,
        onclick: () => { pick(hue); refresh(); },
        style: `--swatch-h:${hue}`,
      }, name)));
  }

  /** Redraw the panel (cheap — settings pages are small). */
  function refresh(): void {
    const p = prefs();
    mount(container,
      el("h2", {}, "Settings"),
      el("p.muted", {}, "Make Seymour yours. Everything applies instantly "
        + "and is remembered on this machine."),

      el("h3", {}, "Theme"),
      pills(["dark", "light", "system"], p.theme,
        (theme) => save({ theme })),

      el("h3", {}, "Accent color"),
      el("p.muted", {}, "Colors the interface: buttons, highlights, and the "
        + "scene around Seymour."),
      swatches(ACCENTS, ACCENTS[p.accent] ?? 90, (hue) => {
        // Store the NAME whose hue matched (names read better in storage).
        const name = Object.entries(ACCENTS).find(([, h]) => h === hue)?.[0] ?? "gameboy";
        save({ accent: name });
      }),

      el("h3", {}, "Seymour's color"),
      el("p.muted", {}, "The monster himself. He repaints immediately →"),
      swatches(AVATAR_HUES, p.avatarHue, (avatarHue) => save({ avatarHue })),

      el("h3", {}, "Seymour's area"),
      el("p.muted", {}, "The reserved column where Seymour lives, with the "
        + "slot queue and health checks underneath. Drag its inner edge to "
        + "widen it; drag the frame's bottom edge to resize the screen."),
      pills(["right", "left", "off"], p.avatarSide,
        (avatarSide) => save({ avatarSide })),
      el("h3", {}, "Seymour's screen"),
      el("p.muted", {}, "Match the app's colors, or the classic Game Boy "
        + "green LCD."),
      pills(["theme", "gameboy"], p.avatarScreen,
        (avatarScreen) => save({ avatarScreen })),

      el("h3", {}, "Frame color"),
      swatches(BEZELS, BEZELS[p.bezel] ?? 215, (hue) => {
        const name = Object.entries(BEZELS).find(([, h]) => h === hue)?.[0]
          ?? "console blue";
        save({ bezel: name });
      }),

      el("h3", {}, "Motion"),
      el("p.muted", {}, "Reduced motion freezes the avatar's animation "
        + "frames (state changes still show)."),
      pills(["full", "reduced"], p.motion, (motion) => save({ motion })),

      el("h3", {}, "Tour"),
      el("div.row", {},
        el("button", { onclick: () => replayTour() }, "Replay the welcome tour"),
      ),

      el("h2", {}, "Inference"),
      el("p.muted", {}, "How the model samples and how much it may think. "
        + "These live on the server — every run (chat, agent, research) "
        + "uses them, the composer can override them per message, and each "
        + "run's trace records the values that were actually used."),
      inferenceCard,

      el("h2", {}, "Skills"),
      el("p.muted", {}, "Reusable know-how the model loads on demand: a SKILL.md "
        + "folder in ~/.seymour/skills (yours), the workspace's .seymour/skills "
        + "(untrusted, per project) or bundled with Seymour. Only the one-line "
        + "index rides in the prompt; use_skill loads the rest."),
      skillsCard,

      el("h2", {}, "MCP servers"),
      el("p.muted", {}, "Mount Model Context Protocol servers: their tools "
        + "join Seymour's catalog as mcp__<server>__<tool> and behave like "
        + "run_command — chat asks once per run before the first use. Each "
        + "server is a program Seymour starts and talks to over stdio."),
      mcpCard,
    );
    loadInference();
    loadSkills();
    loadMcp();
  }

  // ---- Skills (server-side, /api/skills) --------------------------------
  const skillsCard = el("div.card", {}, el("p.muted", {}, "loading…"));

  interface SkillRow {
    name: string; description: string; source: string; enabled: boolean;
    files: string[]; chars: number; tools: string[];
  }

  async function loadSkills(): Promise<void> {
    try {
      renderSkills((await get<{ skills: SkillRow[] }>("/api/skills")).skills);
    } catch {
      mount(skillsCard, el("p.muted", {}, "Skills need the server."));
    }
  }

  function renderSkills(rows: SkillRow[]): void {
    const name = el("input", { placeholder: "name (lowercase-with-dashes)", autocomplete: "off" }) as HTMLInputElement;
    const description = el("input", { placeholder: "one-line description (what it is for)", autocomplete: "off" }) as HTMLInputElement;
    description.style.minWidth = "280px";
    const status = el("span.muted", {}, "");
    const create = async () => {
      status.textContent = "creating…";
      try {
        const made = await post<SkillRow & { path: string }>("/api/skills",
          { name: name.value.trim(), description: description.value.trim() });
        status.textContent = `created ${made.name} — edit ${made.path}`;
        loadSkills();
      } catch (error: any) {
        status.textContent = error?.message ?? "failed";
      }
    };
    /** Toggle a skill in or out of the prompt's index. */
    const toggle = async (row: SkillRow) => {
      await post(`/api/skills/${encodeURIComponent(row.name)}/enabled`, { enabled: !row.enabled });
      loadSkills();
    };
    /** Show the body inline (the exact text use_skill hands the model). */
    const view = async (row: SkillRow, into: HTMLElement) => {
      if (into.childElementCount) { into.replaceChildren(); return; }
      const full = await get<{ body: string }>(`/api/skills/${encodeURIComponent(row.name)}`);
      into.replaceChildren(el("pre.skill-body", {}, full.body));
    };
    mount(skillsCard,
      ...(rows.length ? rows.flatMap((row) => {
        const body = el("div");
        return [
          el("div.row", {},
            el("span.badge", { className: `badge ${row.enabled ? "done" : "failed"}` },
               row.enabled ? "on" : "off"),
            el("strong.mcp-name", {}, row.name),
            el("span.badge", { className: `badge source-${row.source}`,
               title: row.source === "workspace"
                 ? "found in the workspace — its text is treated as untrusted"
                 : row.source === "user" ? "your own, in ~/.seymour/skills" : "shipped with Seymour" },
               row.source),
            el("button", { onclick: () => toggle(row) }, row.enabled ? "Disable" : "Enable"),
            el("button", { onclick: () => view(row, body) }, "View"),
          ),
          el("div.muted.mcp-detail", {}, row.description
            + (row.files.length ? ` · ${row.files.length} file${row.files.length > 1 ? "s" : ""}` : "")),
          body,
        ];
      }) : [el("p.muted", {}, "No skills found.")]),
      el("h3", {}, "Create a skill"),
      el("div.row", {}, name, description, el("button.primary", { onclick: create }, "Create"), status),
      el("p.muted", {}, "A new skill lands in ~/.seymour/skills/<name>/SKILL.md with a "
        + "template body — open it in any editor and write the steps."),
    );
  }

  // ---- MCP servers (server-side, /api/mcp) ------------------------------
  const mcpCard = el("div.card", {}, el("p.muted", {}, "loading…"));

  interface McpRow {
    name: string; command: string; args: string[]; connected: boolean;
    tools: string[]; error: string;
  }

  interface McpPreset {
    name: string; command: string; args: string[]; env: Record<string, string>;
    needs: string; adds: string; trust: string; available: boolean;
  }

  async function loadMcp(): Promise<void> {
    try {
      const [servers, presets] = await Promise.all([
        get<{ servers: McpRow[] }>("/api/mcp"),
        get<{ presets: McpPreset[] }>("/api/mcp/presets"),
      ]);
      renderMcp(servers.servers, presets.presets);
    } catch {
      mount(mcpCard, el("p.muted", {}, "MCP settings need the server."));
    }
  }

  function renderMcp(servers: McpRow[], presets: McpPreset[] = []): void {
    const name = el("input", { placeholder: "name (e.g. github)", autocomplete: "off" }) as HTMLInputElement;
    const command = el("input", { placeholder: "command (e.g. npx)", autocomplete: "off" }) as HTMLInputElement;
    const args = el("input", { placeholder: "arguments (space-separated)", autocomplete: "off" }) as HTMLInputElement;
    args.style.minWidth = "260px";
    const status = el("span.muted", {}, "");
    const add = async () => {
      status.textContent = "connecting…";
      try {
        const result = await post<{ connected: boolean; tools: string[]; error: string }>(
          "/api/mcp", { name: name.value.trim(), command: command.value.trim(),
                        args: args.value.trim() ? args.value.trim().split(/\s+/) : [] });
        status.textContent = result.connected
          ? `connected — ${result.tools.length} tools` : `not connected: ${result.error}`;
        loadMcp();
      } catch (error: any) {
        status.textContent = error?.message ?? "failed";
      }
    };
    mount(mcpCard,
      // One server = a header row (state, name, actions) plus a detail
      // line of its own: the detail is a command line or a tool list and
      // can be long, so it wraps anywhere on its own line instead of
      // pushing the buttons off the card (measured on a full path).
      ...(servers.length ? servers.flatMap((s) => [
        el("div.row", {},
          el("span.badge", { className: `badge ${s.connected ? "done" : "failed"}` },
             s.connected ? "connected" : "down"),
          el("strong.mcp-name", {}, s.name),
          el("button", { onclick: async () => { await post(`/api/mcp/${s.name}/reconnect`, {}); loadMcp(); } }, "Reconnect"),
          el("button.danger", { onclick: async () => {
            await fetch(`/api/mcp/${encodeURIComponent(s.name)}`, { method: "DELETE" }); loadMcp();
          } }, "Remove"),
        ),
        el("div.muted.mcp-detail", {}, s.connected
          ? `${s.tools.length} tools: ${s.tools.slice(0, 6).join(", ")}${s.tools.length > 6 ? "…" : ""}`
          : `${s.error ? s.error + " — " : ""}${s.command} ${s.args.join(" ")}`),
      ]) : [el("p.muted", {}, "No servers configured.")]),
      el("h3", {}, "Add a server"),
      el("div.row", {}, name, command, args, el("button.primary", { onclick: add }, "Add"), status),
      el("h3", {}, "Presets"),
      el("p.muted", {}, "One click each. What a preset adds and what it costs in trust "
        + "is in its tooltip; a greyed one needs a launcher (npx or uvx) this "
        + "machine does not have."),
      ...presets.flatMap((preset) => {
        const mounted = servers.some((s) => s.name === preset.name);
        // Same two-line shape as a mounted server: name + action on one
        // row, what it adds (and its trust note) on a detail line.
        return [el("div.row", {},
          el("strong.mcp-name", { title: `${preset.command} ${preset.args.join(" ")}` }, preset.name),
          el("button", {
            disabled: !preset.available || mounted,
            title: mounted ? "already mounted"
              : preset.available ? `Trust: ${preset.trust}` : `needs ${preset.needs}`,
            onclick: async () => {
              status.textContent = `connecting ${preset.name}…`;
              try {
                const result = await post<{ connected: boolean; tools: string[]; error: string }>(
                  "/api/mcp", { name: preset.name, command: preset.command,
                                args: preset.args, env: preset.env });
                status.textContent = result.connected
                  ? `${preset.name}: ${result.tools.length} tools` : `${preset.name}: ${result.error}`;
                loadMcp();
              } catch (error: any) {
                status.textContent = error?.message ?? "failed";
              }
            },
          }, mounted ? "Mounted" : "Add"),
        ),
        el("div.muted.mcp-detail", { title: `Trust: ${preset.trust}` },
           `${preset.adds}. Trust: ${preset.trust}`)];
      }),
    );
  }

  // ---- Inference settings (server-side, /api/inference) -----------------
  /** The card is filled asynchronously — the rest of the page never waits
   *  on a network round trip. */
  const inferenceCard = el("div.card", {}, el("p.muted", {}, "loading…"));

  interface InferenceState {
    settings: Record<string, number | string>;
    defaults: Record<string, number | string>;
    bounds: Record<string, [number, number]>;
  }

  /** Human labels + one-line meaning, in the order they render. */
  const FIELDS: [string, string, string][] = [
    ["temperature", "Temperature", "randomness of sampling (0 = deterministic)"],
    ["top_p", "Top-p", "nucleus sampling: only the smallest set of tokens whose probability sums to p"],
    ["top_k", "Top-k", "only the k most likely tokens (0 = off)"],
    ["min_p", "Min-p", "drop tokens below this fraction of the top token's probability"],
    ["repeat_penalty", "Repeat penalty", "1.0 = off; raise a little if the model repeats itself"],
    ["presence_penalty", "Presence penalty", "0 = off; discourages tokens that already appeared"],
    ["reasoning_budget", "Thinking budget (tokens)", "-1 = unlimited, 0 = none — only when thinking is on"],
    ["max_tokens", "Reply cap (tokens)", "the most a single chat reply may generate"],
    ["history_tokens", "History budget (tokens)", "how much of a conversation rides along; oldest turns drop first"],
  ];

  async function loadInference(): Promise<void> {
    let state: InferenceState;
    try {
      state = await get<InferenceState>("/api/inference");
    } catch {
      mount(inferenceCard, el("p.muted", {}, "Inference settings need the server."));
      return;
    }
    renderInference(state);
  }

  function renderInference(state: InferenceState): void {
    const s = state.settings;
    /** Persist one change; the server clamps and echoes the truth. */
    const change = async (patch: Record<string, number | string>) => {
      renderInference(await post<InferenceState>("/api/inference", patch));
    };
    const numberField = (key: string, label: string, hint: string) => {
      const [lo, hi] = state.bounds[key] ?? [undefined, undefined];
      const input = el("input", {
        value: String(s[key]), title: `${hint}${lo !== undefined ? ` (${lo} – ${hi})` : ""}`,
        autocomplete: "off",
      }) as HTMLInputElement;
      input.type = "number";
      input.step = Number.isInteger(s[key]) && key !== "temperature" ? "1" : "0.05";
      input.style.width = "110px";
      // Saving on change (not on every keystroke) keeps the round trips
      // honest and the field editable.
      input.onchange = () => change({ [key]: Number(input.value) });
      const isDefault = s[key] === state.defaults[key];
      return el("div.row", {},
        el("span.grow", {}, label,
          isDefault ? "" : el("span.muted", {}, `  (default ${state.defaults[key]})`)),
        input);
    };
    mount(inferenceCard,
      el("h3", {}, "Thinking"),
      el("p.muted", {}, "auto = each kind of run keeps its measured default "
        + "(chat answers directly, agent steps think). on/off overrides both."),
      el("div.pill-row", {}, ...(["auto", "on", "off"] as const).map((v) =>
        el("button", {
          className: `choice${s.thinking === v ? " active" : ""}`,
          onclick: () => change({ thinking: v }),
        }, v))),
      el("h3", {}, "Sampling"),
      ...FIELDS.map(([key, label, hint]) => numberField(key, label, hint)),
      el("div.row", {},
        el("span.grow.muted", {}, "Defaults follow Qwen's model card "
          + "(top-p 0.8, top-k 20, min-p 0)."),
        el("button", {
          onclick: async () => renderInference(
            await post<InferenceState>("/api/inference/reset", {})),
        }, "Reset to defaults")),
    );
  }

  refresh();
  // No bus subscriptions — nothing to tear down.
  return { destroy() { /* nothing subscribed */ } };
}
