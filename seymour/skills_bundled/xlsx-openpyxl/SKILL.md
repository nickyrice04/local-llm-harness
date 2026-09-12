---
name: xlsx-openpyxl
description: Create or edit .xlsx workbooks with openpyxl (sheets, styles, formulas, widths); re-open the file to verify sheet names, cells and formulas before finishing.
---

# Workbooks with openpyxl (installed in the venv)

1. Plan sheet names and column layout before writing code.
2. One script: `from openpyxl import Workbook` / `load_workbook`; rename the default sheet (`wb.active.title = "Budget"`), `wb.create_sheet("Notes")`, write the header then rows with `ws.append([...])`, bold headers with `Font(bold=True)`, totals as FORMULAS (`"=SUM(B2:B6)"`) and, when the task wants values readable without Excel, the computed number in the neighbouring cell; set widths with `ws.column_dimensions["A"].width = 18`; `wb.save(path)`.
3. `run_command python make_book.py`; a nonzero exit is a bug to fix, not a stop.
4. VERIFY by re-opening: `python -c "import openpyxl; wb=openpyxl.load_workbook('budget.xlsx'); print(wb.sheetnames); ws=wb['Budget']; print(ws.max_row, ws.max_column, ws['A1'].value, ws['A1'].font.bold, ws['B7'].value)"` and check every requirement (names, counts, bold, formula text) against the request.
5. Say done only after the check matches; name the file path.

Pitfalls: `load_workbook(data_only=True)` hides formulas; editing an existing file loses nothing only if you load it, not recreate it.
