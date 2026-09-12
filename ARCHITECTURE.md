# Seymour — Architecture

*The map of the system. Read this first; re-read it in six months.*

Seymour is a local-first AI workspace for one person on one laptop: a
background agent and a human sharing **one model** without taking turns. It is
a personal fork-in-spirit of [Odysseus](https://github.com/odysseus-dev/odysseus)
(AGPL-3.0), rebuilt around one idea Odysseus cannot use: *a tool that must run
on every backend has to assume the weakest one; a tool that ships with a known
backend can exploit it.* Seymour ships with llama.cpp, so it can use the
parallel slots, continuous batching, and prompt caching that llama-server
actually provides.

## The whole thing on one page

```
   BROWSER (static/ — TypeScript, no framework)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Chat │ Agent │ Research │ Models │ Memory │ Soul │ Onboarding   │
   │  ┌─ Avatar (pixel Seymour, drawn from REAL scheduler state) ─┐   │
   │  └─ Mode banner ("Concurrent · 4 slots · agent 1, chat 1") ──┘   │
   └────────────────┬─────────────────────────────┬──────────────────┘
                    │ fetch + SSE (/api/…)        │ SSE (/api/events)
   ┌────────────────▼─────────────────────────────▼──────────────────┐
   │                SEYMOUR CORE (Python, FastAPI)                    │
   │                                                                  │
   │  routes/   chat · agent · research · models · memory · soul ·    │
   │            status · events · onboarding    (thin: no logic)      │
   │                                                                  │
   │  agent/    the primary agent: loop · tools · power  (TIER 3)     │
   │  research/ deep-research pipeline            (TIER 2)            │
   │  chat      the human's live stream           (TIER 1)            │
   │                     │                                            │
   │  ╔══════════════════▼════════════════════════════════════════╗   │
   │  ║  scheduler/ — tag → admit → grant → account → preempt     ║   │
   │  ║  three tiers, agent floor, two modes (concurrent/serial)  ║   │
   │  ╚══════════════════╪════════════════════════════════════════╝   │
   │                     │ chosen at startup by                       │
   │            ┌────────▼──────────┐                                 │
   │            │ engine/handshake  │  4 probes: slots? context?      │
   │            └────────┬──────────┘  caching? overlap?              │
   │  ┌──────────────────▼──────────────────────────────────────┐    │
   │  │ engine/ — EngineAdapter: the ONLY code that knows what   │    │
   │  │ an engine is (llamacpp.py real · fake.py for tests)      │    │
   │  └──────────────────┬──────────────────────────────────────┘    │
   │                     │                                            │
   │  memory/ (RAG) · persona/ (soul) · models_manager/ (HF) ·        │
   │  events.py (bus) · db.py (SQLite) · config.py                    │
   └─────────────────────┼────────────────────────────────────────────┘
                         │ OpenAI-compatible HTTP, localhost only
   ┌─────────────────────▼────────────────────────────────────────────┐
   │  llama-server (child process)                                    │
   │  --parallel 4 --kv-unified --cache-prompt --cache-type-k q8_0    │
   │  [slot 0][slot 1][slot 2][slot 3]   shared KV pool               │
   │  ONE COPY OF THE WEIGHTS (e.g. Qwen3.6-35B-A3B Q8_0, ~35 GB)     │
   └──────────────────────────────────────────────────────────────────┘
```

llama-server provides the **mechanism** — slots, batching, a shared KV pool,
prompt caching. Seymour Core provides the **policy**. That division is the
whole design.

## The six decisions

1. **One model instance, priority classes — not two instances.** Two
   instances would double the weights (~70 GB), split bandwidth permanently,
   and waste whichever side is idle. One instance shares weights, lends idle
   capacity to whoever is active, and batches both onto the same forward
   passes.

2. **Three priority tiers, not two.** Priority keys on *"is a human watching
   tokens appear right now?"* — not on who started the job:
   - **Tier 1 — live chat.** Always wins; preempts a lower tier if no slot
     is free. Never itself preempted.
   - **Tier 2 — foreground autonomous** (deep research). Yields to chat,
     outranks the agent, capped so a fan-out can't take the machine.
   - **Tier 3 — the primary agent.** Lowest priority AND a guaranteed
     floor: at least one slot and a minimum share of recent decode tokens.
     Below the line, its next request is promoted (it may bump research,
     never live chat).

3. **Two operating modes, chosen by measurement, shown honestly.**
   *Concurrent* (batching confirmed) runs the full design. *Serial*
   (batching absent/unconvincing) is Odysseus's design — one request at a
   time, foreground cancels background — and it is a supported
   configuration, not a failure state. The UI always says which mode is
   live and what it means.

4. **The startup handshake.** Flags are requests, not facts. At boot Seymour
   probes the running server: how many slots exist (`/props`), the real
   per-slot context (`/slots` — catches static partitioning silently
   dividing your context), whether prompt caching measurably works
   (repeat-prompt TTFT), and whether concurrent requests genuinely overlap
   (a measured speedup ratio). Only then is the mode chosen.

5. **The scheduler sits above the HTTP boundary.** llama-server's own
   scheduler decides which admitted sequences advance each forward pass;
   Seymour's decides what is admitted, how many slots each tier holds,
   which slot a conversation sticks to (cache affinity), and when to cancel
   a stream. Forking the engine would buy exact control at the price of
   maintaining a C++ inference engine forever.

6. **The engine adapter is the seam.** `engine/adapter.py` defines the
   interface; `llamacpp.py` implements it; `fake.py` makes the scheduler
   testable in milliseconds. Two rules keep it honest: no model/engine name
   above the adapter, and capabilities are measured once then frozen.

## What lives where

| Path | Responsibility | Key ideas borrowed (credited in ACKNOWLEDGMENTS.md) |
|---|---|---|
| `seymour/config.py` | every environment-specific value | nothing else reads `os.environ` |
| `seymour/db.py` | the whole schema, one readable file | tables exist only for shipped features |
| `seymour/events.py` | in-process bus → `/api/events` SSE | honesty layer's plumbing; avatar reads it |
| `seymour/engine/` | llama-server child process, streaming, slot affinity (a live slot is never shared; cache reuse only for the conversation that built it), handshake | Odysseus's capability readers + slot-affinity fix; the 2026-09-02 wedge investigation (HARNESS.md) |
| `seymour/engine/mlx.py` | the SECOND engine: an mlx-lm (batching + prompt cache) or mlx-vlm (MTP drafter) child on Apple Silicon, behind the same adapter, handshake and scheduler; the weights' kind (folder vs .gguf) picks the engine | mlx-lm / mlx-vlm servers; the seams map of 2026-09-03 |
| `seymour/engine/mlxinfo.py` | an MLX checkpoint's own config.json read as facts: quant, hybrid-layer KV cost per token, drafter pairing by config (never by name) | the GGUF header reader's discipline, applied to a folder |
| `seymour/skills.py` | skills: SKILL.md folders (bundled / ~/.seymour/skills / workspace), a one-line index in the prompt, bodies loaded by use_skill | the Agent Skills convention (Claude Code, oh-my-pi, dsh) |
| `seymour/scheduler/` | tiers, floor accounting, preemption, mode policy | serial mode = Odysseus's contention gate, reimplemented |
| `seymour/agent/` | task lifecycle, the ~60-line step loop, battery awareness | Odysseus loop-breakers |
| `seymour/tools/` | THE tool registry, shared by chat runs and agents: workspace files (line-addressed reads, tag-proven edits), sandboxed `run_command`, guarded web search/fetch, memory | omp hashline + tiers; dsh pipeline/bash semantics; Odysseus fetch/edit/grep (see HARNESS.md) |
| `seymour/engine/guard.py` | the child guard: every engine server runs under a watchdog that stops it when the app dies without `stop()` (macOS has no PR_SET_PDEATHSIG) | three measured orphans, 2026-09-02/03 |
| `seymour/inference.py` | sampling + thinking settings (server-side, per-run overrides, traced) | Qwen model-card defaults |
| `seymour/mcp.py` | MCP client: external servers' tools mounted as `mcp__<server>__<tool>`; verified one-click presets (playwright, filesystem, fetch, git, memory) | dsh's mcp-client plugin |
| `seymour/run_executor.py` | the one chat run executor: per-message policy, event log, in-band call parsing, approval gate, watchdogs | dsh's loop invariants |
| `seymour/research.py` | deep research pipeline (tier 2) | Odysseus DeepResearcher stage machine, simplified |
| `seymour/memory/` | one system of record (SQLite), embeddings, hybrid retrieval | Odysseus hybrid scoring; their dual-store mistake avoided |
| `seymour/persona/` | the soul file: Markdown on disk, user-editable | server-side (their localStorage personas were invisible to the server) |
| `seymour/models_manager/` | local registry + HF search/download | HF hub cache layout (never `--local-dir`), sentinel-free resume |
| `seymour/prompts/` | every prompt as a static file, never string-built in code | omp's prompt discipline |
| `seymour/routes/` | thin HTTP surface, one module per feature | ~10 routers vs Odysseus's ~46 (single user) |
| `seymour/app.py` | wiring only — startup order: db → engine → handshake → scheduler → agent | the shell contains no logic |
| `frontend/` | TypeScript sources (esbuild → `static/app.js`) | textContent-only rendering (XSS), SSE frame buffering |
| `frontend/src/theme.ts` | user prefs (dark/light, accent, avatar hue, orientation, motion) → CSS variables + body classes, persisted in localStorage | applies instantly; the avatar subscribes for repaints |
| `frontend/src/icons.ts` | the pixel icon set: 16×16 text grids rendered as crisp SVG rects | inherits currentColor, so the theme recolors it |
| `frontend/src/avatar/scene.ts` | procedural pixel scenes (desk+CRT, tea break, sleeping…) on a low-res canvas | tintable, animatable, recomposes for landscape/portrait |
| `frontend/src/main.ts` | the view LIFECYCLE: switching a view destroys the old one (bus unsubscribes + stream aborts) | the structural fix for "background view hijacks the screen" |
| `static/` | what the browser actually loads (incl. the OFL pixel font, served locally) | |
| `tests/` | scheduler + agent-manager proofs against fakes | deterministic, no model needed |

## Cross-cutting rules

- **KV-cache friendliness.** The system prompt is byte-identical across the
  turns of a session (soul + tool text, nothing dynamic). Anything that
  changes turn-to-turn — retrieved memories, the date, task state — rides in
  a separate message near the end. This is what keeps llama.cpp's
  slot-continuation cache ("only the unseen suffix is evaluated") effective.
- **Untrusted content never enters the system role.** Retrieved memories,
  web pages, and search results are wrapped in guard-marked user messages
  (`<<<UNTRUSTED_SOURCE_DATA>>>`), with the markers escaped inside the
  payload.
- **Model output is rendered with `textContent`, never `innerHTML`.**
- **Loopback only.** Both servers bind 127.0.0.1. Nothing leaves the machine
  except what the user's own tools fetch (web search) and HF downloads.
- **Every "the model might never stop" path has a bounded counter** with a
  comment stating its reset condition.
- **Partial work is always persisted** — the user's message before streaming
  starts, the partial reply in a `finally`, the agent's notes after every
  step, a research report's evolving draft on timeout.

## Startup sequence

1. `init_db()` — create missing tables (idempotent).
2. Engine start — launch llama-server, poll `/health` with a dead-child
   check (an OOM death becomes an immediate clear error, not a 10-minute
   timeout).
3. **Handshake** — run the four probes, freeze `EngineCapabilities`.
4. Scheduler — `choose_policy(caps)` picks concurrent or serial; publish it.
5. Agent manager — resume any task that was `running` when the app last
   closed (its notes and journal are its checkpoint).
6. Serve the UI. First launch shows the onboarding tour (a one-time
   `app_state` flag).

Shutdown mirrors it: agent checkpoints, llama-server gets SIGTERM (then
SIGKILL after 10 s) so quitting reclaims the 35 GB instead of orphaning it.
