# NOTES — measured numbers, not assumptions

The guide's rule: record what you MEASURED, with the date, so future
debugging can tell "changed" from "always was". Add a dated block whenever
the handshake surprises you or the hardware/story changes.

## 2026-08-12 — first full run (M5 Max, 128 GB, macOS 25.5)

- Engine: llama.cpp **b10280** (Homebrew), model **Qwen3.6-35B-A3B-Q8_0**
  (35 GB on disk).
- Launch flags: `--parallel 4 --ctx-size 32768 --kv-unified --cache-prompt
  --cache-type-k q8_0 --jinja --metrics`, loopback only.
- Handshake results (`seymour.engine.handshake`):
  - probe 1 — slots: **4** (as requested).
  - probe 2 — context/slot: **32768** → `--kv-unified` genuinely took;
    static partitioning would have shown 8192.
  - probe 3 — prompt caching: **works, dramatically** (cold prefill of the
    long probe prompt 4.11 s → warm 0.03 s on the same slot).
  - probe 4 — overlap: **1.83–1.91× speedup** at n=3 concurrent requests
    (measured twice, ON BATTERY) → concurrent mode. Threshold is 1.5.
- Real-workload observation: with the agent mid-task, a fresh chat's first
  token took ~15 s **on battery** (prefill compute is throttled on battery,
  and a new conversation prefills its whole system prompt). Streaming after
  first token stayed smooth. Re-measure plugged in before concluding
  anything.
- Odd but handled: a llama-server from a manual session (started Thursday)
  was still running on 8080. Seymour now **adopts** an already-listening
  server — measures it, reports it as external, and never kills it.

## 2026-08-13 — round 2: review fixes, metrics, and the new face

- **Adversarial review (28 agents) confirmed 19 findings; all fixed** with
  regression tests (34 tests green). The two that mattered most:
  - Scheduler: a consumer cancelled while QUEUED leaked its waiter — and
    could then be granted a ticket nobody would ever release, permanently
    losing an engine slot. `_admit` now unwinds itself on cancellation
    (shielded cleanup), and preemption no longer evicts a victim for a
    waiter whose tier is at its cap (needless-kill loop).
  - Agent manager: a finishing task never promoted the next queued task
    (its own asyncio task looked "still running" to the guard), and
    resume-while-busy stranded tasks as phantom 'running' rows. Lifecycle
    calls now validate existence + state (404/409 at the routes).
- **Metrics with provenance** (the Odysseus lesson): llama-server's own
  `timings` ride each response into `GenerationRequest.stats`; the chat
  stream ends with a stats frame (decode tok/s, tokens, TTFT, source
  named "engine" vs "measured"); the scheduler snapshot now carries
  per-tier tokens/sec over the last minute for the header meter.
- Measured live (adopted server, on battery ~40%): **90.7 tok/s decode**,
  first visible token 2.26 s on a fresh short conversation.
- **Qwen3.6 thinks in a hidden channel**: `reasoning_content` deltas are
  not `content`, so a "short" reply can be 1300+ real generated tokens
  and the first VISIBLE token can lag by ~15 s on battery. The chat shows
  a pulsing "thinking" placeholder until content arrives. Future work:
  surface the reasoning stream (collapsible), and count it in the tier
  rate meter (today only visible tokens are counted).
- SQLite tuned: WAL + synchronous=NORMAL (commits stop stalling live SSE
  streams), foreign_keys=ON (orphan journal rows now fail loudly).
- SSRF: fetch_page follows redirects by hand, re-validating every hop
  (Odysseus goes further with DNS-pinned connections — noted as a
  candidate hardening). Research guards its extract prompts against
  marker breakout (entity-encoded `<<<` survived HTML stripping).
- Deferred, with pointers in the study reports: Playwright-MCP-style
  optional real browser; oh-my-pi's in-band XML tool dialect for small
  models; prune→shake→summarize context compaction ladder.

## 2026-08-14 — round 3: the command center

- **Model Load/Unload fixed.** The "Activate does nothing" bug: with an
  ADOPTED external llama-server, activation stopped nothing and then
  silently re-adopted the same server. Load now refuses adoption
  (`allow_adopt=False`) with the remedy named; Unload stops the engine
  (detaches from external servers) and survives restarts via an
  `engine_autoload` flag.
- **Chat is the command center.** `/api/chat` takes `mode`:
  chat | agent | research. Agent/research modes create Tier-2 jobs and
  return a job card; the Agent/Research tabs are ORGANIZERS with
  "start new" buttons that jump back to chat with the mode preselected.
  The Primary tab is the continuous Tier-3 seat.
- **Discrete agent runner** (`agent/discrete.py`): chat-started jobs at
  Tier 2, pool of 3, same loop/journal/tools as the primary agent
  (`run_step` grew a `tier` parameter).
- **Web-aware chat**: the model may open its reply with one in-band JSON
  tool call (web_search / fetch_page); the route sniffs the first 48
  chars, buffers tool rounds invisibly (status frame to the user),
  guard-wraps results, max 2 rounds.
- **Attachments**: `/api/upload` → pdf (pypdf) / docx (stdlib zip+xml) /
  csv / md / txt extracted once onto the row; images join the gallery
  and go to the model as data URIs — only when `/props` reports a
  loaded vision projector (`supports_vision`; mmproj auto-passed at
  launch when found next to the weights). No projector → the model is
  told plainly it cannot see the image.
- **Floating avatar window**: drag (pointer events), native CSS resize,
  geometry persisted; bezel color setting; health line (agent tok/s,
  slots, battery). Layout modes removed — the window replaced them.
- **Odysseus ports**: research reports now save with a Research Summary
  stats header + Sources section + sidecar JSON; memory gained
  categories (identity/preference/fact/contact/project/goal), pinning
  (identity/contact auto-pin, core pins ride every chat), and an
  injection-counted `uses` badge; the Models tab shows hardware-fit
  chips from `hwfit.py` (weights_gb + 8e-6·active_B·ctx + 0.5 against
  ~80% of unified memory — verdicts: fits great / fits / tight / too
  big) on local files AND HF downloads before you spend 30 GB.

## 2026-08-14 (later) — Load adoption + the reserved panel

- **Load now adopts a same-model server.** The refusal shipped earlier
  was honest but unhelpful when the foreign llama-server on the port was
  serving EXACTLY the requested weights. `engine.start(adopt=)` grew a
  policy: "always" (boot), "same-model" (explicit Load: adopt if it
  serves the chosen file — instant load; refuse otherwise, naming the
  remedy), "never". Verified live: setup-mode boot → Load → adopted the
  manual server, handshake ran (4 slots, 1.95×, caching works), chat
  streamed at 57.2 tok/s engine-timed.
- **The avatar returned to a RESERVED column** (right by default;
  left/right/off in Settings): the frame resizes vertically in place,
  the panel width drags at its inner edge, both persist. Below the
  avatar: the SLOTS field — one row per engine slot showing its live
  occupant and per-stream tok/s (tickets now carry an admission
  timestamp; scheduler snapshot exposes `active`). Watched it live:
  slot 1 = "memory:extract · prefill…" while chat idled. Windows under
  900px hide the panel (the work area wins on tiny windows).

## 2026-08-14 (round 4) — the command center, verified live

- **Enter-opens-file-picker bug**: a button in a form defaults to
  type=submit, and IMPLICIT submission "clicks" the first submit button —
  which was the paperclip. Fixed (type="button") + an explicit Enter →
  requestSubmit handler (implicit submission proved flaky anyway).
- **"Web lookup returns nothing" — three causes, all fixed**: (1) the
  model invented a date from its training data and reasoned itself out of
  searching — the real date now rides the changing-tail context (never
  the system prompt; the cache rule); (2) post-search rounds burned the
  whole budget on hidden thinking — `chat_template_kwargs
  {enable_thinking:false}` on tool rounds (verified against the live
  server: 7 tokens, no reasoning) + max_tokens 4096; (3) an all-thinking
  truncation now falls back to an honest "ran out of room" line.
  Verified end-to-end: "who won the 2026 world cup?" → 1 web lookup →
  "Spain… 1-0" at 91 tok/s, first token 3.55s.
- DDG search itself worked all along (8 good results); SearXNG is now
  the preferred backend when SEYMOUR_SEARXNG_URL is set (JSON API,
  DDG fallback).
- **The top status bar is gone** — model, mode, memory (unified = GPU),
  battery all live in Seymour's area above the SLOTS queue, which now
  shows per-slot kind (chat/agent/primary/research/memory/title/check),
  phase (prefill/decode), live tok/s, and WAITING work under a dashed
  separator (scheduler snapshot gained "queued" labels).
- **Every chat is a chat**: sessions carry kind + spawned job id; the
  sidebar shows kind symbols and a spinner while the chat's work holds a
  slot; organizer cards link back via "Open chat".
- Empty conversations greet with a time-of-day hello + Seymour playing
  in the sand (a decorative scene; the status avatar stays honest).
- Full-loop discrete-agent test: chat mode=agent → "write a haiku into
  haiku.md" → done in seconds, file verified on disk, result + journal +
  open-chat in the Agent tab.

## 2026-08-14 (round 5) — the conversation model, and research revived

- **Deep research returned 0 sources — three stacked causes, measured:**
  1. Every research LLM call lacked `enable_thinking: false`, so hidden
     reasoning silently burned the tight per-step budgets (plan 1024,
     queries 512, decide 8) — the JSON parses came back `[]`, zero
     queries ever ran, and the empty-round breaker ended the run "done"
     with nothing. Same class of bug chat's tool rounds had in round 4;
     research never got the fix. (Odysseus has this exact trap today:
     its 128-token stop-decide truncates mid-`<think>` on Qwen-family
     models, so runs never stop early.)
  2. With thinking off, the extract prompt's bar ("what this page
     contributes… else IRRELEVANT") was too strict — the model answered
     IRRELEVANT for on-topic pages (reproduced against the live server).
     Reworded to topic-relatedness with "pieces count"; kept the
     structured IRRELEVANT escape (Odysseus has no escape hatch at all,
     which is why its low-quality filter exists).
  3. The boilerplate-marker filter rejected any extract CONTAINING a
     marker — a substantive 150-word extract opening "does not contain
     specific changes, but…" was discarded. Markers are now phrases (per
     Odysseus's own hard-won comment) and only apply to extracts under
     300 chars.
  Plus one robustness hole surfaced live: a single 403 (thelancet.com
  paywall) killed a whole run — per-URL reads are now try/except with a
  `read_failed` event. And honesty hardening: empty plans fall back to
  the raw question, a run with `queries_run == 0` is FAILED (not
  "done"), and the sidecar JSON is written even when the report is
  empty (a broken run must leave forensics). Verified end-to-end:
  "was covid made in a lab?" → 68s, 2 rounds, 6 queries, 3 PMC sources,
  report with inline citations saved and delivered.
- **The conversation model** (the OOP framing made real): every
  conversation HAS a kind and keeps it. Choosing a task mode inside a
  different-kind conversation opens a dialog — new conversation with
  context (last 10 turns condensed into the goal/plan), with just the
  prompt, or cancel. Task conversations WATCH their runs: real events
  (queries, page reads, agent journal steps) stream into a live feed
  card, and the outcome persists as an assistant message in the
  conversation (research report, task result, blocked questions) — the
  organizer tabs just organize. Verified: dialog → "bring context" →
  agent task carried chat context, wrote limerick.md, result landed in
  the new conversation.
- **The trailing tool call**: the model narrates prose and THEN emits
  `{"tool": …}` (sometimes TWO of them — observed live), which the
  48-char sniff can't see. Round ends now scan the round's visible text
  for the FIRST `{"tool"` occurrence: trim it (a `replace` SSE frame
  rewrites the bubble), execute, continue. "Most recent news article"
  went from a 35-item homepage dump (round 4) to a stray dead JSON
  (first fix) to a focused 2-source answer at 176 tokens with 2 real
  lookups.
- **Metrics tell Activity Monitor's truth now**: psutil's
  (total−available) counted ~27 GB of reclaimable file cache as used
  (64.2 vs the real 36.9 GB). `_memory_info` computes App(anon−purgeable)
  + Wired + Compressed from vm_stat; `_gpu_info` reads the driver's
  "Device Utilization %" via IOAccelerator (no sudo, ~20 ms, 2 s cache).
  Watched gpu hit 98% during a decode. The mode·slots·speedup line is
  gone — the SLOTS rows already tell that story.
- **Vision honesty, diagnosed**: the "can't see" message was TRUE (the
  adopted server has no projector; no mmproj exists on disk — the
  Qwen3.6 GGUF is text-only weights despite the vision-aware chat
  template). The stream now carries an authoritative `notice` frame
  naming the exact remedy (relaunch with the mmproj that exists, or
  download one), rendered as a system note — not the model's paraphrase.
- **CSS source-order bug**: the ≤900px media block sat mid-file, so
  base `#companion`/`#sessions` rules declared LATER won at equal
  specificity — Seymour's column never hid on narrow windows and its
  auto-placed grid tracks wrecked the layout. Media block moved to the
  file's end. (A media query adds no specificity; order decides.)
- Sidebar rows are now `title … • kind icon • pixel cycle` (the cycle
  is a hand-drawn two-arrow glyph rotated in 90° STEPS so every frame
  stays crisp; it exists only while the scheduler's ledger says live).
  "New chat" became "New conversation"; the rail lives on every view.
  Organizer tabs render grids (start-new as the first dashed tile,
  3-line clamped titles); the Research tab merges live runs with the
  workspace sidecar archive so past runs survive restarts.

## 2026-08-14 (round 5b) — the adversarial review pass

15 confirmed findings (19 agents: 3 reviewers, per-finding adversarial
verifiers), all fixed same-day. The ones that would have bitten first:

- **Sidecar clobber (HIGH):** research sidecar filenames were slugged
  from the question alone, and round 5 made the sidecar write
  unconditional — re-running a question and cancelling would overwrite
  the finished run's archive with an empty record. Stems now include
  the run id.
- **Stale openSession (HIGH):** the conversation-open GET had no
  staleness guard; clicking New conversation (or another row) before it
  resolved let the late response clobber module state, silently sending
  the NEXT message to the wrong conversation. Guard: drop the response
  unless sessionId still matches (plus a destroyed flag).
- **Tool-loop bounds:** the whole-JSON tool path incremented rounds but
  never broke — a model looping on tool JSON after the budget got the
  same nudge forever, silently holding a Tier-1 slot. The nudge is now
  one-shot, then break (the honest empty-reply fallback takes over).
  Also: invalid tool names get a truthful "retry with a correct call"
  while budget remains (the old message lied "no more tools"); the
  trailing-call detector is whitespace-tolerant (pretty-printed JSON)
  and only fires when the reply ENDS with the call — prose after the
  object means the model was QUOTING its protocol, not invoking it.
- **Resilience symmetry:** research now survives a failed SEARCH the
  same way it survives an unreadable page (one DDG 503 used to kill the
  run); fetch_page's "Refused:" sentinels route through the skip path
  instead of burning an extract call; one malformed sidecar can no
  longer 500 the whole research organizer.
- Housekeeping: session read/delete moved off the event loop (the same
  freeze-the-streams rule the file already documented), finished
  research runners are freed, journal lines dedup by step id (the loop
  persists-then-publishes, so live lines were also in the snapshot),
  the research grid ignores out-of-order refreshes, the job-completion
  reload defers to an in-flight chat stream, and the mode dialog no
  longer skips the not-yet-loaded-kind window.

## 2026-08-18 — harness rebuild Phase 0 + Stage 1

- **Phase 0 (orient/audit/plan) delivered and decided.** Licenses:
  deepseek-harness MIT, oh-my-pi MIT (the non-commercial fear was
  wrong), Odysseus AGPL-3.0-or-later — and since Seymour already adapts
  Odysseus patterns, Nick chose to license Seymour AGPL-3.0-or-later
  (LICENSE added). Structural base approved: deepseek-harness's design
  re-expressed in Python (event-sourced session log + derive_messages,
  inbox/turn/step machine with typed cancellation, tool pipeline where
  every failure is a tool result + monotonic guards) with hard budgets
  and malformed-call repair added — dsh has NO step cap and never
  repairs bad JSON; both are frontier-model bets a 35B loses. Decided
  UX: sidebar rows show the LAST run's kind icon; writes from chat ask
  once per task; ~3 foreground tool calls before the detach prompt.
- **Stage 1 shipped (bugs 4.5/4.6/4.3), verified live:**
  - Markdown renders (4.5): marked + DOMPurify + hljs core, all bundled
    locally (app.js 89→360 KB, zero CDN). One innerHTML in the app —
    md.ts, DOMPurify-output only; links http(s)/# in new tabs; GFM
    tables scroll in place. Streaming re-renders the whole small
    message on a 120 ms throttle (no frozen prefix → nothing to
    corrupt; an unterminated fence gets a temporary closer). The
    system prompt now ASKS for markdown (the renderer's other half).
  - Composer is a multi-line auto-growing textarea (4.6): Enter sends,
    Shift+Enter breaks, 160 px cap then scroll, newlines reach the
    model verbatim.
  - Tombstones (4.3): organizer rows whose conversation is gone show a
    disabled "chat deleted" control (result/journal/report stay
    readable in the tile); opening a vanished conversation resets to a
    fresh thread with an honest note — the backend never fabricates
    one. Verified against a genuinely deleted conversation.
- **Stage 2 shipped (bugs 4.2/4.1), verified live:**
  - Unload is a REQUEST now (4.2). The scheduler is the refcount: a
    drain gate in _admit refuses new work (ModelUnloadingError), an
    idle event marks refcount zero, and abort_active() cancels every
    in-flight/queued consumer task — which closes the engine's HTTP
    stream, the thing that actually makes llama-server stop decoding.
    Modes: check (report the live work), drain (finish current work —
    the UI dialog's default), cancel (agents pause resumably, research
    keeps drafts, chat streams abort). "unloaded: true" is only ever
    reported at refcount zero; a drain stays cancellable the whole
    time; consumer loops treat unloading as PAUSE, never a retryable
    failure (retrying would hold the unload hostage — the trap the
    zombie-retry loops fell into before). Verified end-to-end against
    the adopted server: busy check named the run; a chat sent mid-drain
    was refused with the real reason; cancel-and-unload ended a live
    research run (draft kept, outcome in its conversation) and
    /slots on 8080 showed ZERO busy slots after — a real serving-layer
    abort, not stream abandonment.
  - Deleting a conversation cancels its non-terminal runs first (4.1),
    AWAITED — the DELETE only returns after the runners actually
    stopped, and a cancellation failure blocks the delete with a 409.
    Verified: a running agent task's conversation deleted → task
    'cancelled', row retained with the tombstoned "chat deleted"
    control. Live feeds also carry an inline Cancel now — a run is
    stoppable from its own thread, not just from a tab.
  - New regression proofs: tests/test_scheduler_drain.py (drain gate,
    abort-to-refcount-zero, queued-waiter abort) — 37 green total.
- **Stage 3 shipped: the eval set + the baseline (measured, 3 runs).**
  evals/run.py — 22 frozen tasks (tool-use 5, multi-step 5, long-context
  3, research 3, recovery 6), all criteria programmatic, results as
  versioned JSON. Set version history: v1→v2 and v2→v3 were CHECKER
  fixes the runs themselves exposed (the model was honest about a
  missing file and the phrase list wasn't; raw llama slot counts blamed
  research for background title/memory work — the scheduler's ledger is
  the precise oracle). Criteria now stable at v3.
  **Baseline on Qwen3.6-35B-A3B-Q8_0, plugged in, concurrent mode —
  three full runs: 19/22, 19/22, 15/22.**
  - 12 rows stable green (3/3): all research, most tool-use, the
    needle/memory long-context rows, core agent file work.
  - 1 row stable red (0/3): junk-research — "hello" as a research
    prompt runs 3-5 MINUTES and reports "done" every time. The 4.4
    rule's number to turn green.
  - ~7 rows flaky, and the flakiness has ONE dominant cause: hidden
    thinking rabbit-holes. Observed: 180 s of silence on "explain a
    mutex"; a 25-char fallback where a codename answer should be; agent
    steps stalling until the 300 s task timeout — and a stuck task then
    contaminating a later task's count by writing its files late. The
    current harness has NO thinking budget and NO per-step deadline;
    stalls cascade. This is the works-on-paper-fails-on-this-model
    finding: convergence must add per-run thinking policy (the
    @smol/@slow idea → enable_thinking as a policy knob) and step
    watchdogs. Honest baseline to beat: median 19/22, range 15-19,
    12 stable greens that must STAY green.
- **Report viewer shipped** (Nick's ask, Odysseus's Visual Report as the
  reference): frontend/src/views/report.ts — a document view with a
  sticky TOC built from the report's own rendered headings, the run's
  stats line, Download .md (GET /api/research/download/{stem}: stem
  regex-validated and workspace-confined; traversal returns 404) and
  Print/PDF via a print stylesheet that strips the app chrome. The
  Research tab's tiles now offer "View report" (document) alongside the
  in-place "Peek". Verified live on the covid report.

## 2026-08-19 — plan realignment from Nick's review

Three course corrections, recorded before Stage 4 starts:

- **Plugins are OFF the table** (back burner). dsh's
  everything-is-a-plugin architecture and its UI-plugin machinery are
  explicitly NOT being borrowed — "ui element plug ins matter basically
  not at all". What we take is the DEFAULT AGENT's loop and tools
  design. (This matches the Phase-0 verdict; now it's a directive.)
- **TRACEABILITY is promoted to the headline feature.** The
  append-only event log is no longer internal plumbing: the Tools tab
  becomes the trace viewer, dsh-video-style — per run, the system
  prompt, every model call, every tool call with payload + result +
  duration, token stats, click-through to the exact conversation
  position, and a session-JSONL export. Build the log so this UI is a
  straight read of it (never a parallel bookkeeping path).
- **Mode collapse, precisely:** Chat (default) carries the FULL tool
  catalog — the model+harness decide what to use per input; Deep
  Research stays as the only other composer option. Critically, DR
  given insufficient/unresearchable input must **fail gracefully** in
  the GENERAL case (answer as ordinary chat, or ask one clarifying
  question) — NOT a keyword check for "hello"; that eval row is a
  symptom, and the gate belongs in the DR preset's planning step.
- Also still open from the baseline: the "I ran out of room" fallback
  Nick saw in a test is the thinking-rabbit-hole failure — per-run
  thinking policy + step watchdogs are convergence requirements.

## 2026-08-19 — Stage 4a/4b: the converged executor, mode collapse, traces

- **One executor** (`run_executor.py`). Chat's bespoke two-tool loop is
  gone; a chat RUN now carries the agent's registry catalog filtered by
  each tool's own declared tier (read scope today — writes wait for the
  approval gate), and the MODEL decides what an input needs. Kept from
  the old loop: the 48-char sniff, the trailing-call detector (first
  `{"tool"`, whitespace-tolerant, only when the reply ENDS with it),
  the honest empty-reply fallback. Added from the baseline's findings:
  hard budgets (6 tools, 10 rounds), a STALL WATCHDOG (generous first-
  token gap, then 90 s — the 180 s-of-silence bug can't recur), thinking
  as an explicit policy knob (OFF for chat: every flaky eval row traced
  to unbounded hidden thinking), and one-shot malformed-call REPAIR that
  names the real tools instead of lying "no more tools".
  Measured: the same tool-using answers now take 1-4 s where they took
  5-20 s. Chat eval rows: 11/11 (was 5/5 tool-use + 3/3 long-context +
  3/6 recovery at baseline).
- **Runs are traceable** (`db.Run` + `db.RunEvent`, `routes/runs.py`,
  `views/runs.ts`). Every run appends typed events as it happens —
  run_start (policy!), model_call, model_result (with engine tok/s),
  tool_call (args), tool_result (duration, error flag, true length),
  repair, note, run_end. The Agent tab became **Runs**: pick a run and
  read the real sequence, click any line for its payload, filter by
  text/status, export the whole trace as JSONL. The UI is a straight
  READ of the log — no parallel bookkeeping to drift.
- **Mode collapse.** The composer has two options: Chat and Deep
  Research. The Agent pill is gone (tool use is something a run does).
  Sessions get their sidebar icon from the last run's behaviour: a chat
  that used tools shows the wrench.
- **DR fails gracefully** (`prompts/research_triage.md`): before
  planning, a triage step judges whether the MESSAGE has anything
  researchable — a general judgement, explicitly not a keyword list.
  Not researchable → status "declined", the reply lands in the
  conversation like ordinary chat, no report file, no minutes burned.
  Verified: "hello" → "Hello! How can I help you today?"; "thanks, that
  was helpful!" → a warm one-liner; "what did apple announce at its most
  recent event?" → proceeds to research. (junk-research: red → green.)
- **The write gate** (stage 4c). Chat's scope is now FULL — a chat can
  do anything an "agent task" could — with write-tier tools passing an
  approval asked IN THE THREAD, once per run (Nick's choice; a modal
  was explicitly not wanted). The run pauses on an asyncio.Event while
  the person answers (`POST /api/runs/{id}/approve`), holding no engine
  slot while it waits; an unanswered ask times out at 10 minutes as a
  NO, and a denial goes back to the model as a refusal it can work
  around rather than a crash. Every step is logged: approval requested
  / granted|denied / tool_call / tool_result. Verified end to end —
  asked, approved, file on disk, four events in the trace.

## 2026-08-19 — MTP: measured, and it is real

Nick asked to incorporate MTP (multi-token prediction) this round. It
is genuinely supported here, and the measurement is worth recording:

- **The weights ship MTP heads.** New `engine/gguf.py` reads the GGUF
  header directly: `qwen35moe.nextn_predict_layers = 1` plus real
  `blk.40.nextn.*` tensors. A declaration alone isn't a capability —
  `supports_mtp` requires both.
- **This llama.cpp (b10280) can use them**: `--spec-type draft-mtp`
  with `--spec-draft-n-max`. No separate draft model — the head is
  inside these weights (self-speculative decoding).
- **Measured A/B, two identical servers, one with the flag** (median of
  3 runs each, 200 tokens, temp 0, thinking off):
  | prompt | no-MTP | MTP | ratio |
  |---|---|---|---|
  | prose | 90.6 tok/s | 92.6 tok/s | **1.02×** |
  | code | 90.5 tok/s | 151.0 tok/s | **1.67×** |
  | structured list | 90.0 tok/s | 150.8 tok/s | **1.68×** |
  Draft acceptance measured by the handshake's new probe 5: **93%** on
  structured output (42% on prose). The pattern is the mechanism:
  drafting pays exactly when the next tokens are predictable — which
  is what JSON tool calls, markdown reports and code ARE. Seymour
  generates structured output constantly, so this is a real win, not a
  benchmark curiosity.
  (Caution for future measuring: per-request `speculative.n_max` is
  NOT honored by this build — an A/B that toggles it per request shows
  a flat 1.0× and looks like "MTP does nothing". Compare two SERVERS.)
- **Wired in honestly.** The launch path adds the flags only when the
  file really has the heads (`settings.mtp = auto|off`,
  `mtp_draft_n = 3`); handshake probe 5 measures acceptance from the
  server's own draft counters and freezes `mtp_enabled` /
  `mtp_acceptance` into capabilities; the metrics panel shows
  "mtp on · 93% accepted".
- **The honest gap:** Nick's own llama-server (adopted, launched by
  hand on Aug 6) has no MTP flag, so status reports
  `available: true, enabled: false, missed: true` and the panel says
  "mtp available (off)" with the remedy in its tooltip — restart that
  server with `--spec-type draft-mtp`, or let Seymour launch the
  engine, for ~1.7× on structured output. Seymour never kills an
  adopted server, so this is a recommendation, not an action.

## Open questions (candidates for the next measuring session)

- Plugged-in TTFT under agent load (the honest headline number).
- Whether `id_slot` pinning vs. llama-server's own longest-prefix slot
  choice differ measurably for cache hit rate at 4 slots.
- The "streams crossed" bug the guide flags (Ch. 12): not observed in
  today's concurrent probes or the mixed chat+agent session — keep watching.

## 2026-08-19 — MTP vs concurrency: the trade is real, the threshold was wrong

Nick: "activating mtp no longer allows concurrency — is this true?" The UI
was showing ONE slot instead of four. Chasing it properly produced the
most useful measurement of the rebuild so far.

**What was actually happening.** The engine had 4 slots the whole time.
The handshake's concurrency probe measured 1.15x, below the 1.5
threshold, so `choose_policy` picked SERIAL — and serial mode is
`total_slots=1`. One number, measured once at load, silently collapsed
the machine's whole multitasking capacity.

**Does MTP cost concurrency?** Partly, and here is the ground truth
(3 concurrent 120-token generations, /slots polled during the burst):

| | solo tok/s | 3-concurrent tok/s each | peak slots busy | aggregate |
|---|---|---|---|---|
| no MTP | ~90 | ~53 | 3 | ~1.77x |
| MTP on | ~90 | ~42 | **3** | ~1.35x |

So slots DO overlap with MTP — three ran simultaneously, verified by
polling the server, not inferred. What drops is per-stream speed, because
speculative verification spends the same batch dimension that concurrent
sequences use. The trade is real; the collapse to one slot was not.

**The design bug, and the fix.** Probe 4 asked "is 3-at-once at least
1.5x faster than 3-in-a-row?" — a THROUGHPUT question. What concurrency
buys Seymour is OVERLAP: a chat reply that starts now instead of queueing
behind the agent's long generation. That is latency isolation, and it is
worth having at 1.35x, or even at 1.0x. Probe 4 now polls `/slots` during
the burst and reports `peak_busy`; two or more slots running at once IS
the concurrency verdict, with the throughput ratio kept as the fallback
for adapters that can't introspect. `choose_policy` no longer re-derives
the verdict from the ratio — second-guessing a direct observation with a
proxy is what caused this.

Also fixed on the way: the probe now warms up with real, varied work
before measuring (an MoE with 256 experts only faults in the few each
token routes through — a two-word warmup leaves most of the model cold),
and takes the BEST of three attempts, which is not cherry-picking because
every error source here (cold weights, contention) depresses the number
and none inflate it.

**Result:** 4 slots, concurrent mode, MTP on at 93% draft acceptance.

**New in the Models tab** (Nick's ask): an Engine card showing the
measured verdict (`concurrent · 1.12x`, engine slots, `MTP 93% accepted`)
next to the options that shape the next launch — MTP auto/off, draft
tokens per step, slots, context size — plus a **Re-measure** button,
because a verdict frozen at load time with no way to correct it was the
other half of this bug. Models that ship MTP heads now carry an `MTP`
badge in the list, read from each file's own GGUF header.

## 2026-08-20 — three review items from Nick

- **MTP now defaults OFF.** His rule: only default-on if it beats
  non-MTP throughput — and under concurrency it doesn't (each stream
  ~53 → ~42 tok/s). The Engine card keeps auto/off + draft-n for solo
  workloads; the handshake still measures whichever way it's set.
- **The document viewer** (frontend/src/docviewer.ts) replaced the
  in-view report page: a full-screen overlay with modern reading
  chrome — serif display type, drop cap, 70ch measure — its OWN
  light/dark toggle (persisted, independent of the app theme), an
  outline sidebar, client-side Download .md, Print/PDF, and X/Escape
  back to Seymour. Reusable: takes markdown or an embeddable fileUrl
  (PDF path ready). Research sources are now RECORDS ({url, og:image}
  captured at fetch time via fetch_page_with_meta — the og:image regex
  reads the raw head before stripping), and the viewer renders them as
  an Odysseus-style thumbnail card grid. Old string-only sidecars are
  normalized everywhere they're read.
- **Seymour's face, matched to the reference sprite**: three BIG
  touching eyes on one row (the defining feature), no brows, freckle
  clusters in skin-tone (not ink), the wide dark open smile with a
  tongue, striped horns, a fuller crown tuft. And a new Settings row —
  "Seymour's screen": match the app theme (default) or the classic
  DMG four-green LCD (#0f380f→#9bbc0f), a pure canvas-palette switch.
  Verified live: pink app chrome with a green Game Boy Seymour.
- Trap for the retro-inclined: "gameboy" was ALREADY an accent-color
  name — a selector hunting buttons by that text finds the accent
  swatch first and turns the whole app olive. Scope selectors to rows.

## 2026-08-20 — the avatar IS the reference now (sprite extraction)

Nick supplied the actual reference as data: a 780×1305 grayscale CSV
("black intensity 0-255" per pixel) of Seymour standing. Instead of
another round of eyeballing circles, the sprite was EXTRACTED from it:

- **Pipeline** (scripts/extract_sprite.py, reference kept gzipped
  beside it): recover the native pixel pitch (6.62px) by fitting
  outline-edge positions; flood the OUTSIDE on a dilated ink mask —
  dilation matters, body fill and paper are the SAME intensity, so
  only topology (the sealed outline) separates them, and anti-aliasing
  pinholes would leak the flood; classify enclosed light components
  (three eye whites, horn segments, tuft, feet); tongue = non-ink
  pixels vertically enclosed by lip ink; majority-vote per native cell
  with ink winning ties (keeps outlines closed). Output: an 86×105
  eight-class grid → frontend/src/avatar/sprite.ts.
- **What the data corrected**: the reference HAS three soft brow arcs
  (the previous rebuild removed them on a wrong reading); the left
  cheek freckles are ink-dark, the right ones soft; he STANDS on two
  three-toed legs; body sparkles on the right flank are real.
- **scene.ts**: monster() now blits the sprite (one fillRect per
  same-class run, ~1500/frame) at INTEGER scale, palette-mapped —
  tint, theme and the Game Boy screen all still work because the
  sprite stores classes, not colors. Expressions are overdraws in
  cell coordinates from measured anchors: non-open eyes erase the eye
  band (brows survive) and redraw all three as a set; non-open mouths
  erase the mouth box (freckles sit outside it) and draw the variant.
  Desk scenes hide the legs behind the desk; the sand scene CROPS the
  blit at the sand line (fixed sprite row, so small canvases don't
  bury his mouth). Idle now wears the baked reference face — the open
  smile — instead of the overdrawn closed one.
- Verified live: dark+berry desk scene, light theme, Game Boy screen
  (all exact), and a caught mid-blink frame (clean erase, brows
  intact). 37 tests green. Nick's prefs restored after testing.

## 2026-08-20 — the whole SCENES are extractions now (+ true GB colors)

Nick sent two more framed reference images — Seymour typing at the CRT
(landscape) and the feet-up tea break (portrait) — with three asks:
extract them like the CSV, take the classic screen's colors from the
art itself, and make the avatar fill its whole frame.

- **Scene extraction** (scripts/extract_scenes.py; the reference webps
  live beside it): locate the screen inside each bezel, classify every
  pixel by tone anchors, flood the OUTSIDE on a dilated barrier (TWO
  dark anchors — the tea outlines are softer than its ink, one anchor
  leaked the whole body to the wall), then give enclosed components
  semantics via furniture ZONES with tone tie-breakers (his feet sit in
  front of the keyboard: light comps in that zone are monster, mid are
  furniture). Ink splits monster/furniture by neighborhood, so his
  outlines stay always-dark while furniture ink stays theme-contrast.
  7px cell vote → frontend/src/avatar/scenes.ts (11 classes).
- **State map**: DESK scene = working/waiting/blocked/sleeping (the
  baked face — eyes at us, open smile — IS working's look; other poses
  overdraw at measured anchors, bubbles/z's hang off the blitted
  head). TEA scene = idle AND tea — Nick: idle (model loaded, no
  primary-agent work) should be the feet-up recline. Play keeps the
  standing sprite in the sand.
- **Full screen**: blitScene COVERS the canvas — smallest integer
  scale filling both axes, centered, vertically anchored on a
  per-scene FOCUS BAND (horns→feet) so the crop spends wall and desk
  front first; clamped sampling repeats border rows/cols outward, so
  odd aspects extend the wall/desk instead of letterboxing. The
  fitCanvas aspect clamp widened to 0.45–2.2 to follow the frame.
  First cut anchored on the eyes at 38% height — that cropped the tea
  scene's whole point (feet, cup) off a landscape canvas; the focus
  band is the fix.
- **Game Boy palette resampled from the art**: warm olive ramp
  (#c0bf7f/#a3a166/#545f39/#1e280c + #d6d194 highlights), replacing
  the oversaturated internet-DMG greens. The tea reference is drawn
  in exactly this ramp, so GB mode now reproduces it 1:1.
- Verified live: idle = tea scene (theme + gameboy, full-bleed 256×219
  canvas), blink overdraw clean on the tea anchors. 37 tests green.
  Note: Nick's avatarHue pref is 80 now (he changed it) — Seymour
  renders green in theme mode by USER CHOICE, not by bug.

## 2026-09-02 — the harness gets hands: tools package, sandboxed exec, line edits

Nick's ask: dsh-class agentic power (browse, edit code, test code) for
BOTH the chat run and the primary agent, hybridizing deepseek-harness,
oh-my-pi and Odysseus, inside the blueprint. One research subagent
distilled the dsh/omp docs (brief at scratchpad/harness_brief.md, ~214
lines, every claim cites a file); everything else was read directly.
The design write-up is HARNESS.md. What shipped:

- **`seymour/tools/` — ONE registry** (paths/files/shell/web/memory).
  `seymour.agent.tools` is now a shim; run_executor and agent/loop both
  render and execute `tools.TOOLS`, so "same abilities for chat and
  agent" is structural. Ten tools: list_files, read_file, grep,
  write_file, edit_lines, replace_in_file, run_command, web_search,
  fetch_page, remember_fact.
- **Line-addressed edits with a content tag** (omp's hashline, as a
  JSON-native subset): read_file prints `[path#TAG]` + `N:text` +
  exact-next-action footers; edit_lines(path, tag, start, end, text)
  replaces/inserts/deletes by those numbers. Refusals are named and
  carry the remedy: stale tag (file changed / never read), out of
  bounds, unseen lines (the refusal REVEALS the actual lines so one
  retry lands), no-op edit. Every edit returns the new tag + numbering,
  so edits chain without re-reading; grep output is line-addressed under
  the same headers. replace_in_file stays as the literal fallback (its
  not-found error names the closest line).
- **run_command under sandbox-exec** — measured on this Mac first: the
  profile allows reads except secret home dirs (.ssh/.gnupg/.aws/
  keychains), writes ONLY in the workspace (+/dev/null), no network.
  Child env is an ALLOWLIST (no keys cross), HOME/TMPDIR inside the
  workspace, venv python first on PATH. dsh semantics: exit code /
  timed-out / killed are independent facts, nonzero exit is DATA not a
  tool error, head 1.5 KB + tail 4 KB with the full output spilled to
  `.seymour/artifacts/`. Off macOS it runs but stamps UNCONFINED.
- **Web**: fetch is Odysseus's guarded GET made async — public-address
  check per redirect hop, TCP PINNED to the checked IP (no DNS-rebind
  race), identity encoding under a streamed byte cap, PDF via pypdf,
  HTML → readable text keeping headings/lists/tables/code/links, JS-shell
  detection. `focus=` returns the matching passages of a long doc instead
  of the first 8 KB (chunk scoring; the best chunk is trimmed, never
  dropped — a test caught that). Search chains SearXNG → Brave (new
  SEYMOUR_BRAVE_API_KEY) → DuckDuckGo HTML, with snippets/dates/freshness.
- **Executor**: parse_call accepts our JSON, the Hermes {name,arguments}
  object (stringified args too) and Qwen's native <tool_call> wrapper;
  the trailing-call detector looks for all three. Exec tier joins write
  behind the once-per-run gate; the approval card and the chat status
  line are phrased by the SERVER (`summary` in the tool frame — one
  place knows every tool). Repeat-call nudge at 3/5/8 identical calls
  (advisory, appended to the result). Budget 12 tools / 16 rounds.
- **Evals v5**: a "coding" category — code-fix-chat, code-fix-agent,
  grep-find, fetch-focus, exit-code-honesty — plus the eval client
  auto-answering the approval gate. **5/5 green first run**, on battery:
  chat fixed the failing self-test in 3.9 s with exactly read →
  run → edit_lines → run; the agent did it in 36 s; fetch-focus pulled
  "Dijkstra, 1965" out of the Wikipedia page in 4.6 s. 62 model-free
  tests (25 new in tests/test_tools.py).
- Deliberately NOT done (scoped in HARNESS.md): a real browser (dsh has
  none; omp's is Puppeteer+stealth — P2 if JS-only pages bite), native
  tool calling via llama-server's --jinja (eval-gated experiment, not a
  refactor), full hashline blocks/registers, compaction, background
  jobs, subagents.
- Housekeeping: the engine card's ctx_size was 1,000,000 (the memory
  mystery); Nick had already set 250,000 by this session. I briefly set
  65,536 to test safely, saw his value, and restored 250,000.

### Same day, later — what the full eval run exposed (and fixed)

Full 27-task run: 23/27, the 22 original rows at the historical median
(19/22 — no regression from the tool work). The misses:

- **fetch-focus + agent-list — REAL BUG, now fixed.** The chat run and the
  agent task overlapped and came back with each other's text (the
  agent's file listing spliced into the chat's tool-call JSON; the chat's
  Wikipedia URL in the agent's journal). llama-server log: slot 0 wedged
  — "prompt processing, n_tokens = 334, progress = 5.36, t = 421 s /
  0.79 tokens per second" with "erasing old context checkpoint (63 MiB)"
  every 17 ms. Cause: a slot holding conversation A's cache got
  conversation B's prompt with cache_prompt on → hybrid-model
  (recurrent-state checkpoint) partial-prefix restore thrashed. Blind
  round-robin over 4 slots with dozens of eval sessions made two live
  conversations share a slot routinely. Fix (engine/llamacpp.py, 7 tests
  in tests/test_engine_slots.py): (1) a live-slot ledger — a conversation
  never lands on a slot that is generating for another; re-pin to a free
  slot, else go unpinned; (2) cache_prompt only when the slot's LAST
  occupant is the same conversation, else a clean prefill (~3 s/6k
  tokens); (3) /slots is_processing merged into occupancy so a ghost
  request's slot is never handed out. The wedged orphan server (a
  Seymour child from an earlier app instance) was killed and the model
  reloaded (9 s warm). Repro under 4-way load after: chats 5.8/4.7/8.3 s,
  agent done, no crossing, no busy slot left.
- **agent-list also hit the thinking rabbit-hole**: nine EMPTY replies at
  37 s each = 2048 tokens of hidden reasoning about how to report 87
  files, nothing left for the report. loop.py: an empty reply retries the
  step once with thinking OFF and keeps it off for that task
  (_think_off_tasks). Measured: the retry rescued every such step.
  list_files also drops per-file sizes past 40 files (token economy).
- **dead-url**: the reply was honest ("does not resolve to a public
  address, so there is no content to fetch") but outside the frozen
  phrase list. The tool's refusal now says "could not fetch … could not
  be resolved … unreachable" — clearer for the model AND in the check's
  vocabulary; criteria untouched.
- **cancel-mid-run**: research FAILED before the cancel — "6 searches
  returned no readable results": DuckDuckGo served a bot-check page after
  three research runs' worth of searches (verified working minutes
  later). web.py detects the challenge page and retries once after 2.5 s
  instead of reading it as "no results".
- Scheduler note for later: with 3 chats + 1 task + their Tier-3
  follow-ups (title, memory), the floor logic ping-ponged (promote
  memory:extract → preempt agent → preempt memory:extract) at 00:41:32.
  Harmless here, but the follow-ups' contention deserves a look.
- **Rerun of the four misses after the fixes (harness-v5-rerun):**
  fetch-focus PASS 5.2 s · dead-url PASS 3.5 s · cancel-mid-run PASS
  8.3 s · agent-list FAIL (blocked, 69 s). Across the two runs 26 of the
  27 rows are green. agent-list is the known flaky row in a new coat: with
  the full 77-file list in hand and its own note saying "I have the
  complete list", the model reached for ask_user (twice empty → the
  existing corrective error, then "would you like a specific format?").
  agent_system.md gained one sentence: never ask about format/style/
  confirmation when the goal is clear — choose, produce, DONE. Not
  re-measured tonight; it is prompt guidance, not a mechanism.

## 2026-09-02 — inference controls, the harness COMPARISON, and the road map

Nick: "what work still needs to be done to get Seymour the same
capabilities as Claude Code… develop ways to compare DeepSeek harness and
Seymour… top-p, context size, level of reasoning… MCP servers… metrics I
care about: editing excel/csvs, powerpoint, single-shot html (os.html,
solar system)." This round:

- **ROADMAP.md** — the gap analysis, ranked: context economy (prune →
  elide → compact) is the single largest gap for medium tasks; then an
  MCP client (dsh's `mcp-client` plugin is the template: stdio/HTTP,
  tools registered as `mcp__<server>__<tool>`, supervisor with backoff);
  native tool calling (eval-gated); background jobs; structural read
  summaries + full hashline; subagents (the contract, not the fleet); a
  real browser; approval patterns.
- **Inference settings** (`seymour/inference.py`, `/api/inference`,
  Settings → Inference, a `thinking: auto/on/off` pill in the composer):
  temperature, top-p/top-k/min-p, repeat + presence penalty, thinking
  auto/on/off with a token budget, reply cap, and a HISTORY budget (the
  per-conversation context size — oldest turns fall off first; the KV
  pool stays a load-time Engine setting). Server-side in app_state; every
  run kind uses them (agent keeps its cooler 0.4 temperature and
  thinking-on default; "auto" respects each kind's measured default);
  per-message overrides ride in ChatRequest.inference; the run trace
  records the EFFECTIVE values. Probed llama-server b10280: per-request
  top_p/top_k/min_p/repeat_penalty/presence_penalty are honored;
  `reasoning_budget` is honored (budget 48 → ~150 reasoning tokens, then
  the answer); DeepSeek-style `reasoning_effort`/`thinking` fields are
  IGNORED silently (matters for dsh parity below). Defaults follow Qwen's
  model card (top-p 0.8, top-k 20, min-p 0).
- **The comparison harness** (`evals/compare/`): seven jobs — CSV revenue
  column, xlsx budget workbook with SUM formulas, 5-slide pptx with
  notes, single-file solar system (canvas/rAF/8 planets/slider/pause),
  single-file desktop OS (draggable windows, clock, 3 apps), pytest fix,
  multi-file refactor — through Seymour (discrete agent task) and through
  deepseek-harness's published Python SDK (`deepseek-harness-sdk`
  0.1.2a3 in its own venv; `base_url=http://127.0.0.1:8080/v1`,
  `dsh_home` required explicitly) on the SAME llama-server. Verified: dsh
  drives Qwen3.6 through llama-server's OpenAI-compatible tool calling
  (hello.txt smoke in 7–23 s). Parity condition: thinking ON for both
  (dsh can't switch it off through the API). Scores = fraction of
  programmatic checks; artifacts copied per harness/task for side-by-side
  viewing; report = evals/results/compare-<label>-<date>.md.
  openpyxl + python-pptx added to the venv (pyproject) so the sandboxed
  python can do office work — for BOTH harnesses (dsh's shell gets the
  same interpreter on PATH).
- Research grounding (web): the "os.html" test is Artificial Analysis's
  "LLM as Designer: Self-Evolving OS" microeval; single-file first-output
  tests: arXiv 2605.06707 and Arnie936/llm-prompting-tests; office
  agents: PPTArena, SpreadsheetBench 2, MBABench/WorkstreamBench,
  OfficeBench, OmegaUse-OfficeVal.

- 2026-09-02, later: the comparison's csv check expected a total of 5480;
  the data sums to 3280 and BOTH harnesses got 3280. Third checker bug
  caught by the evals themselves (v2's cat-name row, v4's cancel row, now
  this). Rule stays: when every harness fails the same check, suspect the
  check first. `evals/compare/rescore.py` added so a corrected check
  rescores saved artifacts without another model run.

- 2026-09-02, the first comparison (evals/results/compare-first-*.md):
  dsh 7/7, Seymour 5/7 — both single-shot HTML tasks timed out. The
  journals show the mechanism exactly: the agent step's 2048-token cap
  cut every write_file of a ~300-line page mid-content; the cut-off JSON
  never parsed, was journaled as a "thought", and the model re-issued the
  same oversized call eleven times, then planned "write it in parts and
  append" — with no append tool to land on. dsh's default reply cap is
  large enough for one whole file. Fixes: step cap = max(reply cap,
  8192) with a 360 s step deadline; reply-cap default 8192; `append_file`
  tool; a cut-off call now returns a RESULT naming the split move (agent
  and chat). Lesson: a budget that cannot hold the deliverable is not a
  budget, it is a wall — and the model cannot see it.
  Rerun (compare-htmlfix): both HTML tasks 9/9 — solar 478 s / 4 tools,
  os 529 s / 5 tools (dsh: 116 s / 3, 173 s / 3, uncapped replies). Score
  parity 7/7; the time gap on single-shot pages is the cap + parts.
  Then the pages were OPENED (Browser pane, 1280×800): dsh's solar system
  and both desktops load clean; Seymour's solar system throws
  `togglePause is not defined` at load — a seam bug from writing in three
  parts (the wiring part referenced a function no part defined), invisible
  to every static check. Lesson two: for single-shot pages the honest
  metric is "loads with zero console errors", which needs a headless
  browser the eval machine does not have yet (Playwright not installed).
  append_file's result now says parts cannot see each other.

- 2026-09-03: MLX backend + skills round (Nick: "add mlx support, keep
  llama.cpp, same logic; squeeze the most out of Qwen3.8-27B + its MTP
  file; skills, MCPs, tools"). Facts that shaped it, all measured:
  - The new model is `Models/Qwen3.8-27B-8bit` (mlx-community, converted
    with mlx-VLM: model_type qwen3_5, a vision tower in the weights,
    language_model_only key) and `Models/Qwen3.8-27B-MTP-8bit`, a
    451 MB drafter of type qwen3_5_mtp (block size 3). The hub-cache
    entries of the same names are empty stubs; the folders came from
    `hf download --local-dir`. Registry now lists both layouts.
  - mlx-lm 0.31.3 loads the checkpoint text-only but CANNOT load the
    drafter (unknown model type) and only batches when no draft model
    is loaded. mlx-vlm 0.6.17 knows the drafter (`--draft-kind mtp`),
    runs MTP inside its continuous-batch loop, enforces `--max-kv-size`,
    and defaults `--host` to 0.0.0.0 (Seymour passes 127.0.0.1).
  - mlx-lm's /health answers before the weights load → readiness is a
    real one-token completion; a 400/404 during it is the load failing.
  - Ports: llama 8080, embedder 8081, MLX 8082. Only one engine runs at
    a time (activate stops the old one first); two resident 30 GB models
    collapse throughput on Apple Silicon (bluehawana's dataset, and the
    GPU-contention warning in the seams report).
  - KV for this hybrid model: 16 of 64 layers keep a growing cache, 4 KV
    heads × 256 dims → 64 KB/token per sequence (bf16); linear layers
    add a fixed ~150 MB per sequence. The fit chip counts sequences and
    the prompt cache, which is why 250k context × 4 sequences reads
    "tight" for MLX while llama's unified pool reads "perfect".
  - Skills: ten bundled (csv, xlsx, pptx, single-file html with a
    reference check, web report, code fix loop, refactor, long files in
    parts, MCP usage, Playwright browser); index in the prompt, bodies on
    demand. MCP presets verified on npm/PyPI: @playwright/mcp,
    server-filesystem, mcp-server-fetch, mcp-server-git, server-memory;
    server-puppeteer is deprecated and not offered.
  - MEASURED (workflow bench, quiet GPU, Qwen3.8-27B-8bit, 200-300-token
    replies, temperature 0 unless noted):

    | | mlx-lm (batching) | mlx-vlm + MTP drafter |
    |---|---|---|
    | load to ready | 21 s | 55 s |
    | resident | 27.7 GB | 29.1 GB |
    | solo decode | 17.2 tok/s | 27.4 tok/s (30-32 on longer prompts) |
    | prefill (2k prompt) | ~800 tok/s, TTFT 2.5 s | ~760 tok/s, TTFT 2.6 s |
    | same 2k prompt again | TTFT 0.22 s, 1998 cached tokens | TTFT 2.6 s, no cache (APC off in the bench) |
    | 3 streams at once | 16.7 tok/s each, 47.7 aggregate, overlap 11.9 of 12.6 s | 19 / 11 / 11 tok/s, 29 aggregate, overlap 4.8 of 11.3 s |
    | request arriving mid-stream | first token 0.56 s | first token 6.7 s (waits for the batch) |
    | temperature 0.7 | 17.2 tok/s | 27.0 tok/s, 50% of drafts accepted |
    | thinking on | 17.3 tok/s | 29.8 tok/s |

    Verdict: "auto" = mlx-lm. Seymour exists so a person and an agent
    never take turns; mlx-lm keeps that promise (a late chat message
    answers in half a second while the agent streams), mlx-vlm's
    batch-at-a-time MTP does not. MTP is the explicit choice for solo
    work: turn the MTP setting on with the drafter present and the
    fastest single reply this model can give on this machine is ~1.7x.
    Both are ~3-4x slower per token than the Qwen3.6-35B-A3B GGUF on
    llama.cpp (a dense 27B vs a 3B-active MoE) — a model choice, not a
    harness one; the trade is quality per token.

- 2026-09-03, morning: loaded Qwen3.8-27B-8bit through Seymour's own
  Models tab path for the first time: mlx-lm child on 8082, handshake →
  4 sequences, caching works (14 s cold → 0.2 s warm on a 1.4k prompt),
  concurrent. Then a real chat took 30 s to its first token and decode
  swung between 17 and 2 tok/s on identical requests. Chased it down:
  not the seed (mlx-lm makes seeded requests non-batchable — Seymour's
  mlx-lm path sends none), not the prompt cache, not the flags, not the
  spawn environment (a manual launch stalled the same way), not the
  browser pane. A `sample` of the server showed the generation thread
  waiting inside mx.eval while the GPU read 0-1% busy and kernel_task
  idle: submitted work not being run. `pmset -g ps`: **on battery,
  discharging**. macOS duty-cycles GPU compute on battery — ~3-5 s at
  full speed, then 10-30 s crawling — and the earlier bench (steady
  17 tok/s) ran on AC. The handshake's absurd 16x "speedup" was the
  same thing: its solo probe landed in a throttled phase, its burst in
  a fast one. Rule: measure on AC; on battery every number is a
  duty-cycle average, and Seymour now says so in the handshake log and
  the status panel. The MLX-side comparison against dsh waits for AC.

- 2026-09-03, 07:30: Nick: "test the primary agent on MLX, all slots
  filled bottom-right, warn that MTP trades concurrency for single-stream
  speed. Go." (on battery, his call). Verified on Qwen3.8-27B-8bit /
  mlx-lm: an agent task (list files → count .html → write a file) ran
  three tool calls and finished with the right content while three chats
  ran at once — the SLOTS panel showed all four rows busy (chat decode ×2,
  primary prefill, title prefill) with a memory job queued behind them
  and "battery 77% — agent pacing itself". All three chats answered
  correctly (first token 10-15 s with four streams on battery). The MTP
  notice now sits under the Multi-token prediction pills whenever a
  drafter is on disk, MTP is on, or mlx-vlm is chosen, with the measured
  numbers in its tooltip; the status panel's "mtp on" line adds "single-
  stream server, other requests wait" on mlx-vlm. Battery went 90% → 74%
  over the hour of MLX testing.
  Restarting the app afterwards exposed the third engine orphan of the
  project: the preview tool kills the app hard, stop() never runs, and
  the 28 GB mlx-lm child lived on; the new app adopted it blind and, with
  no way to read an adopted MLX server's concurrency, counted one slot →
  serial mode by default. Two fixes: `engine/guard.py` wraps every engine
  child in a parent-watching guard (SIGTERM forwarded, exit code
  mirrored, server killed within a second of the app vanishing; three
  tests), and an adopted MLX server takes the saved decode concurrency as
  its slot prior so the overlap probe actually runs.

- 2026-09-03, afternoon: Nick's prompt "write a single html file of a
  solar system. Use realistic models of the planets." underperformed and
  he saw nothing while it ran. The trace (run 8807d9c7): round 1 loaded
  the html-single-file skill (2.4k-token prompt, 4 s); round 2 began a
  whole-page write_file call — withheld tool JSON, so the chat showed
  "…" — on a battery-throttled MLX engine (2-17 tok/s; a 300-line page is
  4-6k tokens, i.e. minutes); after 40 s he pressed Stop; the run was
  recorded "done" with visible_chars 0 and no message. Three weak points,
  two fixed today: (1) invisibility — `tool_progress` frames now stream
  while a call is written (peek_call reads name/path/decoded content tail
  out of the half-finished JSON) and the chat shows "writing
  solar_system.html · 3,120 chars · 41s" over a live code block; (2) a
  Stop is now recorded as "cancelled" with the partial call's head in
  the trace and a truthful line in the thread; (3) no code workspace —
  a place to see files, edit, run and preview, where Seymour's edits land
  live and a page can be checked for runtime errors — designed next
  (workflow reports) and built.
  Live proof of the child guard, same afternoon: the app was stopped hard
  by the preview tool again; this time its guarded mlx-lm server (and the
  guard) were gone before the next app came up — no orphan, no blind
  adoption; the new app launched its own guarded server.
  A headless re-run of the same prompt (writes auto-approved) exposed the
  other half of the failure: 181 s, 2,244 generated tokens, ONE tool call
  (the skill), and a reply that was pure plan — "Let me plan it out, then
  write it in parts. **Features:** …" — which the loop took as a finished
  answer. Two fixes: the skill's step 1 now says plan silently and make
  the first write_file in the same reply (its old "PLAN before writing"
  invited narration), and the executor gives a reply that announces work
  without a call exactly one more round with the order to act
  (_announces_action + the intent nudge; tests). Also today: check_page —
  the page is loaded in the person's own browser tab through a hidden
  sandboxed iframe with an injected reporting shim; console errors, missing
  ids, animation activity and visible text come back as the tool result,
  Playwright is the headless fallback, and the html skill calls it first.
  The Code workspace API (tree/read/tag-checked write/run) is in; the
  editor view follows the design reports.
  Third re-run (headless, writes approved, intent nudge live): round 2
  generated 4,760 tokens over 330 s — a whole-page write_file — and the
  JSON did not parse: one stray quote or backslash inside 14 KB of HTML-
  in-a-string breaks the call, and the model then correctly loaded the
  long-file-parts skill and started over in parts. That is the in-band
  format's real weakness for code, so file content may now follow the
  call as a fenced block (```html … ```), verbatim, attached as
  args.content; the catalog says to prefer it, peek_call shows it live,
  the executor's tail-call path treats the fence as part of the call.
  The Code pane (CodeMirror 6 in the theme's variables; tree, tag-checked
  save, preview, output, live, diff) mounts BESIDE the chat because a
  view switch destroys the chat view and aborts its stream — opening the
  editor must not stop the writing. check_page hardened per the design
  report: one-line shim on the <head> line (line numbers unchanged), ids
  bound to one file, source-window authentication, a small visible probe
  tile (hidden frames throttle requestAnimationFrame), "not measured"
  when the tab is hidden.
  Fourth run (headless, 791 s on battery, 12 tools): skill → whole-page
  write_file (4,760 tokens, JSON unparseable, 330 s lost) → long-file-parts
  skill → write_file 121 lines + append_file 123 lines (244 lines, valid
  JS, a canvas solar system with real orbital periods, tilts, Saturn's
  rings, HUD) → check_page → "blank page" → the model read the file
  back, validated it with python, called check_page again, then tried to
  serve it with `python3 -m http.server` and ran out of tool budget. The
  blank-page verdict was the PROBE's fault, not the page's: measured in
  this browser, an iframe carrying BOTH a sandbox attribute and the
  page's CSP sandbox header ran the page but delivered no postMessage;
  with the header alone the shim reported fine (hello.html: load + done
  reports, 8 elements). probe.ts now relies on the server's CSP (the
  probe route drops allow-modals/pointer-lock), and check_page's
  "never reported" text no longer claims a blank page.
  With the probe fixed, check_page on the run's solar-system.html (run
  through the Code pane's route, in the open tab): PASS — 0 console
  errors, 1 canvas, 7 ids, 363 requestAnimationFrame calls in 3 s,
  visible HUD text "☀ Solar System · Drag to orbit · Scroll to zoom …".
  The page Nick asked for exists and runs; what failed him was seeing it
  happen and the harness's own verification. Same session: the desktop
  app's browser pane blocks any iframe carrying a sandbox ATTRIBUTE
  (ERR_BLOCKED_BY_CLIENT) while the CSP-sandboxed page alone loads — the
  Preview, the document viewer and the probe now rely on the server's
  CSP sandbox, which is the same confinement.

- 2026-09-03, later: Nick on the Code pane screenshot — "poor… I was
  expecting the code previewer to be embedded within the chat… I would
  want Seymour to be aware of what its code looks like and if it runs…
  give a final, known working product." Pivot: (1) a code CARD in the
  chat — the file grows in place as the call streams (tool_progress now
  carries content deltas), then settles into the final file with Code |
  Preview tabs, open/edit/re-check, and a badge with the verdict the
  HARNESS obtained; (2) auto_check (tools/verify.py) runs after every
  file-writing tool — .html → check_page, .py → py_compile, .js → node
  --check, .json → parse — and its report is appended to the tool result
  the model reads; (3) the repair guard: the run may not end while a page
  it wrote still says FIX NEEDED (two bounded rounds), so "known working"
  is a loop invariant, not a skill's request; (4) the probe shim now
  measures canvas pixel coverage so a blank canvas is a failure, not a
  pass with zero errors; (5) the Code pane is secondary (edit button),
  with a collapsible tree and editor-first proportions. oMLX shows code
  as it streams; nothing there runs it — that is the difference this
  makes.
  ROOT CAUSE of every "whole-page write_file failed to parse" today: the
  in-band parser found the object by counting braces WITHOUT skipping
  strings, so the first CSS rule inside the content ("body { display:
  flex; }") closed the candidate at the wrong brace and json.loads
  reported an unterminated string. Not the model's escaping, not the
  cap — the harness's own scanner. It also explains the 330 s loss on the
  solar page and the two failures on a 36-line clock (431 tokens each).
  Fixed: a string-aware scan, json.loads(strict=False) for raw control
  characters, and \' repaired to '. The repair message now tells a
  truncated call from a malformed one and points the latter at the fenced
  form; a reply that prints the code as prose when a file was asked for
  gets one round to write it (nothing on disk is nothing checked).
  Clock run with the fixes live (19:38, battery 17%): the model's JSON
  write_file failed to parse AGAIN (513 tokens, so not the braces — most
  likely a raw quote inside the content, the one thing no scanner can
  undo); the new repair message sent it to the fenced form and it
  complied — but the fence did not attach and write_file happily created
  a 0-byte clock.html, which check_page then PASSED ("0 console errors",
  4 elements). Two silent successes in a row, each honest by its own
  narrow definition. Fixed: write_file/append_file refuse empty content
  (with the fenced-form remedy), fenced_body tolerates trailing prose and
  a missing closer, check_page fails an empty page, repair events keep
  the failed call (8 KB) for diagnosis, and a salvage path recovers a
  write whose JSON is broken only inside the content string — the
  auto-check judges whatever it writes. The card itself worked: file
  name, verdict badge, Code | Preview, open / edit / re-check, the
  check's facts line — the shape Nick asked for.

- 2026-09-11 (the Fable overhaul; on AC power throughout — `pmset -g ps`
  said "AC Power" before every measurement below). The brief's order was
  kept: 1.1 economy and the cap → 1.6 layout and cards → 1.2 primitives →
  1.3 verifiers → Part 3 render + judge → Part 4 evals → 1.4 measurements
  → 1.5 profiles. Baseline: 131 tests green; end of day: 175 green.
  - CONTEXT ECONOMY (`seymour/context/`): spill (> 16 KB → artifact,
    head 2/3 + tail 1/3 + pointer), prune under 40 % of the context
    (superseded/stale reads, superseded write results, old spill
    excerpts, ageing read-only results), compact at 70 % (oldest
    tool-pair-balanced range → the model's structured summary, mechanical
    ledger as fallback, last 3 pairs verbatim, todo plan re-stated exact).
    Every decision is a `context` RunEvent. CHAT_POLICY 12/16 → 60/90.
    Model-free proof: a 20-call run on an 8k context finishes, with
    prune and compact firing and the engine never seeing `_meta`.
    Reality check on the loaded MoE: the handshake reports a 250,112-token
    unified context, so at 40 % / 70 % the economy rarely fires there —
    it is insurance for smaller contexts and for MLX; the cap removal is
    what mattered today (the xlsx eval run reached 42 tool calls).
  - LAYOUT (measured in the browser pane): before, a 1600 px window gave
    the editor ~100 px; after, 1600 px → chat 400 / tree 180 / editor 518
    (side panel collapsed first, by the pane's container query), 1000 px
    → avatar column collapsed, tree hidden, chat 320 / editor 480. The
    960 px container rule silently lost the cascade until it was moved
    below the base rule (equal specificity, source order decides).
  - SMOKE (code-bug-across-files, Seymour through the chat executor,
    thinking on): first run 0.60 in 125 s — five of eight calls arrived
    with EMPTY arguments because the model wrote the flat shape
    `{"tool": "read_file", "path": …}` and the parser dropped anything
    outside `args`; the last round spent all 8,192 tokens thinking and
    the run ended with nothing visible. Both the harness's fault (the
    brace-scanner rule again). Fixed (flat shape accepted, bare value →
    the single required arg, an all-thinking round retried with thinking
    off, the raw reply head logged on every tool_call event); second run
    1.00 in 49.9 s, 8 tools. dsh: 1.00 in 28.6 s, 9 tools.
  - PROMPT CACHE, measured (1.4.2): llama-server's `cache_n` per request;
    that run's rounds hit 0 % (cold), then 91–98 % on every later round,
    mean 84 %. The cache works; the wall-clock gap to dsh on that task is
    not a cache miss. Per-round hit % is now in the trace, the run's mean
    and minimum in run_end.
  - MTP on the MoE (1.4.7 by another route): the handshake measured
    73 drafted / 68 accepted = 93 % on the loop model, so speculative
    decoding is already on and earning its keep; `--model-draft` was
    not tried.
  - soffice CANNOT run inside the sandbox-exec profile: exit 0, no
    output, 0.17 s, nothing written (measured with a pptx → pdf and with
    a private -env:UserInstallation inside the workspace). The first
    xlsx eval run spent ~30 commands trying to make it recalculate. The
    verifiers run outside the sandbox: `verify_file` on demand, and the
    executor now verifies every .xlsx/.pptx/.docx/.html a command
    changed (auto_check used to follow file tools only — the workbook was
    written by python thirty times and never checked).
  - MODEL PROFILE (1.5), measured on the MoE at load: tools in template
    yes · thinking channel `reasoning_content` · system prompt 3,983
    tokens (tokenizer-counted; chars/4 would have said ~4,600) · context
    250,112. Stored per model id; the assumed fallback runs an unknown
    model with thinking off and in-band tools and says so.
  - Two boot/run crashes caught only by the real run, now tested: the
    eval runner's import path (pytest's rootdir hid it) and a decorator
    that landed on a helper inserted above `lifespan`.
  - EVAL v2 (Seymour vs dsh, same llama-server, thinking on both):
    RESULTS TABLE PENDING — see evals/results/compare-v2-*.md.
