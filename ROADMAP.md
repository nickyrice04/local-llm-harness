# Seymour — Road to Claude-Code-class agency (written 2026-09-02)

*What is still missing, ranked by how much it moves the agent, with the
honest reason each item is where it is. Companion to HARNESS.md (what was
built) and NOTES.md (what was measured).*

## Where Seymour stands

Seymour can now browse (guarded search + focused fetch), edit code by
line with a freshness tag, run and test code inside a kernel-enforced
sandbox, and do all of that from a chat turn or a background task, with
every step traced. On the coding evals a 35B fixes a failing test in
four tool calls. What separates that from Claude Code / dsh / omp is not
one feature — it is the layer of *economy and reach* around the tools.

## The gaps, ranked

1. **Context economy (prune → elide → compact).** Every tool result stays
   in the prompt forever; a 30-turn coding session on a 35B will hit the
   history budget and start forgetting. omp elides uneventful results
   ("[Uneventful result elided]"), blanks superseded reads, spills long
   results to artifacts, and summarizes the oldest tool-pair-balanced
   range when pressure rises; dsh does the same with a lock bracket in
   the log. Seymour now has the history budget (drops oldest turns) and
   spill files for commands, but no pruning or summarization. **This is
   the single largest gap for medium-length tasks.** ~3–4 days.
2. **MCP client.** Claude Code and dsh get their long tail (GitHub,
   databases, browsers, Slack…) from MCP servers mounted as native tools
   named `mcp__<server>__<tool>`. dsh's client (`packages/mcp/mcp-client`)
   is the template: stdio or streamable-HTTP transport, tools only, a
   supervisor with backoff, `listTools` → register, `callTool` with a
   timeout, names normalized deterministically. Seymour's registry is one
   dict, so mounting is ~200 lines of Python (JSON-RPC over stdio) plus a
   Settings card. Approval tier "exec" for everything MCP. ~2 days.
3. **Native tool calling (eval-gated).** llama-server runs `--jinja`; Qwen's
   template can carry `tools` and parse `<tool_call>` itself. In-band JSON
   works but costs the model a learned protocol. Run the coding evals both
   ways; keep whichever scores higher. ~1 day to wire, evals decide.
4. **Background jobs.** Long commands (installs, servers, builds) block a
   run today. dsh's job runtime: `run_in_background` → job id,
   `job_output` consuming reads, `job_kill`, completion delivered as a
   message. ~2 days.
5. **Structural read summaries + full hashline.** A 1,500-line module
   should read as declarations with bodies elided (`{ … }`) and the model
   re-reads only the ranges it needs; block-level edits (`PUT N*:`) need
   tree-sitter. Big for large codebases, irrelevant for the workspace
   sizes evals use today. ~3–4 days.
6. **Subagents (the contract, not the fleet).** omp's `task` batch —
   blank context, `# Target / # Change / # Acceptance`, bounded summary +
   full artifact — is how parallel research and multi-file changes scale.
   Seymour's scheduler already has the tiers; what's missing is the loop
   that spawns a child run and folds its report back. ~4–5 days.
7. **A real browser.** Playwright-Python behind a `browse` tool
   (observe → click/fill → extract/screenshot), used only when
   `fetch_page` reports a JavaScript-only page. Chromium download, large
   surface, P2. ~5 days.
8. **Approval policy patterns.** omp's `bash.patterns` allow/prompt/deny
   with per-segment matching; today Seymour asks once per run for any
   write/exec. Fine for chat, coarse for long agent tasks. ~1 day.

## Measuring instead of hoping

The old set proves the harness doesn't lie (junk research declines,
dead URLs fail honestly, tools fire when needed). It does not measure
*how good* the agent is. `evals/compare/` does: the same seven office /
web / code jobs through Seymour and through deepseek-harness on the same
llama-server, scored by the same programmatic checks (a workbook's
formulas, a deck's slide count and notes, a single-file HTML app's
features and self-containment, a test suite going green), with wall
time and tool-call counts, and every HTML output linked for a human to
open side by side — because mechanical checks cannot see whether the
solar system is beautiful.

Public tests worth borrowing prompts from (researched 2026-09-02):
Artificial Analysis's "LLM as Designer: Self-Evolving OS" microeval (the
"os.html" test), the Single-File Test paper (arXiv 2605.06707) and the
`Arnie936/llm-prompting-tests` set (orbital sims, tower defense, pixel
editors — one-shot single-file apps), and for office work PPTArena
(PowerPoint editing), SpreadsheetBench 2 and MBABench/WorkstreamBench
(end-to-end spreadsheet workflows), OfficeBench and OmegaUse-OfficeVal
(multi-app office tasks). Seymour's set is the small, local, runnable
cousin of those.

## The model is a variable too

Qwen3.6-35B-A3B is the development model, not the ceiling. The Inference
settings (temperature, top-p/k, min-p, penalties, thinking on/off/auto
with a token budget, reply cap, history budget) exist so the comparison
can hold the sampler fixed while the harness varies — or vary the sampler
while the harness holds. Thinking is the big lever: on for planning
steps, off for dispatch, budgeted when a step rabbit-holes.

## Measured 2026-09-02 (evening): the first comparison

`evals/results/compare-first-*.md` and `compare-htmlfix-*.md`. Same seven
tasks, same model, same checks: dsh 7/7 (539 s total); Seymour 5/7 on the
first run — both single-shot HTML pages timed out on a 2048-token step
cap — then 7/7 after the step cap, `append_file` and the cut-off notice
landed (HARNESS.md). Remaining measured gap: single-shot pages take
3–4× dsh's wall-clock because Seymour bounds each step and writes in
parts. Candidates, in order: a larger step floor when the goal names a
file (cheap, eval-gated); a per-step thinking budget so a long think
cannot crowd out the file (llama-server honors `reasoning_budget`); and
the context-economy work above, which is what makes bigger steps safe.

## 2026-09-03: MLX, skills, presets — what moved and what is next

Shipped: the MLX engine (mlx-lm by default, mlx-vlm + MTP on request),
backend-aware Models tab and downloads, skills (ten bundled), MCP presets
including a real browser through `@playwright/mcp` — which closes the
"real browser" item above by delegation rather than by building one.

Next, in order:
1. **Measure APC on mlx-vlm** (the adapter sets `APC_ENABLED=1`; the
   bench ran without it) and MTP with concurrency > 1 in Seymour's own
   handshake, then revisit whether "MTP on" should also raise
   `--max-num-seqs`.
2. **The on-MLX comparison**: the same seven tasks, Seymour vs dsh, both
   on the mlx-lm server (`evals/compare/run.py --dsh-base-url
   http://127.0.0.1:8082/v1`). Score parity is the bar again; expect
   3-4x the wall-clock of the 35B-A3B GGUF (dense 27B).
3. **Search quality for MLX repos**: the hub's `mlx` tag is noisy (GGUF
   repos carry it); filter by the expanded config's model_type and
   `quantization_config.bits`, as the download report specifies.
4. **Per-file resume is whole-file** on huggingface_hub 1.x; a cancelled
   5 GB shard restarts. Acceptable, documented; a chunked downloader is
   not worth owning.
5. **Skills that reach further**: a `docx` skill (python-docx is not
   installed), a `verify-page` helper that runs a headless load when
   Playwright is present, and workspace skills opt-in from Settings.

## Shipped 2026-09-03 (afternoon): seeing the work

Live tool-call view, honest cancel, the intent nudge, `check_page` in the
person's own browser, the Code pane beside the chat, fenced file content.
What this round measured and left: (1) chat runs are still bound to their
SSE consumer — the Code pane follows them over the bus, but a closed tab
still cancels; detaching runs (frames over the bus keyed by run_id, an
explicit cancel route) is the next structural step and also what lets a
run's diff and result cards appear anywhere; (2) tool results never reach
the chat as cards (only the trace) — dsh's diff card and terminal card are
the model to copy; (3) Playwright as an optional extra for headless
`check_page` in the eval runner; (4) a resumable streaming-JSON extractor
(oh-my-pi's) if the per-frame regex peek ever shows in profiles.

## 2026-09-11: after the overhaul — what is now left

Shipped today (HARNESS.md, "The overhaul"): the context economy and the
60-call budget, the agency primitives (todo, glob, structure, jobs, the
persistent shell, git, read_image, ask_user_question, subagents), the
office and interaction verifiers, the renderer and the judge packets,
the Code-pane layout fix, tool cards, detached runs, the rewritten
skills, the MCP config file, the measured cache-hit rate, the model
profile, and the second eval set. The list above is superseded; what
remains, ranked:

1. **Native tool calling, eval-gated.** The profile now measures whether
   the template accepts `tools`; the experiment itself (run the v2 set
   both ways, keep the winner, write the number in NOTES.md) is not done.
   The in-band parser is the one place malformed calls are repaired and
   logged, and today it also accepts the flat shape; switching is a
   day's work plus a full eval run. ~1 day.
2. **Two engines at once** — the MoE on llama.cpp for the loop, the
   dense VLM on mlx-vlm for `read_image` and page screenshots — gated on
   the hwfit budget. Not on the critical path (the judge runs outside
   Seymour). ~2 days.
3. **Parallel dispatch of independent read-only calls.** Three reads in
   a row are three round trips; the executor handles one call per
   reply. Needs a multi-call reply shape the model reliably produces,
   which is the native-calling question again. ~1 day after (1).
4. **Per-step thinking budget** (on for planning, off for dispatch and
   writes): the chat run now turns thinking off after a round that spent
   its whole cap thinking; the finer per-call policy is still to do.
   ~half a day.
5. **Speculative decoding on llama.cpp** (`--model-draft` with a small
   Qwen): measure the accept rate, keep it only above 40 %. ~half a day.
6. **`check_page` for a served URL** (a dev server started with
   run_in_background): the browser probe injects into workspace files
   only; the Playwright path could take a URL today with a small change.
7. **Cards for reopened conversations**: a re-attached live run replays
   its cards; a finished one shows only the persisted prose — the trace
   has the rest. Rebuilding cards from the run log is ~half a day.
8. **Structural read with tree-sitter** only if the regex outline proves
   insufficient on real repos (it handles py/js/ts/md/html/css today).
