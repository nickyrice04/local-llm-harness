# Harness comparison — htmlfix — 2026-09-02

Model: `Qwen3.6-35B-A3B-Q8_0.gguf` on llama-server (thinking on for both). Score = fraction of programmatic checks passed.

| task | category | seymour score | seymour time | seymour tools |
|---|---|---|---|---|
| csv-revenue-column | office | — | — | — |
| xlsx-budget-workbook | office | — | — | — |
| pptx-five-slides | office | — | — | — |
| html-solar-system | web | 1.00 | 477.9s | 4 |
| html-desktop-os | web | 1.00 | 528.7s | 5 |
| code-fix-and-test | code | — | — | — |
| code-multifile-refactor | code | — | — | — |

**seymour mean score: 1.00** over 2 tasks, 1007s total.

## Per-check detail

### seymour · html-solar-system — 1.00 (done, 477.9s, 4 tools)
- ✅ solar_system.html exists — solar_system.html
- ✅ solar_system.html is a complete HTML document with script — 14 tags; script=True
- ✅ solar_system.html is self-contained — 0 external references
- ✅ uses a canvas — <canvas
- ✅ animates with requestAnimationFrame — requestAnimationFrame
- ✅ solar_system.html has ('mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune') — found ['mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune'] of ['mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'n
- ✅ has a speed slider — type\s*=\s*["\']range["\']
- ✅ has pause/play — pause|play
- ✅ solar_system.html >= 6 KB — 7438 bytes
- artifact: `results/compare-htmlfix/seymour/html-solar-system/solar_system.html`
- final: Created `solar_system.html` — a self-contained HTML file featuring a canvas-based solar system animation with the sun, all eight planets (distinct colors, sizes, and orbital speeds), labels, Saturn's 

### seymour · html-desktop-os — 1.00 (done, 528.7s, 5 tools)
- ✅ os.html exists — os.html
- ✅ os.html is a complete HTML document with script — 29 tags; script=True
- ✅ os.html is self-contained — 0 external references
- ✅ draggable windows — mousedown|pointerdown|dragstart
- ✅ live clock — setInterval|requestAnimationFrame
- ✅ os.html has ('calculator', 'notepad', 'about') — found ['calculator', 'notepad', 'about'] of ['calculator', 'notepad', 'about']
- ✅ window stacking — z-?index
- ✅ typeable notepad — textarea|contenteditable
- ✅ os.html >= 8 KB — 18874 bytes
- artifact: `results/compare-htmlfix/seymour/html-desktop-os/os.html`
- final: Created `os.html` in the workspace. It is a single, self-contained HTML file simulating a small desktop OS. Features include: - A modern dark-themed wallpaper and desktop layout - A taskbar with a liv
