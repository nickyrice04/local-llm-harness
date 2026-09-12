"""The comparison task set, second edition (2026-09-11).

The first set (tasks_v1.py) was retired because both harnesses scored
7/7 on it — a test everything passes measures nothing. This set is built
to land the current harness at 40–70 %: each task needs reading before
acting, running things rather than only writing them, or a deliverable
whose correctness can only be established by recalculating, rendering
or driving it. The architecture is unchanged — same prompt for both
harnesses, same model, same programmatic checks, a score in [0, 1] per
task — with one addition: a `render` field names how a task's artifact
becomes images for the judge (evals/render), whose 1–10 score is
reported in its own column, never averaged with the machine score.

The six tasks and their axes:

    xlsx-messy-cleanup        a dirty CSV → a multi-sheet workbook with live
                              two-dimensional formulas, checked by RECALCULATING
                              and comparing every aggregate with pandas truth
    pptx-from-data            a CSV + a brief → a deck with a chart from the
                              numbers, notes, no overflow; the judge scores it
    code-bug-across-files     a failing test whose trace ends in one file and
                              whose cause is in another; minimal fix; a second
                              test that the tempting wrong fix would break
    code-make-it-fast         a correct, slow function with a benchmark in the
                              repo: same outputs, measurably faster, the
                              measurement run and reported
    html-desktop-os-verified  the os.html genre, checked by DRIVING it with
                              Playwright (start menu, notepad, calculator,
                              drag, z-order), then judged for looks
    audit-long-horizon        a 14-file repo with three real bugs and red
                              herrings: find, fix, report — sized to need 25+
                              tool calls, the regression test for the economy

Fixtures are generated deterministically (seeded) so the checkers can
compute ground truth independently of what the model produces.
"""

from __future__ import annotations

import csv
import hashlib
import html.parser
import io
import json
import random
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Check = Callable[[Path], tuple[bool, str]]
SET_VERSION = 2


@dataclass
class Task:
    """One job both harnesses get, verbatim."""

    id: str
    category: str                       # office | web | code
    prompt: str
    setup: dict[str, str] = field(default_factory=dict)
    checks: list[tuple[str, Check]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    timeout_s: int = 900
    # How the judge sees this task's output: {"kind": deck|page|workbook|
    # document|chart, "artifact": <relative path>, "steps": [interaction
    # steps for pages]}. None = machine checks only.
    render: dict | None = None


# ------------------------------------------------------------------ helpers
def exists(rel: str) -> tuple[str, Check]:
    return (f"{rel} exists", lambda ws: ((ws / rel).exists(), rel))


def text_has(rel: str, *needles: str, any_of: bool = False) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        hits = [n for n in needles if n.lower() in text]
        ok = bool(hits) if any_of else len(hits) == len(needles)
        return ok, f"found {hits} of {list(needles)}"
    return (f"{rel} has {needles}", check)


def regex_in(rel: str, pattern: str, label: str) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        return bool(re.search(pattern, p.read_text(encoding="utf-8", errors="replace"),
                              re.IGNORECASE | re.DOTALL)), pattern
    return (label, check)


def unchanged(rel: str, original: str) -> tuple[str, Check]:
    """The file is byte-identical to what setup placed (tests, benchmarks)."""
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()

    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        return hashlib.sha256(p.read_bytes()).hexdigest() == digest, "sha256 compared"
    return (f"{rel} untouched", check)


def html_parses(rel: str) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        text = p.read_text(encoding="utf-8", errors="replace")
        tags: list[str] = []

        class Counter(html.parser.HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append(tag)
        try:
            Counter().feed(text)
        except Exception as error:
            return False, f"parse error: {error}"
        ok = "html" in tags and "script" in tags and "body" in tags
        return ok, f"{len(tags)} tags; script={'script' in tags}"
    return (f"{rel} is a complete HTML document with script", check)


def no_external_deps(rel: str) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        text = p.read_text(encoding="utf-8", errors="replace")
        leaks = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', text, re.I)
        return not leaks, f"{len(leaks)} external references"
    return (f"{rel} is self-contained", check)


def _pytest(ws: Path, *args: str, timeout: int = 180) -> tuple[bool, str]:
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                              cwd=ws, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or ["(no output)"]
    return proc.returncode == 0, tail[0][:120]


def pytest_passes(*args: str) -> tuple[str, Check]:
    return ("tests pass" + (f" ({' '.join(args)})" if args else ""), lambda ws: _pytest(ws, *args))


# --------------------------------------------------------------- 1. xlsx
# A deliberately dirty orders file. Every row is generated from clean
# values first (the truth), then dirtied — so the checker knows the
# answer without trusting anything the model wrote.
def _orders() -> tuple[str, list[dict]]:
    rng = random.Random(20260911)
    regions = ["North", "South", "East", "West"]
    products = [("Widget", 12.5), ("Gadget", 40.0), ("Gizmo", 95.0)]
    clean: list[dict] = []
    for i in range(48):
        region = rng.choice(regions)
        product, price = rng.choice(products)
        qty = rng.randint(1, 30)
        month = rng.choice([1, 2, 3])
        day = rng.randint(1, 28)
        clean.append({"order_id": 1000 + i, "date": (2026, month, day), "region": region,
                      "product": product, "qty": qty, "amount": round(qty * price, 2)})
    rows: list[list[str]] = []
    for i, r in enumerate(clean):
        y, m, d = r["date"]
        date = [f"{y}-{m:02d}-{d:02d}", f"{m:02d}/{d:02d}/{y}", f"{d} {['Jan', 'Feb', 'Mar'][m - 1]} {y}"][i % 3]
        amount = [f"${r['amount']:,.2f}", f"{r['amount']:.2f}", f"€ {r['amount']:,.2f}"][i % 3]
        region = r["region"] if i % 7 else r["region"].lower() + " "        # case/space noise
        rows.append([str(r["order_id"]), date, region, r["product"], str(r["qty"]), amount])
    # Duplicates with one differing field (the same order id twice; the
    # second copy has a different date format only) — the truth keeps one.
    for i in (5, 17, 33):
        dup = list(rows[i])
        y, m, d = clean[i]["date"]
        dup[1] = f"{m}/{d}/{y}"
        rows.append(dup)
    # Blanks: two rows missing the amount (not orders — drop or note them).
    rows.append(["2001", "2026-02-14", "North", "Widget", "4", ""])
    rows.append(["2002", "", "South", "Gizmo", "", ""])
    rows.append(["TOTAL", "", "", "", "", "see summary"])                   # a trailing junk row
    rng.shuffle(rows)
    text = "order_id,date,region,product,qty,amount\n" + "\n".join(",".join(f'"{c}"' if "," in c else c for c in row) for row in rows) + "\n"
    return text, clean


ORDERS_CSV, ORDERS_TRUTH = _orders()


def _truth_matrix() -> dict[tuple[str, str], float]:
    """region × month → amount, from the clean rows."""
    out: dict[tuple[str, str], float] = {}
    for r in ORDERS_TRUTH:
        y, m, _d = r["date"]
        key = (r["region"], f"{y}-{m:02d}")
        out[key] = round(out.get(key, 0.0) + r["amount"], 2)
    return out


def _recalculated(ws: Path, rel: str):
    """The workbook as LibreOffice computes it (values), or None."""
    import openpyxl
    src = ws / rel
    if not src.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(["/opt/homebrew/bin/soffice", "--headless", "--norestore", "--convert-to", "xlsx",
                            "--outdir", tmp, str(src)], capture_output=True, timeout=180)
            out = Path(tmp) / src.name
            if not out.exists():
                return None
            return openpyxl.load_workbook(out, data_only=True)
        except Exception:
            return None


def _numbers_in(wb) -> list[float]:
    values = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    values.append(round(float(v), 2))
    return values


def xlsx_aggregates_match(rel: str, minimum_fraction: float) -> tuple[str, Check]:
    """Every region×month truth total (and every region total, and the
    grand total) appears as a RECALCULATED number somewhere in the
    workbook. Loose about layout, strict about arithmetic."""
    truth = _truth_matrix()
    region_totals = {}
    for (region, _month), amount in truth.items():
        region_totals[region] = round(region_totals.get(region, 0.0) + amount, 2)
    grand = round(sum(truth.values()), 2)
    wanted = list(truth.values()) + list(region_totals.values()) + [grand]

    def check(ws: Path):
        wb = _recalculated(ws, rel)
        if wb is None:
            return False, "could not recalculate (missing file or soffice failure)"
        present = set(_numbers_in(wb))
        hits = sum(1 for w in wanted if any(abs(w - p) < 0.011 for p in present))
        return hits / len(wanted) >= minimum_fraction, f"{hits}/{len(wanted)} aggregates present after recalc (grand total {grand})"
    return (f"{rel}: ≥{minimum_fraction:.0%} of region×month/region/grand aggregates match pandas truth after recalc", check)


def xlsx_has(rel: str, what: str, fn: Callable) -> tuple[str, Check]:
    def check(ws: Path):
        import openpyxl
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        try:
            return fn(openpyxl.load_workbook(p))
        except Exception as error:
            return False, f"openpyxl: {error}"
    return (f"{rel}: {what}", check)


def _formula_count(wb) -> int:
    return sum(1 for sh in wb.worksheets for row in sh.iter_rows() for c in row
               if isinstance(c.value, str) and c.value.startswith("="))


def _has_chart(wb) -> bool:
    return any(getattr(sh, "_charts", []) for sh in wb.worksheets)


def _data_rows(wb) -> int:
    """Rows on the sheet that looks like the data sheet (most rows)."""
    return max((sh.max_row - 1 for sh in wb.worksheets), default=0)


XLSX_TASK = Task(
    id="xlsx-messy-cleanup", category="office",
    prompt=("orders.csv in the workspace is an export with problems: dates in three formats, amounts with "
            "currency symbols and thousands separators, region names with inconsistent case/spaces, a few "
            "duplicate orders (same order_id, one field differs), rows with blank amounts, and a junk TOTAL "
            "row at the end. Produce orders_clean.xlsx with python (pandas and openpyxl are installed): "
            "(1) a 'Data' sheet with the cleaned rows — real dates, numeric amounts, normalised regions, "
            "duplicates removed (keep one), rows without an amount dropped, no junk row; "
            "(2) a 'Summary' sheet with LIVE Excel formulas (SUMIFS over the Data sheet) giving total amount "
            "by region (rows) and month (columns, 2026-01..2026-03), with a total per region, per month, "
            "and a grand total — formulas, not pasted numbers; (3) a bar chart of total amount by region on "
            "the Summary sheet; (4) a 'Notes' sheet stating exactly what was cleaned and how many rows were "
            "removed. Verify the workbook by recalculating it with LibreOffice "
            "(/opt/homebrew/bin/soffice --headless --convert-to xlsx) and reading the values back with "
            "openpyxl data_only=True before you finish; compare two totals against pandas."),
    setup={"orders.csv": ORDERS_CSV},
    checks=[
        exists("orders_clean.xlsx"),
        xlsx_has("orders_clean.xlsx", "three sheets incl. Data, Summary, Notes",
                 lambda wb: ({"data", "summary", "notes"} <= {s.lower() for s in wb.sheetnames}, str(wb.sheetnames))),
        xlsx_has("orders_clean.xlsx", f"Data has exactly {len(ORDERS_TRUTH)} cleaned rows",
                 lambda wb: (_data_rows(wb) == len(ORDERS_TRUTH), f"{_data_rows(wb)} rows (truth {len(ORDERS_TRUTH)})")),
        xlsx_has("orders_clean.xlsx", "Summary uses ≥ 12 live formulas",
                 lambda wb: (_formula_count(wb) >= 12, f"{_formula_count(wb)} formulas")),
        xlsx_aggregates_match("orders_clean.xlsx", 0.5),
        xlsx_aggregates_match("orders_clean.xlsx", 1.0),
        xlsx_has("orders_clean.xlsx", "has a chart", lambda wb: (_has_chart(wb), "chart objects found" if _has_chart(wb) else "none")),
        xlsx_has("orders_clean.xlsx", "Notes mentions duplicates and the removed count",
                 lambda wb: (any("duplic" in str(c.value).lower() and any(ch.isdigit() for ch in str(c.value))
                                 for sh in wb.worksheets if sh.title.lower() == "notes" for row in sh.iter_rows() for c in row if c.value),
                             "looked for 'duplic' + a number on Notes")),
    ],
    artifacts=["orders_clean.xlsx"],
    render={"kind": "workbook", "artifact": "orders_clean.xlsx"},
)


# --------------------------------------------------------------- 2. pptx
QUARTERLY_CSV = "\n".join(["month,region,revenue,customers",
                           *[f"2026-0{m},{r},{rev},{cust}" for m, r, rev, cust in [
                               (1, "North", 128000, 310), (1, "South", 84000, 240), (1, "East", 61000, 190), (1, "West", 45000, 120),
                               (2, "North", 135500, 322), (2, "South", 88900, 251), (2, "East", 58200, 185), (2, "West", 47800, 131),
                               (3, "North", 149000, 340), (3, "South", 91000, 258), (3, "East", 66500, 201), (3, "West", 52300, 140)]]]) + "\n"


def pptx_facts(rel: str, what: str, fn: Callable) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        try:
            from pptx import Presentation
            return fn(Presentation(str(p)))
        except Exception as error:
            return False, f"python-pptx: {error}"
    return (f"{rel}: {what}", check)


def pptx_mechanically_clean(rel: str) -> tuple[str, Check]:
    """The harness's own deck check (no overflow, no empty placeholder)."""
    def check(ws: Path):
        import asyncio
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from seymour.verify import office
        result = asyncio.run(office.check_pptx(p, render=False))
        return result["verdict"] == "PASS", result["report"].split("\n", 1)[0][:140]
    return (f"{rel}: no overflow, no empty placeholders", check)


def _font_sizes(prs) -> set:
    sizes = set()
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    for r in p.runs:
                        if r.font.size:
                            sizes.add(r.font.size.pt)
    return sizes


PPTX_TASK = Task(
    id="pptx-from-data", category="office",
    prompt=("quarterly.csv in the workspace has monthly revenue and customer counts by region for Q1 2026. "
            "Brief: the leadership team wants a short, presentable Q1 review deck. Create q1_review.pptx with "
            "python (python-pptx, pandas installed): exactly 6 slides — a title slide; one slide with a CHART "
            "built from the actual revenue numbers (revenue by region, or by month per region) and a one-line "
            "takeaway; one slide of key numbers (total revenue, best region, growth from January to March, "
            "computed from the data); one slide of risks or observations; one slide of next steps; a closing "
            "slide. Use one consistent visual style on every slide (one palette, same fonts and sizes, nothing "
            "from the default template showing), keep text short so nothing overflows its box, and add speaker "
            "notes to every slide. Render the deck to PDF with /opt/homebrew/bin/soffice --headless "
            "--convert-to pdf and check the page count before you finish."),
    setup={"quarterly.csv": QUARTERLY_CSV},
    checks=[
        exists("q1_review.pptx"),
        pptx_facts("q1_review.pptx", "exactly 6 slides", lambda prs: (len(prs.slides) == 6, f"{len(prs.slides)} slides")),
        pptx_facts("q1_review.pptx", "a native chart on some slide",
                   lambda prs: (any(sh.has_chart for s in prs.slides for sh in s.shapes), "chart shape found")),
        pptx_facts("q1_review.pptx", "the chart carries the real numbers",
                   lambda prs: (any(any(abs(float(v or 0) - 412500) < 1 or abs(float(v or 0) - 128000) < 1 or abs(float(v or 0) - 149000) < 1
                                        for series in sh.chart.plots[0].series for v in series.values)
                                    for s in prs.slides for sh in s.shapes if sh.has_chart), "looked for 128000/149000/412500 in chart series")),
        pptx_facts("q1_review.pptx", "speaker notes on every slide",
                   lambda prs: (all(s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip() for s in prs.slides), "notes checked")),
        pptx_facts("q1_review.pptx", "the key numbers are computed (total 1,007,200; north best; +16% growth)",
                   lambda prs: (any(n in "".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame).replace(",", "").replace(" ", "")
                                    for n in ("1007200", "1.007", "1.0m", "1007k")) and
                                "north" in "".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame).lower(),
                                "looked for the total and 'North'")),
        pptx_facts("q1_review.pptx", "one type scale (≤ 4 distinct font sizes)",
                   lambda prs: (0 < len(_font_sizes(prs)) <= 4, f"sizes {sorted(_font_sizes(prs))}")),
        pptx_mechanically_clean("q1_review.pptx"),
    ],
    artifacts=["q1_review.pptx"],
    render={"kind": "deck", "artifact": "q1_review.pptx"},
)


# ------------------------------------------------- 3. bug across files
INV_LOADER = '''"""Load an inventory export (CSV) into Item records."""

import csv

from inv.models import Item


def _int(value):
    """Integers arrive as text; anything else is left as-is for the caller."""
    return int(value) if value.isdigit() else value


def _float(value):
    return float(value.replace(",", "")) if value.replace(",", "").replace(".", "").isdigit() else value


def load(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [Item(sku=row["sku"], qty=_int(row["qty"]), price=_float(row["price"])) for row in rows]
'''
INV_MODELS = '''"""The inventory record."""

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    qty: int
    price: float
'''
INV_REPORT = '''"""Reports over inventory items."""


def total(items):
    """Stock value: sum of qty × price."""
    return sum(item.qty * item.price for item in items)


def low_stock(items, threshold=5):
    return [item.sku for item in items if item.qty < threshold]
'''
INV_TEST = '''import pytest

from inv.loader import load
from inv.models import Item
from inv.report import low_stock, total

FIXTURE = "data/stock.csv"


def test_total_from_file():
    items = load(FIXTURE)
    assert total(items) == pytest.approx(1187.5)


def test_low_stock_direct():
    items = [Item("a", 2, 1.0), Item("b", 9, 1.0)]
    assert low_stock(items) == ["a"]


def test_total_rejects_bad_qty():
    # A non-numeric quantity must FAIL LOUDLY, never be coerced or skipped:
    # silent totals are how stock counts go wrong.
    with pytest.raises(TypeError):
        total([Item("x", "abc", 1.0)])
'''
INV_DATA = "sku,qty,price\nA-100, 3,12.50\nB-200,10,40.00\nC-300, 5,150.00\n"
INV_HINT = "Note: the export tool pads quantities with a leading space on some rows; that is how the file arrives.\n"

BUG_TASK = Task(
    id="code-bug-across-files", category="code",
    prompt=("The workspace has a small package inv/ (loader.py, models.py, report.py), data/stock.csv and "
            "tests/test_inventory.py. `python -m pytest -q` has one failing test. Find the ROOT CAUSE and fix "
            "it with the smallest correct change. Do not edit the tests or the data file. All three tests must "
            "pass when you are done. Finish by stating which file had the bug and why the failure showed up "
            "where it did."),
    setup={"inv/__init__.py": "", "inv/loader.py": INV_LOADER, "inv/models.py": INV_MODELS, "inv/report.py": INV_REPORT,
           "tests/__init__.py": "", "tests/test_inventory.py": INV_TEST, "data/stock.csv": INV_DATA, "data/README.txt": INV_HINT},
    checks=[
        pytest_passes(),
        unchanged("tests/test_inventory.py", INV_TEST),
        unchanged("data/stock.csv", INV_DATA),
        ("the fix is in loader.py (the cause), not report.py (the symptom)",
         lambda ws: ((ws / "inv/loader.py").read_text() != INV_LOADER and (ws / "inv/report.py").read_text() == INV_REPORT,
                     f"loader changed={ (ws / 'inv/loader.py').read_text() != INV_LOADER }, report changed={ (ws / 'inv/report.py').read_text() != INV_REPORT }")),
        ("the fix is minimal (≤ 3 lines of loader.py differ)",
         lambda ws: (_diff_lines(INV_LOADER, (ws / "inv/loader.py").read_text()) <= 3,
                     f"{_diff_lines(INV_LOADER, (ws / 'inv/loader.py').read_text())} lines differ")),
    ],
    artifacts=["inv/loader.py"],
)


def _diff_lines(a: str, b: str) -> int:
    import difflib
    return sum(1 for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
               if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


# ------------------------------------------------------ 4. make it fast
TEXTSTATS = '''"""Word statistics over a corpus. Correct, and slow on purpose."""

import re

_WORD = re.compile(r"[a-z']+")


def words(text):
    return _WORD.findall(text.lower())


def top_words(text, n=10):
    """The n most frequent words (ties broken alphabetically), with counts."""
    ws = words(text)
    unique = []
    for w in ws:
        if w not in unique:                 # O(n) membership on a list, per word
            unique.append(w)
    counts = []
    for w in unique:
        counts.append((w, ws.count(w)))    # O(n) count, per unique word
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return counts[:n]


def bigrams(text, n=10):
    """The n most frequent adjacent word pairs."""
    ws = words(text)
    pairs = [(ws[i], ws[i + 1]) for i in range(len(ws) - 1)]
    unique = []
    for p in pairs:
        if p not in unique:
            unique.append(p)
    counts = [(p, pairs.count(p)) for p in unique]
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return counts[:n]
'''
BENCH = '''"""Benchmark textstats on a fixed synthetic corpus. Run: python bench.py
Prints the wall time and a checksum of the results — the checksum must not change."""

import hashlib
import random
import time

import textstats

rng = random.Random(7)
# Letter-only pseudo-words (the tokenizer keeps [a-z']+), a few common words weighted up.
VOCAB = ["".join(chr(97 + (i // 26 ** k) % 26) for k in range(3)) for i in range(300)] + ["the", "of", "and", "a", "to"] * 10
CORPUS = " ".join(rng.choice(VOCAB) for _ in range(15_000))

t0 = time.perf_counter()
top = textstats.top_words(CORPUS, 20)
big = textstats.bigrams(CORPUS, 20)
ms = (time.perf_counter() - t0) * 1000
digest = hashlib.sha256(repr((top, big)).encode()).hexdigest()[:16]
print(f"ms={ms:.1f} checksum={digest}")
'''

FAST_TASK = Task(
    id="code-make-it-fast", category="code",
    prompt=("textstats.py in the workspace is correct but slow; bench.py times it on a fixed corpus and prints "
            "ms=… and a checksum of the results. Run the benchmark first and note the time. Then make "
            "textstats.py measurably faster WITHOUT changing its results (the checksum must stay identical), "
            "run the benchmark again, and finish by reporting the before and after times in milliseconds and "
            "the checksum. Do not edit bench.py."),
    setup={"textstats.py": TEXTSTATS, "bench.py": BENCH},
    checks=[
        unchanged("bench.py", BENCH),
        ("results identical (checksum unchanged)", lambda ws: _bench_checksum_matches(ws)),
        ("at least 5× faster than the original", lambda ws: _speedup(ws, 5.0)),
        ("at least 20× faster than the original", lambda ws: _speedup(ws, 20.0)),
    ],
    artifacts=["textstats.py"],
    timeout_s=900,
)


def _run_bench(ws: Path, module_text: str | None = None) -> tuple[float, str] | None:
    """(ms, checksum) of bench.py in ws — or of the ORIGINAL textstats when
    module_text is given (run in a temp copy so nothing in ws changes)."""
    root = ws
    tmp = None
    if module_text is not None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "textstats.py").write_text(module_text)
        (root / "bench.py").write_text(BENCH)
    try:
        proc = subprocess.run([sys.executable, "bench.py"], cwd=root, capture_output=True, text=True, timeout=600)
        m = re.search(r"ms=([\d.]+) checksum=(\w+)", proc.stdout)
        return (float(m.group(1)), m.group(2)) if m else None
    except subprocess.TimeoutExpired:
        return None
    finally:
        if tmp:
            tmp.cleanup()


_ORIGINAL_BENCH: dict = {}


def _original() -> tuple[float, str] | None:
    if "value" not in _ORIGINAL_BENCH:
        _ORIGINAL_BENCH["value"] = _run_bench(Path("."), TEXTSTATS)
    return _ORIGINAL_BENCH["value"]


def _bench_checksum_matches(ws: Path) -> tuple[bool, str]:
    now = _run_bench(ws)
    ref = _original()
    if now is None or ref is None:
        return False, "bench did not run"
    return now[1] == ref[1], f"checksum {now[1]} vs original {ref[1]}"


def _speedup(ws: Path, factor: float) -> tuple[bool, str]:
    now = _run_bench(ws)
    ref = _original()
    if now is None or ref is None:
        return False, "bench did not run"
    ratio = ref[0] / max(now[0], 0.01)
    return ratio >= factor and now[1] == ref[1], f"{ref[0]:.0f} ms → {now[0]:.0f} ms ({ratio:.1f}×)"


# ---------------------------------------------- 5. desktop, verified
OS_STEPS = [
    "expect #start-button visible",
    "click #start-button",
    "expect #start-menu visible",
    "click [data-app=notepad]",
    "expect #notepad-text visible",
    "type #notepad-text hello from the eval",
    "expect #notepad-text visible",
    "click #start-button",
    "click [data-app=calculator]",
    "expect #calc-display visible",
    "click [data-key='8']",
    "click [data-key='/']",
    "click [data-key='2']",
    "click [data-key='=']",
    "expect #calc-display text 4",
    "drag .window .titlebar 120 60",
    "expect changed",
    "click #start-button",
    "click [data-app=about]",
    "click .window",
    "expect changed",
]


def interactions_pass(rel: str, steps: list[str], minimum: float) -> tuple[str, Check]:
    """Drive the page with Playwright; the fraction of steps that pass."""
    def check(ws: Path):
        import asyncio
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from seymour.verify import web
        report = asyncio.run(web.run_interactions(p, steps))
        if report is None:
            return False, "Playwright not available"
        passed = sum(1 for s in report.steps if s.ok)
        failed = [s.step for s in report.steps if not s.ok][:3]
        return passed / max(len(report.steps), 1) >= minimum, f"{passed}/{len(report.steps)} steps passed" + (f"; first failures: {failed}" if failed else "")
    return (f"{rel}: ≥{minimum:.0%} of interaction steps pass", check)


def no_console_errors(rel: str) -> tuple[str, Check]:
    def check(ws: Path):
        import asyncio
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from seymour.verify import web
        report = asyncio.run(web.run_interactions(p, ["wait 1000"]))
        if report is None:
            return False, "Playwright not available"
        return not report.console_errors, f"{len(report.console_errors)} console errors"
    return (f"{rel}: no console errors at load", check)


OS_TASK = Task(
    id="html-desktop-os-verified", category="web",
    prompt=("Create os.html in the workspace: a single self-contained HTML file (vanilla HTML/CSS/JS, no "
            "external resources) that simulates a small desktop operating system: a wallpaper, a taskbar with "
            "a live clock and a start button (id=\"start-button\") that toggles a start menu (id=\"start-menu\") "
            "listing apps as elements with data-app=\"notepad\", data-app=\"calculator\" and data-app=\"about\"; "
            "clicking an app opens it in a draggable window (class=\"window\", dragged by its child with "
            "class=\"titlebar\") that can be closed and minimised; clicking a window brings it to the front. "
            "The notepad has a textarea id=\"notepad-text\". The calculator has a display id=\"calc-display\" "
            "and buttons with data-key for the digits, operators (+ - * /), \".\", \"=\" and \"C\", and must "
            "compute correctly (8 / 2 = 4). Make it look clean and modern. Write the file, then verify it "
            "actually works (check_page with interaction steps, or a headless browser) before you finish."),
    checks=[
        exists("os.html"),
        html_parses("os.html"),
        no_external_deps("os.html"),
        regex_in("os.html", r'id\s*=\s*["\']start-button["\']', "has the start button id"),
        regex_in("os.html", r"z-?index", "window stacking"),
        no_console_errors("os.html"),
        interactions_pass("os.html", OS_STEPS, 0.5),
        interactions_pass("os.html", OS_STEPS, 0.9),
        interactions_pass("os.html", OS_STEPS, 1.0),
    ],
    artifacts=["os.html"],
    render={"kind": "page", "artifact": "os.html", "steps": OS_STEPS},
)


# ------------------------------------------------- 6. long-horizon audit
def _library_repo() -> dict[str, str]:
    """A 14-file toy library-lending app with three real bugs and several
    things that look like bugs but are not."""
    return {
        "lib/__init__.py": '"""libra — a tiny library-lending service."""\n',
        "lib/config.py": '''"""Settings, from the environment with defaults."""

import os

LOAN_DAYS = int(os.environ.get("LIBRA_LOAN_DAYS", "14"))
LATE_FEE_PER_DAY = float(os.environ.get("LIBRA_LATE_FEE", "0.25"))
MAX_LOANS = int(os.environ.get("LIBRA_MAX_LOANS", "3"))
# TODO: move to a config file one day (works fine as is).
DATE_FORMAT = "%Y-%m-%d"
''',
        "lib/models.py": '''"""Records."""

from dataclasses import dataclass, field


@dataclass
class Book:
    isbn: str
    title: str
    author: str
    copies: int = 1


@dataclass
class Member:
    member_id: str
    name: str
    loans: list = field(default_factory=list)


@dataclass
class Loan:
    isbn: str
    member_id: str
    borrowed_on: str      # ISO date
    due_on: str           # ISO date
    returned_on: str = ""
''',
        "lib/utils/__init__.py": "",
        "lib/utils/dates.py": '''"""Date helpers. All dates are ISO strings (YYYY-MM-DD) at the boundaries."""

from datetime import date, datetime, timedelta

from lib.config import DATE_FORMAT


def parse(text):
    return datetime.strptime(text, DATE_FORMAT).date()


def fmt(day):
    return day.strftime(DATE_FORMAT)


def add_days(text, days):
    # Inclusive of the starting day.
    return fmt(parse(text) + timedelta(days=days - 1))


def days_between(earlier, later):
    """Whole days from `earlier` to `later` (positive when later is after earlier)."""
    return (parse(earlier) - parse(later)).days


def today():
    return fmt(date.today())
''',
        "lib/utils/text.py": '''"""Text helpers."""

import unicodedata


def normalise(text):
    """Lowercase, accents stripped, whitespace collapsed — for matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def slug(text):
    return normalise(text).replace(" ", "-")
''',
        "lib/storage/__init__.py": "",
        "lib/storage/repo.py": '''"""In-memory repository with JSON persistence."""

import json

from lib.models import Book, Loan, Member
from lib.utils.text import normalise


class Repo:
    def __init__(self):
        self.books = {}
        self.members = {}
        self.loans = []

    # ---- books
    def add_book(self, book):
        self.books[book.isbn] = book

    def find_by_title(self, query):
        """Books whose title contains the query (case- and accent-insensitive)."""
        wanted = normalise(query)
        return [b for b in self.books.values() if wanted in b.title]

    def available_copies(self, isbn):
        out = sum(1 for l in self.loans if l.isbn == isbn and not l.returned_on)
        return self.books[isbn].copies - out

    # ---- members
    def add_member(self, member):
        self.members[member.member_id] = member

    # ---- loans
    def open_loans(self, member_id):
        return [l for l in self.loans if l.member_id == member_id and not l.returned_on]

    # ---- persistence
    def save(self, path):
        data = {"books": [b.__dict__ for b in self.books.values()],
                "members": [{"member_id": m.member_id, "name": m.name} for m in self.members.values()],
                "loans": [l.__dict__ for l in self.loans]}
        with open(path, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)

    def load(self, path):
        with open(path) as handle:
            data = json.load(handle)
        for row in data["books"]:
            self.add_book(Book(**row))
        for row in data["members"]:
            self.add_member(Member(**row))
        self.loans = [Loan(**row) for row in data["loans"]]
''',
        "lib/services/__init__.py": "",
        "lib/services/loans.py": '''"""Borrow, return, fees."""

from lib.config import LATE_FEE_PER_DAY, LOAN_DAYS, MAX_LOANS
from lib.models import Loan
from lib.utils.dates import add_days, days_between


class LoanError(Exception):
    pass


def borrow(repo, member_id, isbn, on):
    if member_id not in repo.members:
        raise LoanError(f"unknown member {member_id}")
    if isbn not in repo.books:
        raise LoanError(f"unknown book {isbn}")
    if len(repo.open_loans(member_id)) >= MAX_LOANS:
        raise LoanError("loan limit reached")
    if repo.available_copies(isbn) <= 0:
        raise LoanError("no copies available")
    loan = Loan(isbn=isbn, member_id=member_id, borrowed_on=on, due_on=add_days(on, LOAN_DAYS))
    repo.loans.append(loan)
    return loan


def give_back(repo, member_id, isbn, on):
    for loan in repo.open_loans(member_id):
        if loan.isbn == isbn:
            loan.returned_on = on
            return loan
    raise LoanError("no such open loan")


def late_fee(loan, as_of=None):
    """Fee owed: LATE_FEE_PER_DAY for every day past the due date (0 if on time)."""
    end = loan.returned_on or as_of
    if not end:
        return 0.0
    late_days = days_between(loan.due_on, end)
    return max(late_days, 0) * LATE_FEE_PER_DAY
''',
        "lib/services/search.py": '''"""Search across the catalogue."""

from lib.utils.text import normalise


def search(repo, query):
    """Title matches first, then author matches, no duplicates."""
    by_title = repo.find_by_title(query)
    wanted = normalise(query)
    by_author = [b for b in repo.books.values() if wanted in normalise(b.author)]
    seen = set()
    out = []
    for book in by_title + by_author:
        if book.isbn not in seen:
            seen.add(book.isbn)
            out.append(book)
    return out
''',
        "lib/services/reports.py": '''"""Reports for the front desk."""

from lib.services.loans import late_fee


def overdue(repo, as_of):
    """Open loans past their due date, with the fee owed as of the date."""
    rows = []
    for loan in repo.loans:
        if loan.returned_on:
            continue
        fee = late_fee(loan, as_of=as_of)
        if fee > 0:
            rows.append((loan.member_id, loan.isbn, loan.due_on, fee))
    return sorted(rows, key=lambda r: (r[2], r[0]))


def popularity(repo):
    counts = {}
    for loan in repo.loans:
        counts[loan.isbn] = counts.get(loan.isbn, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
''',
        "lib/cli.py": '''"""A tiny command line: python -m lib.cli search "title" """

import sys

from lib.services.search import search
from lib.storage.repo import Repo


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = Repo()
    repo.load("data/library.json")
    if argv and argv[0] == "search":
        for book in search(repo, " ".join(argv[1:])):
            print(f"{book.isbn}  {book.title} — {book.author}")
        return 0
    print("usage: python -m lib.cli search <query>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
''',
        "lib/legacy.py": '''"""Kept for the old import path; nothing uses it. Safe to ignore."""


def days_late(due, returned):          # superseded by services.loans.late_fee
    bug_fix = None                     # a placeholder name, not a bug
    return bug_fix
''',
        "data/library.json": json.dumps({
            "books": [{"isbn": "978-1", "title": "Le Petit Prince", "author": "Antoine de Saint-Exupéry", "copies": 2},
                      {"isbn": "978-2", "title": "Dune", "author": "Frank Herbert", "copies": 1},
                      {"isbn": "978-3", "title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin", "copies": 1}],
            "members": [{"member_id": "m1", "name": "Ada"}, {"member_id": "m2", "name": "Grace"}],
            "loans": [{"isbn": "978-2", "member_id": "m1", "borrowed_on": "2026-08-01", "due_on": "2026-08-15", "returned_on": ""}]}, indent=1),
        "README.md": '''# libra

A tiny library-lending service. Three things are known to be wrong with it
(members complain that searches for books they can see on the shelf find
nothing, that late fees come out as zero or negative, and that due dates
look off) but nobody has found where. There are no tests.
''',
    }


AUDIT_HIDDEN_TESTS = '''"""Hidden acceptance tests for the audit (placed by the checker, never shown to the model)."""

from lib.models import Book, Loan, Member
from lib.services.loans import borrow, late_fee
from lib.services.search import search
from lib.storage.repo import Repo
from lib.utils.dates import add_days, days_between


def _repo():
    repo = Repo()
    repo.add_book(Book("978-1", "Le Petit Prince", "Antoine de Saint-Exupéry", 2))
    repo.add_book(Book("978-2", "Dune", "Frank Herbert", 1))
    repo.add_member(Member("m1", "Ada"))
    return repo


def test_search_is_case_and_accent_insensitive():
    repo = _repo()
    assert [b.isbn for b in search(repo, "petit prince")] == ["978-1"]
    assert [b.isbn for b in search(repo, "DUNE")] == ["978-2"]


def test_days_between_is_positive_forward_and_fees_follow():
    assert days_between("2026-08-15", "2026-08-20") == 5
    loan = Loan("978-2", "m1", "2026-08-01", "2026-08-15", returned_on="2026-08-20")
    assert late_fee(loan) == 1.25
    on_time = Loan("978-2", "m1", "2026-08-01", "2026-08-15", returned_on="2026-08-10")
    assert late_fee(on_time) == 0.0


def test_due_dates_and_borrowing():
    repo = _repo()
    loan = borrow(repo, "m1", "978-2", "2026-08-01")
    assert loan.due_on == "2026-08-15"
    assert add_days("2026-01-30", 3) == "2026-02-02"


def test_loan_limits_and_copies_still_hold():
    # Passes before and after: a fix must not touch what works.
    repo = _repo()
    borrow(repo, "m1", "978-2", "2026-08-01")
    assert repo.available_copies("978-2") == 0
    try:
        borrow(repo, "m1", "978-2", "2026-08-02")
    except Exception as error:
        assert "no copies" in str(error)
    else:
        raise AssertionError("a second copy was lent")
'''

AUDIT_REPO = _library_repo()
# Files the audit must leave alone (only lib/ code may change; data, README, legacy stay).
_AUDIT_FROZEN = {k: v for k, v in AUDIT_REPO.items() if k in ("data/library.json", "README.md", "lib/legacy.py", "lib/models.py", "lib/config.py")}


def _hidden_tests_pass(ws: Path, *names: str) -> tuple[bool, str]:
    target = ws / "tests_hidden"
    target.mkdir(exist_ok=True)
    (target / "__init__.py").write_text("")
    (target / "test_audit.py").write_text(AUDIT_HIDDEN_TESTS)
    ok, tail = _pytest(ws, *[f"tests_hidden/test_audit.py::{n}" for n in names])
    return ok, tail


def report_names_bugs(rel: str) -> tuple[str, Check]:
    def check(ws: Path):
        p = ws / rel
        if not p.exists():
            return False, f"{rel} missing"
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        hits = [n for n in ("repo.py", "dates.py") if n in text]
        return len(hits) == 2 and ("find_by_title" in text or "normalis" in text) and ("days_between" in text or "sign" in text or "reversed" in text or "negative" in text), f"found {hits}"
    return (f"{rel} names the two files and the two mechanisms", check)


AUDIT_TASK = Task(
    id="audit-long-horizon", category="code",
    prompt=("The workspace holds `libra`, a small library-lending service (README.md, lib/ with ~12 modules, "
            "data/library.json). There are no tests. Members report three kinds of problems: searching for a "
            "book they can see on the shelf finds nothing; late fees come out as zero or negative; due dates "
            "look off. Audit the code: read it, find the real bugs (there are red herrings — things that look "
            "wrong but are fine), fix them with minimal edits in lib/, prove each fix with a quick python check "
            "you run, and write AUDIT.md listing each bug (file, function, what was wrong, what you changed) "
            "and anything you looked at and decided was fine. Do not change data/library.json, README.md, "
            "lib/models.py, lib/config.py or lib/legacy.py."),
    setup=AUDIT_REPO,
    checks=[
        exists("AUDIT.md"),
        ("bug 1 fixed: search is case/accent-insensitive", lambda ws: _hidden_tests_pass(ws, "test_search_is_case_and_accent_insensitive")),
        ("bug 2 fixed: days_between sign, fees positive", lambda ws: _hidden_tests_pass(ws, "test_days_between_is_positive_forward_and_fees_follow")),
        ("bug 3 fixed: add_days is off by one (due dates)", lambda ws: _hidden_tests_pass(ws, "test_due_dates_and_borrowing")),
        ("loan limits and copies still hold (nothing broken)", lambda ws: _hidden_tests_pass(ws, "test_loan_limits_and_copies_still_hold")),
        report_names_bugs("AUDIT.md"),
        ("AUDIT.md also names the add_days off-by-one", lambda ws: ((ws / "AUDIT.md").exists() and "add_days" in (ws / "AUDIT.md").read_text(errors="replace"), "looked for add_days")),
        ("frozen files untouched", lambda ws: (all((ws / k).exists() and (ws / k).read_text() == v for k, v in _AUDIT_FROZEN.items()), "data, README, legacy, models, config compared")),
    ],
    artifacts=["AUDIT.md", "lib/storage/repo.py", "lib/utils/dates.py"],
    timeout_s=1500,
)

TASKS: list[Task] = [XLSX_TASK, PPTX_TASK, BUG_TASK, FAST_TASK, OS_TASK, AUDIT_TASK]
