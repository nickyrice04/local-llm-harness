# The Seymour eval set

"Best possible harness" is meaningless without a measuring stick. This is
the stick: a FIXED list of representative tasks with programmatic pass
criteria, run against the real app over HTTP with the real local model
(Qwen3.6-35B-A3B-Q8_0). The harness rebuild is judged by these numbers —
before and after, same set, same model.

## Rules

- **The set is frozen.** Changing a task or a criterion invalidates every
  earlier result. Additions/changes bump `EVAL_SET_VERSION` in `run.py`,
  and comparisons are only valid within one version.
- **Criteria are programmatic.** A task passes when its checks pass —
  file contents, substring/regex on replies, tool-round counts, run
  statuses, slot states. No impressions, no judge model.
- **Failures are results.** A known-red row (e.g. "contentless research
  should be declined" — the 4.4 rule that isn't built yet) stays in the
  set as the target the rebuild must turn green.
- **Conditions are recorded.** Battery state and mode matter on this
  machine (prefill throttles on battery); every results file records them.

## Categories (22 tasks)

| Category | Tasks | What it proves |
|---|---|---|
| tool-use | 5 | web search/fetch fire when needed — and DON'T when not; results get synthesized, not dumped |
| multi-step | 5 | agent mode plans, uses tools, writes real files with the right content |
| long-context | 3 | facts survive long documents (start/middle/end + a needle) and multi-turn conversations |
| research | 3 | deep research produces sourced reports and delivers them into the conversation |
| recovery | 6 | broken URLs, absent tools, contention, cancellation, and junk prompts fail HONESTLY |

## Running

```bash
.venv/bin/python evals/run.py --label baseline
```

Requires the app on port 8765 with a model loaded. Results land in
`evals/results/<label>-<date>.json` (full per-check detail) and a summary
prints as numbers. Runtime is dominated by the local model: expect
20–45 minutes. Eval artifacts in the workspace are prefixed `eval-` and
cleaned at the start of every run.

## The harness comparison (evals/compare/)

The frozen set above proves the harness does not lie. It does not measure
*how good* the agent is — "remember my cat is Waffles" is a memory check,
not a capability check. `evals/compare/` is the capability check: the
same seven jobs run through Seymour and through deepseek-harness (its
published Python SDK, pointed at Seymour's own llama-server through
`DEEPSEEK_BASE_URL`), on the same model, scored by the same programmatic
checks, with wall time and tool-call counts side by side.

| task | category | what the checks open |
|---|---|---|
| csv-revenue-column | office | the CSV: revenue header, TOTAL row, sum = 5480 |
| xlsx-budget-workbook | office | openpyxl: two sheets, SUM formulas, bold header, 5 rows |
| pptx-five-slides | office | python-pptx: exactly 5 slides, text on each, notes on two, on topic |
| html-solar-system | web | parses, self-contained, canvas, rAF, 8 planets named, slider, pause |
| html-desktop-os | web | parses, self-contained, drag handlers, clock timer, 3 apps, z-index |
| code-fix-and-test | code | `pytest` green; tests untouched |
| code-multifile-refactor | code | `app/env.py` exists; `pytest` green; imports rewired |

Score = fraction of checks passed (a "how good", not a pass/fail). Both
harnesses run with the model's thinking ON — llama-server ignores dsh's
`reasoning_effort` field, so parity means Seymour's agent path (thinking
on by default) against dsh's agent. Every produced file is copied to
`results/compare-<label>/<harness>/<task>/` and linked from the report,
because mechanical checks cannot see whether the solar system is
beautiful — open the two side by side.

```bash
.venv/bin/python evals/compare/run.py --label first            # both harnesses
.venv/bin/python evals/compare/run.py --harness seymour --only html-desktop-os
```

Requires: the app with a model loaded, and the dsh SDK venv (`DSH_VENV`,
default: the scratch venv created 2026-09-02 with `uv pip install
deepseek-harness-sdk`).

**Checker bug, 2026-09-02 (the third in this project's history).** The
csv task's "total" check expected 5480; the six rows sum to 3280, and both
harnesses computed 3280. Corrected in `tasks.py`; `evals/compare/rescore.py
--label <label>` re-runs the current checks over a finished run's saved
artifacts and regenerates its report, so a fixed check never needs the
models re-run. Checks that need a whole workspace (pytest) keep their
recorded result.

**Running a subset.** `run.py --harness seymour --only html-solar-system,
html-desktop-os --label htmlfix` reruns two tasks through one harness
under a new label; every label keeps its own report and artifact folder,
so a fix is judged against the run that exposed the problem, never by
overwriting it.

**Reply caps are not equal, and the report should be read knowing it.**
dsh's SDK sends no `max_tokens` unless configured (its cap is
"adapter-owned"; on llama-server that means uncapped — a whole file plus
its thinking in one reply). Seymour's agent step is capped at the saved
reply cap or 8192 tokens, thinking included, and writes anything longer
in parts with `append_file`. That is a deliberate bound (a step that
never ends is the failure it prevents), and it costs Seymour extra steps
on single-shot pages — visible as more tool calls and more seconds on the
HTML rows, not as lower scores once the parts land.

**The runtime line.** Static checks say a page *contains* a canvas and a
pause button; they cannot say it *runs*. Opening the first run's pages by
hand found one that threw at load and had passed 9/9. The runner now
records a `runtime (headless load)` line per HTML task — error count per
page from a headless Chromium — beside the score, or "not measured" when
Playwright is absent. To enable it on this machine:

    .venv/bin/pip install playwright && .venv/bin/playwright install chromium

**Which engine the comparison runs on.** The runner reads the engine's
name from `/api/status` for the report header, and `--dsh-base-url` points
dsh at the same server Seymour is using (llama-server on 8080, the MLX
server on 8082), so an MLX run compares both harnesses on identical
weights and identical serving.

## The second comparison set (2026-09-11)

The seven-task set above retired at 7/7 for both harnesses — a test
everything passes measures nothing. `evals/compare/tasks.py` is now the
second set (the first lives on as `tasks_v1.py` for rescoring old
labels); the architecture is unchanged and the calibration target is
40–70 % for the current harness:

| task | category | what the checks do |
|---|---|---|
| xlsx-messy-cleanup | office | recalculate through LibreOffice; compare every region×month / region / grand aggregate against pandas truth computed from the seeded generator; sheets, live formula count, chart, notes |
| pptx-from-data | office | 6 slides, a native chart carrying the real numbers, notes on every slide, the computed key numbers, ≤ 4 font sizes, the harness's own overflow/placeholder check |
| code-bug-across-files | code | tests pass; tests and data untouched; the fix is in loader.py (cause) not report.py (symptom); ≤ 3 changed lines; a second test the tempting wrong fix breaks |
| code-make-it-fast | code | bench.py untouched; identical checksum; ≥ 5× and ≥ 20× faster than the original (timed by the checker) |
| html-desktop-os-verified | web | static checks, then Playwright DRIVES the page: start menu, notepad typing, calculator 8/2=4, drag, z-order — scored at 50 / 90 / 100 % of steps |
| audit-long-horizon | code | hidden acceptance tests per planted bug (search normalisation, days_between sign, add_days off-by-one) plus one that must keep passing; the report names the files; frozen files untouched |

**Seymour's side runs through the chat executor** (`POST /api/chat`),
the path a person uses: the context economy, the 60-call budget, the
verifiers and the repair guard. The runner auto-approves writes and
answers any question with "proceed on your best judgement"; thinking is
sent on per message (parity with dsh). Each record carries the run's
peak prompt tokens, compactions, spills and prunes.

**Rendering and the judge.** Every task with a `render` field is turned
into PNGs by `evals/render/` (pages at three viewports and after each
interaction step; decks per slide plus a contact sheet; workbooks
recalculated and rasterized) into `results/compare-<label>/<harness>/<task>/render/`.
`evals/judge/packet.py --label <label>` builds blind packets (random
A/B, neutral names, the anchors verbatim); a vision-capable subagent
judges them OUTSIDE Seymour; `--collect` folds the verdicts back and the
report shows the judge's 1–10 in its own column, never averaged into
the machine score. `evals/render/calibration/` holds three hand-scored
references re-judged every run (`--calibration`, `--drift`).

```bash
SEYMOUR_BASE=http://127.0.0.1:8000 .venv/bin/python evals/compare/run.py --label v2
.venv/bin/python evals/judge/packet.py --label v2          # then judge, then:
.venv/bin/python evals/judge/packet.py --collect --label v2
```
