# Seymour — The Harness

*What makes an agent harness good, what three good ones do, and what
Seymour took from each. Written 2026-08-20 at the end of the harness
rebuild; read it before touching `seymour/tools/`, `run_executor.py` or
`agent/loop.py`.*

## The question

Seymour's agent could search, fetch, and write whole files. It could not
change a file precisely, could not run anything, and the model spent its
budget re-learning a home-grown call format. The ask was the power of a
DeepSeek-Harness-style agent — browsing, editing, testing — inside
Seymour's blueprint: one model, three tiers, a traceable run log, every
"might never stop" path bounded.

Three harnesses were read in source, not summary: **deepseek-harness**
(dsh — TypeScript, MIT), **oh-my-pi** (omp — TypeScript/Bun+Rust, MIT,
the small-model specialist), and **Odysseus** (Python, AGPL, the project
Seymour forks in spirit). The distilled brief is long; this is the part
that changed Seymour.

## What makes each one good

**dsh** is a machine for never lying to itself. Its loop invariant —
"model-visible means logged" — means every prompt can be rebuilt from an
append-only event log, which Seymour already had (Run/RunEvent) and kept.
Its tool pipeline turns every failure into a *result the model reads*
(unknown tool, bad arguments, a thrown exception) so a bad call never
ends a turn. Its bash executor reports exit code, timeout and kill as
*independent facts* ("a process can time out AND exit 0 because it
trapped the signal") and treats a nonzero exit as data, not tool
failure — a harness that flags a failing test run as an error teaches
the model to stop running tests. Oversized output keeps the head and the
tail and spills the whole thing to a file the model can read back. Its
web seam is two operations behind one policy owner, with the honest
rule that a 404 is a *result*. Its one gap for Seymour's setting: it
defers SSRF blocking to deployment — a LAN laptop cannot.

**omp** is the small-model specialist, and its one big idea is
**hashline**: file reads come back as numbered lines under a `[path#TAG]`
header, and edits are expressed as *line ranges from that read* plus the
tag — the model never reproduces old text byte-for-byte, the tag proves
its view of the file is current before any matching happens, and a
seen-lines guard refuses edits on lines no read displayed (revealing them
so one retry lands). omp measured a 6.7% → 68.3% edit success jump on one
small model from the format alone. Around it: a token economy (elide
uneventful results, spill long ones, never cut a call from its result),
an exact wire format for Qwen3's native `<tool_call>` dialect, and a
non-interactive shell environment list worth copying verbatim.

**Odysseus** contributed the Python. Its `edit_file` (unique literal
replacement with an error that says *why*), its `ls`/`glob`/`grep` with
secret-path denylists, its DNS-pinned fetch transport (the connection
goes to the address that was checked, closing the rebinding race), its
capped streaming downloads, its multi-provider search with freshness
filters, and its subprocess tools with process-group kills are all
patterns Seymour's `tools/` now carries — re-expressed, credited.

## What Seymour became

```
seymour/tools/            ONE registry, used by every run
  __init__.py   Tool(name, description, args, tier, func, describe)
                catalog(scope) · render_catalog · parse_call · execute
  paths.py      the sandbox boundary (resolve, secrets denylist, artifacts)
  files.py      list_files · read_file · grep · write_file · edit_lines · replace_in_file
  shell.py      run_command  (sandbox-exec profile, dsh result semantics)
  web.py        web_search (SearXNG → Brave → DuckDuckGo) · fetch_page (+focus)
  memory.py     remember_fact
```

- **Same abilities everywhere.** `run_executor.py` (chat) and
  `agent/loop.py` (primary agent, discrete tasks) render and execute the
  same `tools.TOOLS`. Chat gates write/exec tiers with the once-per-run
  in-thread approval; the unattended agent works inside the sandbox
  without asking, because the sandbox *is* its permission boundary.
- **Edits by line, proven fresh.** `read_file` → `[path#TAG]` + `N:text`
  + an exact-next-action footer. `edit_lines(path, tag, start, end,
  text)` replaces/inserts/deletes by the numbers just read; stale tag,
  out-of-range, unseen lines and no-op edits are all *named* refusals
  with the remedy in the text. Each edit returns the new tag and
  numbering so edits chain without re-reading. `replace_in_file` keeps
  the literal form as a fallback (its not-found error names the closest
  line). `grep` output is line-addressed under the same headers, so an
  edit can anchor from a search.
- **Commands run confined.** `run_command` executes under a macOS
  `sandbox-exec` profile measured on this machine: reads allowed
  (interpreters, libraries) *except* secret home folders (.ssh, .gnupg,
  .aws, keychains); writes only inside the workspace; no network at all.
  The child environment is an allowlist (no keys cross), HOME/TMPDIR
  point inside the workspace, PATH puts the project venv first. Results
  lead with `[exit code N (Xs)]`, `[TIMED OUT …]` or `[killed by a
  signal]`; long output keeps 1.5 KB head + 4 KB tail and names the
  spill file. Off macOS the tool still runs but stamps every result
  `UNCONFINED`.
- **The web, pulled properly.** `fetch_page` is Odysseus's guarded GET
  made async: public-address check per redirect hop, TCP pinned to the
  checked address, identity encoding under a streamed byte cap, PDF via
  pypdf, HTML → readable text with headings/lists/tables/code preserved
  and in-content links kept. A `focus` argument returns the *matching
  passages* of a long document instead of its first 8 KB — the way a 35B
  reads a 40-page page. `web_search` chains SearXNG (if configured),
  Brave (if keyed) and DuckDuckGo HTML, with snippets, dates and a
  freshness filter.
- **Calls parse in every shape.** `parse_call` accepts our documented
  JSON, the OpenAI/Hermes `{name, arguments}` object (stringified
  arguments too), and Qwen's native `<tool_call>…</tool_call>` wrapper —
  a model trained on the last one reaches for it, and a repair round is
  a waste of its budget.
- **Loop hygiene.** Identical consecutive calls get an advisory nudge at
  3/5/8 (dsh's repeat-tool reminder), appended to the result, never
  replacing it. Chat's budget rose to 12 tools / 16 rounds so a
  read → edit → test → fix loop fits twice.

## What the full eval run taught (2026-09-02)

The first full run of the new set scored 23/27 — the 22 original rows at
their historical median (19/22) — and the misses pointed at one real
defect that the tool work had made *visible*: a chat run and an agent
task running at the same time came back holding each other's text. The
llama-server log showed the mechanism: a slot that had cached one
conversation's prefix received another conversation's prompt with
`cache_prompt` on, and the hybrid-model reuse path (Qwen3.6 has recurrent
layers, so reusing a cache means restoring a recurrent-state checkpoint)
**wedged the slot** — "prompt processing … 0.79 tokens per second" while
erasing and recreating a 63 MiB checkpoint every 17 ms, for minutes.
Every request pinned to that slot then queued behind it (the "no tokens
for 120 s" stalls), and its output crossed streams. Blind round-robin
over four slots with dozens of conversations made the collision routine.

Three rules now live in `engine/llamacpp.py`, each with a test:

1. **A live slot is never shared.** The adapter keeps a ledger of which
   conversation is generating on each slot; a conversation whose pinned
   slot is live for *another* is re-pinned to a free slot, and with none
   free it goes unpinned — cache lost once, nothing corrupted.
2. **Cache reuse only for the conversation that built it.** `cache_prompt`
   is on only when the slot's last occupant was the same key; any other
   request on that slot gets a clean prefill (~3 s for a 6k prompt) rather
   than a partial-prefix restore.
3. **The server's own `/slots` occupancy counts.** A ghost — a request
   whose client gave up while the server kept going — is invisible to our
   ledger; slots the server reports busy are not handed to a new
   conversation.

The same run exposed the agent's other stall: a step whose entire 2048-
token budget went into hidden thinking (an 87-file listing to report)
returned *empty*, nine times in a row at 37 s each. The loop now retries
such a step once with thinking off, and keeps it off for that task.
Under a deliberate four-way load afterwards, three chats finished in
5–8 s with correct answers and the agent completed its task, with no
crossing and no slot left processing.

## Added 2026-09-02 (evening): controls, MCP, the comparison

- **Inference settings** (`seymour/inference.py`, Settings → Inference,
  the composer's `thinking` pill): temperature, top-p/k, min-p, repeat and
  presence penalties, thinking auto/on/off with a token budget, the reply
  cap and a per-conversation history budget. Server-side, applied to
  every run kind, overridable per message, recorded in each run's trace.
  llama-server honors the sampling fields and `reasoning_budget` per
  request (measured); it ignores DeepSeek-style `reasoning_effort`.
- **MCP client** (`seymour/mcp.py`, Settings → MCP servers): stdio
  JSON-RPC, `tools/list` on connect, tools registered in the ONE registry
  as `mcp__<server>__<tool>` (dsh's shape), tier exec, 60 s per call,
  reconnect/remove from the UI. Tools only — resources and prompts have
  no consumer yet. `scripts/fake_mcp_server.py` is a two-tool demo.
- **Opening what a run made**: `/api/workspace/file` serves workspace
  files (confined, HTML under a sandboxing CSP); chat shows an "open"
  chip for every `.html` a run writes, and the document viewer renders
  it in a sandboxed iframe — the solar system runs inside Seymour.
- **The comparison** (`evals/compare/`): the same seven office/web/code
  jobs through Seymour and through deepseek-harness's Python SDK on the
  same llama-server; see EVALS.md. ROADMAP.md ranks what is still
  missing against Claude Code.

## What the first comparison taught (2026-09-02, evening)

Seven tasks, both harnesses, one llama-server: dsh finished all seven;
Seymour finished the three office tasks and both coding tasks at the
same scores, and **lost both single-shot HTML tasks to its own step
budget**. The agent step was capped at 2048 tokens (right for planning
steps, and the cap that made the empty-reply retry necessary); a
~300-line page does not fit, so every `write_file` was cut off
mid-content, the half-JSON never parsed, and the loop journaled it as a
"thought" and asked for the next step — eleven identical oversized calls
in ten minutes. The model diagnosed it correctly ("write it in parts,
then append") and had no tool to land that plan on. dsh's reply cap
simply holds a whole file.

Four changes, each with a test: the step cap is now the saved reply cap
or 8192, whichever is larger, with a 360 s step deadline to match; the
reply-cap default is 8192; `append_file` exists, and `write_file`'s
description says when to reach for it; and a call that looked like one
but never parsed comes back as a *result* saying it was cut off and how
to split — in the agent loop and in chat. The general lesson: a budget
the deliverable cannot fit is a wall the model cannot see, and the
harness has to say so in words.

Rerun of the two HTML tasks with the fixes (`compare-htmlfix`): both
9/9 checks — a 241-line solar system with a canvas, eight planets, a
speed slider and pause; a 532-line desktop with draggable, stacking
windows, a live clock, calculator, notepad and about. Time is the honest
remaining gap: 478 s and 529 s in 4–5 tool calls against dsh's 116 s and
173 s in 3, because dsh's reply is uncapped on llama-server (one whole
file plus its thinking) while Seymour's step is bounded at 8192 tokens
and writes the rest in parts. Score parity on all seven tasks; wall-clock
parity on the five that fit one reply.

## Added 2026-09-03: the MLX engine, skills, MCP presets

- **A second engine, same logic.** `engine/mlx.py` launches an MLX server
  child on Apple Silicon and sits behind the same adapter, handshake and
  scheduler as llama-server. The weights' KIND picks it: a `.gguf` file
  is llama.cpp, a checkpoint folder (config.json + safetensors) is MLX —
  in the Models tab, at boot, on Load. Two MLX servers exist because the
  two things worth having live in different programs: mlx-lm (continuous
  batching, a prefix-matching prompt cache) and mlx-vlm (MTP speculative
  decoding with the drafter checkpoint, batch-at-a-time). `auto` is a
  measured default — mlx-lm: three streams at 16.7 tok/s each with full
  overlap and a 0.56 s first token for a late arrival; mlx-vlm + MTP:
  27-32 tok/s solo but a 6.7 s first token for a late arrival (NOTES.md,
  2026-09-03) — and the Engine card can force either. What an MLX server
  lacks (/props, /slots) the adapter synthesizes from its launch flags
  and its own in-flight ledger — a request counts as busy only once
  tokens flow, so the overlap probe cannot be fooled by queueing.
- **Facts from the checkpoint, not the filename.** `engine/mlxinfo.py`
  reads config.json the way `gguf.py` reads a header: quantization, the
  context ceiling, which layers keep a growing KV cache (16 of 64 on
  Qwen3.8-27B → 64 KB per token per sequence), whether a folder is a
  drafter, and which base model a drafter belongs to (same text type and
  bits — the `-MTP-` name only ranks). The fit chip counts sequences and
  the prompt cache, which is why a 250k context reads "tight" on MLX and
  "perfect" on llama's unified pool.
- **Downloads follow the format.** Search filters by the hub's gguf/mlx
  tag; an MLX repo downloads whole, file by file, pinned to one revision,
  into the same resumable hub layout; config.json is fetched first so the
  fit verdict uses real KV numbers; the matching drafter repo is offered
  next to it.
- **Skills** (`skills.py`, ten bundled): SKILL.md folders in the Agent
  Skills format, three roots (bundled, ~/.seymour/skills, the workspace —
  the last marked untrusted), a one-line index in every prompt, bodies
  loaded by `use_skill`. The bundled ones encode what the comparison
  taught: verify by re-reading (csv, xlsx, pptx), check every referenced
  id and function in a single-file page, write long files in parts.
- **MCP presets** (Settings → MCP → Presets): playwright (a real browser,
  the roadmap's P2), filesystem scoped to the workspace, fetch, git,
  memory — each verified on its registry, each with its trust note.

## Measure on AC (2026-09-03)

The first end-to-end MLX load through the Models tab worked — mlx-lm
child, four sequences, caching confirmed, concurrent mode — and then a
real chat took 30 s to its first token, with decode swinging between 17
and 2 tok/s on identical requests. It was not the seed (mlx-lm makes
seeded requests non-batchable; Seymour's mlx-lm path sends none), not
the prompt cache, not the flags, not the spawn environment, not the
browser pane: a manual launch stalled identically, and a `sample` of the
server showed its generation thread waiting inside `mx.eval` while the
GPU read 0% busy and the kernel sat idle. The laptop was on battery.
macOS duty-cycles GPU compute on battery — a few seconds at full speed,
then ten to thirty at a crawl — and the earlier bench ran on AC. The
handshake now records `measured_on_battery`, logs a warning, the status
panel says "on battery · gpu duty-cycled", and the Models tab shows
"measured on battery" instead of a speedup ratio that means nothing
(16x was recorded). The MLX-side harness comparison waits for AC.

## Seeing the work (2026-09-03, afternoon)

Nick's "write a single html file of a solar system" exposed a different
kind of weakness: not what the harness could do, but what it let a person
see. The trace said it all — a whole-page `write_file` streaming as
withheld JSON, the chat showing "…", a Stop after 40 s recorded as
"done". Then a headless re-run showed the model narrating a plan for
2,244 tokens and never calling a tool, which the loop took as a finished
answer. Five changes:

- **Live tool calls.** `tools.peek_call` reads the tool name, the path and
  the decoded tail of the content out of a half-written JSON call;
  `tool_progress` frames stream ~3 times a second and the chat shows
  "writing solar-system.html · 3,120 chars · 41s" over a live code block.
- **Honest cancel.** A Stop is recorded as `cancelled`, with the partial
  call's name, path, size and head in the trace, and a truthful line in
  the thread instead of silence.
- **The intent nudge.** A reply that announces work ("Let me plan it out,
  then write it in parts") without a tool call gets exactly one more
  round with the order to act. The skill that invited the narration
  ("PLAN before writing") now says plan silently and write in the same
  reply.
- **check_page.** The person's own browser is the test runner: the tool
  publishes a probe event, the open tab loads the page in a hidden
  sandboxed iframe with a reporting shim injected at the top, and console
  errors with line numbers, ids referenced but missing, animation
  activity and visible text come back as the result. Playwright is the
  headless fallback; with neither, the tool says so. The html skill calls
  it before anything else in its verify step.
- **The Code workspace.** `/api/workspace/tree|read|write|run|run-tool`
  over the same confined workspace the tools use, with content tags so a
  person's save and the model's edit refuse to clobber each other; on top
  of it a pane (CodeMirror 6 dressed in the theme's variables) with a file
  tree, tabs, Save, a sandboxed Preview, Output (the model's own sandbox),
  Live (the call being written, in full) and Diff (what each edit changed).
  It mounts beside the chat, never as a view: switching views aborts the
  chat stream and the server reads that as Stop.
- **Fenced file content.** A whole page inside a JSON string is one stray
  quote away from an unparseable call (measured: 4,760 tokens, 330 s,
  lost). Content may now follow the call as a fenced block, verbatim; the
  catalog prefers it, the live view shows it, the executor accepts it.

## Known working, by construction (2026-09-03, evening)

Nick's correction, seeing the Code pane: the code belongs in the chat,
and Seymour must know whether what it wrote runs. Two mechanisms:

- **The code card.** Each file a run writes is a card in the reply: it
  streams as the call is generated (content deltas, ~3 frames/s), then
  settles into the file as it is on disk with Code | Preview, open / edit
  / re-check, and a verdict badge. The verdict is the harness's, never
  the model's claim.
- **auto_check + the repair guard.** After every file-writing tool the
  harness runs the check for the file's kind — `check_page` in the
  person's browser for pages (errors with line numbers, missing ids,
  animation frames, canvas pixel coverage), `py_compile`, `node --check`,
  a JSON parse — and appends the report to the tool result. When the
  model tries to finish while a page still says FIX NEEDED, the loop
  sends it back with the report, twice at most. A check that cannot run
  says "not measured" and never blocks or passes.

## The parser was the bug (2026-09-03, evening)

Every "whole-page write_file failed to parse" this project logged today
had one cause, and it was ours: the in-band call parser located the JSON
object by counting braces without skipping string contents, so the first
CSS rule inside a page's content closed the object early and `json.loads`
reported an unterminated string. The 4,760-token solar page (330 s) and a
36-line clock (twice) died the same way, and the repair message blamed
the reply cap. The scanner is now string-aware, `json.loads` runs with
`strict=False` (raw control characters inside strings) and `\'` is
repaired to `'`; the repair message tells a truncated call from a
malformed one and points the latter at the fenced form; and a reply that
prints code as prose when a file was asked for gets one round to write
it. Lesson, again: when every model attempt fails the same check, suspect
the check.

## Two silent successes (2026-09-03, night)

The first live run of the code card found the last two holes, both
"honest by a narrow definition": a fenced-form write whose fence did not
attach created a **0-byte page**, and `check_page` **passed it** — no
console errors, four elements. Now `write_file`/`append_file` refuse
empty content and name the remedy, `check_page` fails an empty page
(html+head+body+shim is not a page), the fence parser tolerates trailing
prose and a missing closer, repair events keep the whole failed call for
diagnosis, and a salvage path recovers a write whose JSON is broken only
inside its content string — the auto-check then judges what was written.
The rule these share: a tool's success must mean what the person would
mean by it.

## What was deliberately not done (yet)

- **A real browser.** omp's browser tool is Puppeteer plus stealth
  scripts plus Playwright's ARIA snapshot bundle — a Chromium download
  and a large surface. dsh has none; its "browsing" is HTTP search and
  fetch, which is what Nick pointed at. Seymour's fetch flags pages that
  render only in JavaScript; a Playwright-backed `browse` is the P2 if
  those turn out to be common.
- **Native tool calling through llama-server.** The server runs with
  `--jinja`, so Qwen's template could carry `tools` and parse
  `<tool_call>` itself. The owned in-band parser stays for now because it
  is the one place malformed output is repaired and logged; switching is
  an eval-gated experiment, not a refactor.
- **Full hashline** (block ops, registers, tree-sitter boundaries),
  **compaction** (prune → elide → summarize), **background jobs**, and
  **subagents**. Each is scoped in the brief; none is needed for the
  eval set to turn green.

## How it is judged

`evals/run.py` v5 adds a *coding* category: fix a failing self-test by
running and editing (chat and agent), find a file by grep, read a page
with focus, report an exit code honestly. Model-free proofs live in
`tests/test_tools.py` and `tests/test_engine_slots.py`: the sandbox
boundary, stale-tag refusal, seen-lines reveal, no-op detection,
exit/timeout independence, spill, extraction, focus, private-address
refusal, and the slot-affinity rules above.

Numbers (2026-09-02, Qwen3.6-35B-A3B-Q8_0, concurrent mode): the five
coding rows 5/5 on their first run; the full 27-row set 23/27 before the
slot fix, with the 22 original rows at their historical median of 19/22;
after the fix the four misses reran 3/4 — 26 of 27 rows green across the
two runs. The one red row, `agent-list`, is the pre-existing "asks a
question instead of finishing" flake (the model had the complete list and
asked which format was wanted).

## The overhaul (2026-09-11)

The author's verdict — Seymour did not feel like Claude Code or dsh, the
UI was bad, performance was poor — had specific causes, and the day's
work is organised by them. Everything below has tests (172 in all) and
ships in the same one-registry / one-executor / append-only-log spine.

- **The context economy** (`seymour/context/`). The twelve-tool cap
  existed because the harness could not survive a long run, so it
  forbade one. Now a result over 16 KB is spilled whole to an artifact
  and excerpted (head 2/3, tail 1/3, a pointer read_file can follow);
  under pressure (40 % of the loaded context) superseded and stale
  reads, superseded write results and old spill excerpts are blanked in
  place; at 70 % the oldest tool-pair-balanced range is summarized by
  the model into a structured block (a mechanical ledger when the model
  cannot) with the last three pairs verbatim and the todo plan re-stated
  exactly. Every decision is a `context` RunEvent; the chat shows a
  one-line note. Chat's budget is 60 tool calls / 90 rounds; a
  twenty-call run on an 8k context is a test. The agent loop got the
  same spill and a character budget on its journal tail.
- **Agency primitives** (`seymour/tools/`): `todo_write` (the plan kept
  by the harness, shown as a panel, survives compaction), `glob`,
  `read_structure` (declarations with line numbers, bodies elided),
  `run_in_background` / `job_output` / `job_kill` (localhost networking
  allowed inside the sandbox so a dev server can serve), a persistent
  `shell` (one confined /bin/sh per run), `git_overview` /
  `git_file_diff` / `git_hunk`, `read_image` (attaches the picture when
  vision is measured, says so when not), `ask_user_question` (one
  question in the thread, answered through the run's route) and `task`
  / `tasks` (subagents on a Target / Change / Acceptance contract:
  bounded summary plus the full journal on disk; a child cannot spawn
  children). `tools/context.py` carries the run scope.
- **Verifiers finished** (`seymour/verify/`): workbooks are RECALCULATED
  through LibreOffice and the values read back (error literals and empty
  formulas fail); decks and documents are rendered to PNG (soffice → PDF
  → PyMuPDF, plus a contact sheet) and checked for empty placeholders,
  overflow and slide count; pages can be DRIVEN through Playwright
  (`check_page(interact=…)`: click / type / drag / expect, each step
  reporting whether the DOM changed) and an interaction failure
  overrides a clean load. `auto_check` covers .xlsx / .pptx / .docx.
- **The UI**: the Code pane's editor has a 480 px floor, the side panel
  and tree collapse via container queries on the pane, every column is
  drag-resizable and persisted, and the avatar column steps aside when
  the shell cannot hold chat + editor. Every tool call is a card in the
  thread (terminal with the exit code, diff with the server-computed
  +/- and unified diff, search, result) beside the code card; the plan
  panel; the question card. Runs are DETACHED (`seymour/live_runs.py`):
  a closed tab no longer cancels a run, Stop is an explicit route, and a
  reopened conversation re-attaches to the coalesced replay.
- **Skills**: xlsx, pptx and code-fix-loop rewritten with a worked
  example and a required verification step; `docx-python` and
  `data-analysis` added. **MCP**: `~/.seymour/mcp.json` mounts servers;
  Settings shows each tool's real schema.
- **The eval set v2** (`evals/compare/tasks.py`), the renderer
  (`evals/render/`) and the judge packets (`evals/judge/`); Seymour's
  side of the comparison now runs through the chat executor.
- **Measured**: the prompt cache hits (mean 84 % over a run, 91–98 % on
  every round after the cold one — `cache_n` from llama-server, per
  round in the trace); the parser was dropping flat-shaped arguments
  and a round could spend its whole cap thinking (both fixed, both
  found by the first smoke run of the new set). **The model profile**
  (`engine/profile.py`) measures tools-in-template, the thinking channel
  and the system prompt's token cost per model and drives the defaults.
- **Deliverables made by commands are verified too** (2026-09-11, late).
  The first xlsx eval run wrote its workbook through python thirty
  times and the xlsx verifier never fired — auto_check followed only the
  file tools. The executor now snapshots the workspace's .xlsx / .pptx /
  .docx / .html before `run_command` or `shell` and verifies what the
  command changed; the repair guard follows those files. And `soffice`
  cannot run inside the sandbox (exit 0, no output, 0.17 s — measured),
  so `verify_file` runs the harness's verifiers on demand, outside it,
  and the skills call that instead of soffice.
