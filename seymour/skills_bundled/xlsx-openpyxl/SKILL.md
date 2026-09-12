---
name: xlsx-openpyxl
description: Build or edit .xlsx workbooks that are RIGHT when opened in Excel — pandas for the data, openpyxl for the presentation (real formulas, several sheets, number formats, conditional formatting, a native chart, freeze panes) — and verify by recalculating with LibreOffice and reading the values back.
---

# Workbooks: pandas for the data, openpyxl for the presentation

A workbook a person opens must look finished and compute correctly. Most
local-model workbooks fail in three ways: values pasted where formulas
were wanted, formulas that reference the wrong range (so Excel shows
#REF! or 0), and raw numbers with no formats. This skill attacks all three.

## The order of work

1. **Wrangle in pandas, never in cell loops.** Load, clean, aggregate with
   pandas (installed). Dates → `pd.to_datetime(..., errors="coerce")`;
   currency strings → strip `$ , £ €` and `pd.to_numeric`; duplicates →
   `drop_duplicates`; blanks → decide (fill or drop) and NOTE it.
2. **Write with openpyxl, sheet by sheet.** Data sheet first (clean rows),
   summary sheet with FORMULAS that reference the data sheet, chart on the
   summary, a notes sheet stating what you did.
3. **Save, then verify by recalculating** (step 6). The harness also runs
   this check automatically after every write and reports back; fix
   anything it names.

## Worked example (copy, then adapt)

```python
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

df = pd.read_csv("sales.csv")
df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=False)
df["amount"] = pd.to_numeric(df["amount"].astype(str).str.replace(r"[$,£€\s]", "", regex=True), errors="coerce")
before = len(df)
df = df.drop_duplicates().dropna(subset=["date", "amount"])
cleaned_note = f"Removed {before - len(df)} duplicate/blank rows; parsed dates and currency."

wb = Workbook()
ws = wb.active
ws.title = "Data"
header = list(df.columns)
ws.append(header)
for row in df.itertuples(index=False):
    ws.append([v.to_pydatetime() if hasattr(v, "to_pydatetime") else v for v in row])
n = len(df) + 1                                   # last data row (1-based, header is row 1)
bold = Font(bold=True)
for cell in ws[1]:
    cell.font = bold
    cell.fill = PatternFill("solid", fgColor="DDEBF7")
ws.freeze_panes = "A2"                            # header stays visible
amount_col = get_column_letter(header.index("amount") + 1)
date_col = get_column_letter(header.index("date") + 1)
for r in range(2, n + 1):
    ws[f"{amount_col}{r}"].number_format = "#,##0.00"
    ws[f"{date_col}{r}"].number_format = "yyyy-mm-dd"
for i, col in enumerate(header, 1):
    ws.column_dimensions[get_column_letter(i)].width = max(12, len(col) + 4)

# Summary by region: LIVE formulas over the Data sheet, not pasted numbers.
region_col = get_column_letter(header.index("region") + 1)
summary = wb.create_sheet("Summary")
summary.append(["Region", "Total", "Count", "Average"])
for cell in summary[1]:
    cell.font = bold
regions = sorted(df["region"].dropna().unique())
for i, region in enumerate(regions, start=2):
    summary[f"A{i}"] = region
    summary[f"B{i}"] = f'=SUMIF(Data!{region_col}$2:{region_col}${n},A{i},Data!{amount_col}$2:{amount_col}${n})'
    summary[f"C{i}"] = f'=COUNTIF(Data!{region_col}$2:{region_col}${n},A{i})'
    summary[f"D{i}"] = f"=IF(C{i}=0,0,B{i}/C{i})"
    for col in "BD":
        summary[f"{col}{i}"].number_format = "#,##0.00"
last = len(regions) + 1
summary[f"A{last + 1}"] = "Total"
summary[f"B{last + 1}"] = f"=SUM(B2:B{last})"
summary[f"A{last + 1}"].font = bold
summary[f"B{last + 1}"].font = bold
# Conditional formatting: totals above the mean in green.
summary.conditional_formatting.add(f"B2:B{last}", CellIsRule(operator="greaterThan", formula=[f"AVERAGE($B$2:$B${last})"],
                                                            fill=PatternFill("solid", fgColor="C6EFCE")))
chart = BarChart()
chart.title = "Total by region"
chart.add_data(Reference(summary, min_col=2, min_row=1, max_row=last), titles_from_data=True)
chart.set_categories(Reference(summary, min_col=1, min_row=2, max_row=last))
chart.height, chart.width = 7, 14
summary.add_chart(chart, "F2")
summary.column_dimensions["A"].width = 16

notes = wb.create_sheet("Notes")
notes["A1"] = "What was done"
notes["A1"].font = bold
notes["A2"] = cleaned_note
notes["A3"] = "Summary formulas are live (SUMIF/COUNTIF over the Data sheet)."
notes.column_dimensions["A"].width = 80
notes["A2"].alignment = Alignment(wrap_text=True)
wb.save("report.xlsx")
print("saved report.xlsx", n - 1, "rows")
```

For a two-dimensional summary (region × month) use `SUMIFS` with a helper
month column in Data (`=TEXT(B2,"yyyy-mm")`) and a header row of months.

## Rules that keep workbooks correct

- Totals, subtotals, averages, percentages: **formulas**, referencing the
  data range by its real size (`n` above). Never paste a Python-computed
  total where a formula belongs — the workbook must stay right when the
  person edits a number.
- Formula ranges must not include the header row or run past the data.
- `data_only=True` when loading hides formulas — never use it to write.
- Editing an existing file: `load_workbook(path)` and change cells; never
  recreate it (styles, other sheets and the person's work would be lost).
- Dates as real `datetime` values with a number_format, not strings.
- Numbers as numbers (`float`/`int`), formatted with `number_format`.

## Verify by recalculating (required)

```
run_command: python -c "import subprocess,tempfile,openpyxl,os; t=tempfile.mkdtemp(); subprocess.run(['/opt/homebrew/bin/soffice','--headless','--convert-to','xlsx','--outdir',t,'report.xlsx'],capture_output=True,timeout=120); wb=openpyxl.load_workbook(os.path.join(t,'report.xlsx'),data_only=True); s=wb['Summary']; print(wb.sheetnames); [print(r) for r in s.iter_rows(min_row=1,max_row=s.max_row,values_only=True)]"
```

LibreOffice recalculates every formula; `data_only=True` then shows the
REAL values. Every summary cell must show a number — `None`, `#REF!`,
`#NAME?`, `#DIV/0!` or `0` where data exists means a wrong range or name:
fix the formula and save again. Compare two or three totals against a
pandas `groupby` of the same data before you say done. The harness runs
this same recalculation after every save and appends its verdict to your
tool result; do not finish while it says FIX NEEDED.
