# Harness comparison — smoke2 — 2026-09-11

Model: `Qwen3.6-35B-A3B-Q8_0.gguf` on llama.cpp (build b10280-61881b1f7) (thinking on for both). Score = fraction of programmatic checks passed (task set v2, 2026-09-11). Judge = the blind vision subagent's 1–10 (evals/judge), shown separately.

| task | category | seymour score | seymour judge | seymour time | seymour tools |
|---|---|---|---|---|---|
| xlsx-messy-cleanup | office | — | — | — | — |
| pptx-from-data | office | — | — | — | — |
| code-bug-across-files | code | 1.00 | — | 49.9s | 8 |
| code-make-it-fast | code | — | — | — | — |
| html-desktop-os-verified | web | — | — | — | — |
| audit-long-horizon | code | — | — | — | — |

**seymour mean score: 1.00** over 1 tasks, 50s total.

## Per-check detail

### seymour · code-bug-across-files — 1.00 (done, 49.9s, 8 tools)
- ✅ tests pass — 3 passed in 0.01s
- ✅ tests/test_inventory.py untouched — sha256 compared
- ✅ data/stock.csv untouched — sha256 compared
- ✅ the fix is in loader.py (the cause), not report.py (the symptom) — loader changed=True, report changed=False
- ✅ the fix is minimal (≤ 3 lines of loader.py differ) — 2 lines differ
- context economy: peak ~6129 prompt tokens · 0 compaction(s) · 0 spill(s) · 0 prune(s)
- artifact: `results/compare-smoke2/seymour/code-bug-across-files/loader.py`
- final: The bug was in **`inv/loader.py`**, specifically in the `_int` helper function.  **Root cause:** The CSV data has leading spaces in the `qty` column (e.g., `" 3"` and `" 5"`). The original code used `
