# The judge — a subagent you run, not a feature Seymour has

Seymour produces and renders (`evals/render/`). Judging "is this output
actually good?" is done OUTSIDE Seymour, by a vision-capable model with
a blank context, spawned by whoever is running the evals (the Agent
tool in Claude Code, one subagent reused across every artifact of a
run). Seymour never judges itself, and the judge never runs on the
local 27B.

`packet.py` prepares what the judge sees and nothing more:

    .venv/bin/python evals/judge/packet.py --label <label>            # every rendered task of a comparison run
    .venv/bin/python evals/judge/packet.py --calibration              # the reference artifacts, for drift

It writes `evals/results/judge-<label>/<task>/` with:

- `A/` and `B/` — the two artifacts' images, copied under neutral names
  (`01.png`, `02.png` …), **A/B assigned at random per task** and the
  mapping kept in `mapping.json` (which the judge is never shown);
- `packet.json` — the original task prompt, the rubric, the image
  paths, and the console log text for pages;
- `prompt.md` — the judge's instructions with the anchors verbatim.

Run the judge on each packet, save its strict-JSON verdict next to it as
`verdict.json`, then `packet.py --collect --label <label>` folds the
verdicts back through the mapping into `evals/results/judge-<label>.json`
(per task: seymour score, dsh score, winner) so the report can show the
machine score and the judge score as separate columns.

## The 10-point scale is anchored (put verbatim in every judge prompt)

- **10** — professional enough to present to a client with no edits.
- **7** — correct and clean, visibly machine-made.
- **4** — the content is right, the presentation is not.
- **1** — broken or empty.

## Anti-bias rules

- A/B is randomized per task; filenames are neutral; the judge is never
  told which harness produced which.
- Every criterion score must cite specific visual evidence ("slide 3's
  bullet text overflows the placeholder's right edge"). A verdict whose
  justifications do not reference what is in the images is rejected and
  re-run.
- `calibration/` holds reference artifacts with hand-fixed scores. They
  are re-judged on every run and the drift is reported; a judge whose
  calibration moved by more than 1.5 points is reporting noise and that
  run's scores are not comparable to the previous run's.
- One subagent per eval run, reused across artifacts.
