---
name: csv-edit
description: Read, transform and write small CSV files with the csv module (for analysis and cleaning at scale, load the data-analysis skill — pandas is installed); re-read the output to verify header, row count and sample values.
---

# CSV editing that survives a check

1. `read_file` the input first: learn the exact header and a few rows (delimiter, quoting, number formats).
2. Write ONE script with `write_file` (for example `csv_task.py`) using `csv.DictReader` / `csv.DictWriter` with `newline=""`. Convert numbers explicitly (`float()`, `int()`), round as the task says, and write the output with the SAME header order plus any new columns. Keep the task's row order.
3. `run_command python csv_task.py` and read the `[exit code N]` line. A traceback is data: fix the script, run again.
4. VERIFY with a second one-liner that reopens the OUTPUT and prints the header, the row count and two recomputed sample values. Compare with the input by hand.
5. Report what you saw (header, row count, the values), never what you intended.

Pitfalls: a TOTAL row must have one field per column; blank strings are not zeros; do not let Excel-style thousands separators into numeric columns.
