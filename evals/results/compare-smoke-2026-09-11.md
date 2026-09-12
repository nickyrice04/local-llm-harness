# Harness comparison — smoke — 2026-09-11

Model: `Qwen3.6-35B-A3B-Q8_0.gguf` on llama.cpp (build b10280-61881b1f7) (thinking on for both). Score = fraction of programmatic checks passed (task set v2, 2026-09-11). Judge = the blind vision subagent's 1–10 (evals/judge), shown separately.

| task | category | dsh score | seymour score | dsh judge | seymour judge | dsh time | seymour time | dsh tools | seymour tools |
|---|---|---|---|---|---|---|---|---|---|
| xlsx-messy-cleanup | office | — | — | — | — | — | — | — | — |
| pptx-from-data | office | — | — | — | — | — | — | — | — |
| code-bug-across-files | code | 1.00 | 0.60 | — | — | 28.6s | 124.8s | 9 | 8 |
| code-make-it-fast | code | — | — | — | — | — | — | — | — |
| html-desktop-os-verified | web | — | — | — | — | — | — | — | — |
| audit-long-horizon | code | — | — | — | — | — | — | — | — |

**dsh mean score: 1.00** over 1 tasks, 29s total.

**seymour mean score: 0.60** over 1 tasks, 125s total.

## Per-check detail

### seymour · code-bug-across-files — 0.60 (done, 124.8s, 8 tools)
- ❌ tests pass — 1 failed, 2 passed in 0.01s
- ✅ tests/test_inventory.py untouched — sha256 compared
- ✅ data/stock.csv untouched — sha256 compared
- ❌ the fix is in loader.py (the cause), not report.py (the symptom) — loader changed=False, report changed=False
- ✅ the fix is minimal (≤ 3 lines of loader.py differ) — 0 lines differ
- context economy: peak ~6051 prompt tokens · 0 compaction(s) · 0 spill(s) · 0 prune(s)
- artifact: `results/compare-smoke/seymour/code-bug-across-files/loader.py`
- final: I ran out of room before writing an answer — ask again and I'll get straight to it.

### dsh · code-bug-across-files — 1.00 (done, 28.6s, 9 tools)
- ✅ tests pass — 3 passed in 0.00s
- ✅ tests/test_inventory.py untouched — sha256 compared
- ✅ data/stock.csv untouched — sha256 compared
- ✅ the fix is in loader.py (the cause), not report.py (the symptom) — loader changed=True, report changed=False
- ✅ the fix is minimal (≤ 3 lines of loader.py differ) — 2 lines differ
- artifact: `results/compare-smoke/dsh/code-bug-across-files/loader.py`
- final: **All 3 tests pass.**  ### Root cause  **File:** `inv/loader.py`, function `_int()` (line 10)  **Bug:** `value.isdigit()` returns `False` for strings with leading/trailing whitespace (e.g. `" 3"`). Th
