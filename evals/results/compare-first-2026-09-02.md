# Harness comparison — first — 2026-09-02

Model: `Qwen3.6-35B-A3B-Q8_0.gguf` on llama-server (thinking on for both). Score = fraction of programmatic checks passed.

| task | category | dsh score | seymour score | dsh time | seymour time | dsh tools | seymour tools |
|---|---|---|---|---|---|---|---|
| csv-revenue-column | office | 1.00 | 1.00 | 24.5s | 36.1s | 3 | 3 |
| xlsx-budget-workbook | office | 1.00 | 1.00 | 46.6s | 64.1s | 5 | 4 |
| pptx-five-slides | office | 1.00 | 1.00 | 90.3s | 88.2s | 12 | 7 |
| html-solar-system | web | 1.00 | 0.00 (timeout) | 115.7s | 601.1s | 3 | 1 |
| html-desktop-os | web | 1.00 | 0.00 (timeout) | 172.8s | 600.8s | 3 | 0 |
| code-fix-and-test | code | 1.00 | 1.00 | 45.4s | 88.2s | 7 | 5 |
| code-multifile-refactor | code | 1.00 | 1.00 | 43.4s | 56.2s | 11 | 9 |

**dsh mean score: 1.00** over 7 tasks, 539s total.

**seymour mean score: 0.71** over 7 tasks, 1535s total.

## Per-check detail

### seymour · csv-revenue-column — 1.00 (done, 36.1s, 3 tools)
- ✅ sales_with_revenue.csv exists — sales_with_revenue.csv
- ✅ has a revenue header — header=['region', 'product', 'units', 'unit_price', 'revenue']
- ✅ has a TOTAL row — TOTAL row
- ✅ total is 3280 — total revenue should be 3280.00 (300+400+750+675+750+405)
- artifact: `results/compare-first/seymour/csv-revenue-column/sales_with_revenue.csv`
- final: I added the `revenue` column (units × unit_price) to every row of `sales.csv` and appended a `TOTAL` row with the sum of all revenue (3280.00). The result is saved in `sales_with_revenue.csv` and veri

### dsh · csv-revenue-column — 1.00 (done, 24.5s, 3 tools)
- ✅ sales_with_revenue.csv exists — sales_with_revenue.csv
- ✅ has a revenue header — header=['region', 'product', 'units', 'unit_price', 'revenue']
- ✅ has a TOTAL row — TOTAL row
- ✅ total is 3280 — total revenue should be 3280.00 (300+400+750+675+750+405)
- artifact: `results/compare-first/dsh/csv-revenue-column/sales_with_revenue.csv`
- final: Done. Here's a summary of the result in `sales_with_revenue.csv`:  | # | region | product | units | unit_price | revenue | |---|--------|---------|-------|------------|---------| | 2 | north | widget 

### seymour · xlsx-budget-workbook — 1.00 (done, 64.1s, 4 tools)
- ✅ budget.xlsx exists — budget.xlsx
- ✅ two sheets Budget + Notes — ['Budget', 'Notes']
- ✅ Total column uses SUM formulas — formulas present
- ✅ 5 categories + totals — 7 rows
- ✅ header is bold — A1 bold
- artifact: `results/compare-first/seymour/xlsx-budget-workbook/budget.xlsx`
- final: Created `budget.xlsx` with the 'Budget' sheet (headers, 5 expense rows, formulas for Total and Monthly Total) and 'Notes' sheet. Verified the file structure and formulas using `verify_budget.py`.

### dsh · xlsx-budget-workbook — 1.00 (done, 46.6s, 5 tools)
- ✅ budget.xlsx exists — budget.xlsx
- ✅ two sheets Budget + Notes — ['Budget', 'Notes']
- ✅ Total column uses SUM formulas — formulas present
- ✅ 5 categories + totals — 7 rows
- ✅ header is bold — A1 bold
- artifact: `results/compare-first/dsh/xlsx-budget-workbook/budget.xlsx`
- final: Here's a summary of what was created and verified:  **File:** `budget.xlsx` in `/private/tmp/seymour-compare/first/xlsx-budget-workbook/ws`  **Sheet "Budget":** | Row | Category | Jan | Feb | Mar | To

### seymour · pptx-five-slides — 1.00 (done, 88.2s, 7 tools)
- ✅ seymour_pitch.pptx exists — seymour_pitch.pptx
- ✅ exactly 5 slides — 5 slides
- ✅ every slide has text — every slide has text
- ✅ speaker notes on two slides — notes on >=2 slides
- ✅ on topic — mentions Seymour
- artifact: `results/compare-first/seymour/pptx-five-slides/seymour_pitch.pptx`
- final: Created seymour_pitch.pptx with 5 slides covering the idea, how it works, why local, and next steps. Speaker notes were added to slides 2 and 4. The file was verified to load correctly and contain the

### dsh · pptx-five-slides — 1.00 (done, 90.3s, 12 tools)
- ✅ seymour_pitch.pptx exists — seymour_pitch.pptx
- ✅ exactly 5 slides — 5 slides
- ✅ every slide has text — every slide has text
- ✅ speaker notes on two slides — notes on >=2 slides
- ✅ on topic — mentions Seymour
- artifact: `results/compare-first/dsh/pptx-five-slides/seymour_pitch.pptx`
- final: Done. `seymour_pitch.pptx` has been created and verified. Here's what's in the deck:  | Slide | Title | Content | Speaker Notes | |-------|-------|---------|---------------| | 1 | **Seymour** | Subtit

### seymour · html-solar-system — 0.00 (timeout, 601.1s, 1 tools)
- ❌ solar_system.html exists — solar_system.html
- ❌ solar_system.html is a complete HTML document with script — solar_system.html missing
- ❌ solar_system.html is self-contained — solar_system.html missing
- ❌ uses a canvas — solar_system.html missing
- ❌ animates with requestAnimationFrame — solar_system.html missing
- ❌ solar_system.html has ('mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune') — solar_system.html missing
- ❌ has a speed slider — solar_system.html missing
- ❌ has pause/play — solar_system.html missing
- ❌ solar_system.html >= 6 KB — 0 bytes

### dsh · html-solar-system — 1.00 (done, 115.7s, 3 tools)
- ✅ solar_system.html exists — solar_system.html
- ✅ solar_system.html is a complete HTML document with script — 16 tags; script=True
- ✅ solar_system.html is self-contained — 0 external references
- ✅ uses a canvas — <canvas
- ✅ animates with requestAnimationFrame — requestAnimationFrame
- ✅ solar_system.html has ('mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune') — found ['mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune'] of ['mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'n
- ✅ has a speed slider — type\s*=\s*["\']range["\']
- ✅ has pause/play — pause|play
- ✅ solar_system.html >= 6 KB — 12264 bytes
- artifact: `results/compare-first/dsh/html-solar-system/solar_system.html`
- final: The file has been created successfully at `/private/tmp/seymour-compare/first/html-solar-system/ws/solar_system.html` (388 lines). Here's a summary of what's included:  **Visual elements:** - **Sun** 

### seymour · html-desktop-os — 0.00 (timeout, 600.8s, 0 tools)
- ❌ os.html exists — os.html
- ❌ os.html is a complete HTML document with script — os.html missing
- ❌ os.html is self-contained — os.html missing
- ❌ draggable windows — os.html missing
- ❌ live clock — os.html missing
- ❌ os.html has ('calculator', 'notepad', 'about') — os.html missing
- ❌ window stacking — os.html missing
- ❌ typeable notepad — os.html missing
- ❌ os.html >= 8 KB — 0 bytes

### dsh · html-desktop-os — 1.00 (done, 172.8s, 3 tools)
- ✅ os.html exists — os.html
- ✅ os.html is a complete HTML document with script — 36 tags; script=True
- ✅ os.html is self-contained — 0 external references
- ✅ draggable windows — mousedown|pointerdown|dragstart
- ✅ live clock — setInterval|requestAnimationFrame
- ✅ os.html has ('calculator', 'notepad', 'about') — found ['calculator', 'notepad', 'about'] of ['calculator', 'notepad', 'about']
- ✅ window stacking — z-?index
- ✅ typeable notepad — textarea|contenteditable
- ✅ os.html >= 8 KB — 20599 bytes
- artifact: `results/compare-first/dsh/html-desktop-os/os.html`
- final: The file `os.html` has been created at `/private/tmp/seymour-compare/first/html-desktop-os/ws/os.html` (585 lines). Here's what's included:  **Desktop** - Dark purple gradient wallpaper - Three deskto

### seymour · code-fix-and-test — 1.00 (done, 88.2s, 5 tools)
- ✅ tests pass — 4 passed in 0.00s
- ✅ test_stats.py has ('def test_median_even',) — found ['def test_median_even'] of ['def test_median_even']
- artifact: `results/compare-first/seymour/code-fix-and-test/stats.py`
- final: The tests now pass. The bugs were:  1.  **`mean` function**: Divided by `len(xs) - 1` instead of `len(xs)`. This caused incorrect averages and also broke the `variance` function (which depends on `mea

### dsh · code-fix-and-test — 1.00 (done, 45.4s, 7 tools)
- ✅ tests pass — 4 passed in 0.00s
- ✅ test_stats.py has ('def test_median_even',) — found ['def test_median_even'] of ['def test_median_even']
- artifact: `results/compare-first/dsh/code-fix-and-test/stats.py`
- final: All 4 tests pass. Here's what was wrong in **stats.py**:  1. **`mean()` — off-by-one in denominator** (`len(xs) - 1` → `len(xs)`): The mean of `[2, 4, 6]` was returning `6.0` (12 / 2) instead of `4.0`

### seymour · code-multifile-refactor — 1.00 (done, 56.2s, 9 tools)
- ✅ app/env.py exists — app/env.py
- ✅ tests pass — 2 passed in 0.01s
- ✅ config imports env — passed when scored against the live workspace during the run (app/config.py was not among the kept artifacts then)
- artifact: `results/compare-first/seymour/code-multifile-refactor/env.py`
- final: Refactoring complete. Created `app/env.py` with the canonical `read_env` function. Updated `app/config.py`, `app/db.py`, and `app/main.py` to import from `app.env` instead of defining their own copies

### dsh · code-multifile-refactor — 1.00 (done, 43.4s, 11 tools)
- ✅ app/env.py exists — app/env.py
- ✅ tests pass — 2 passed in 0.00s
- ✅ config imports env — passed when scored against the live workspace during the run (app/config.py was not among the kept artifacts then)
- artifact: `results/compare-first/dsh/code-multifile-refactor/env.py`
- final: Both tests pass. Here's a summary of the refactoring:  1. **Created `app/env.py`** — the single canonical `read_env(name, default=None)` implementation. 2. **Updated `app/config.py`** — removed its lo
