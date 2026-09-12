---
name: data-analysis
description: Analyse a CSV/XLSX with pandas and produce readable charts with matplotlib (PNG, dpi 150, labelled axes, one message per chart); state findings with the numbers that support them and verify every figure by printing it.
---

# Data analysis: pandas for numbers, matplotlib for pictures

## The order of work

1. **Look before you compute.** `read_file` the first 30 lines of the CSV.
   Then in Python: `df.shape`, `df.dtypes`, `df.head()`, `df.isna().sum()`,
   `df.describe()`. Print them — decide from what you SEE, not from the
   column names.
2. **Clean explicitly and say what you did**: dates
   (`pd.to_datetime(..., errors="coerce")`), currency and thousands
   separators (`str.replace(r"[$,£€\s]", "", regex=True)` then
   `pd.to_numeric(errors="coerce")`), duplicates (`drop_duplicates`),
   junk rows (`dropna(subset=[key columns])`). Keep a list of the cleaning
   steps for the report.
3. **Compute with groupby, print the result**, and keep the printed table:
   it is the evidence for every claim you make.
4. **One message per chart.** Title says the finding ("North is 48% of
   sales"), axes are labelled with units, a legend only when there are
   several series, no 3D, no pie charts with more than 4 slices.
5. **Report**: findings first (with the number), then the cleaning notes,
   then the chart paths.

## Worked example (copy, then adapt)

```python
import matplotlib
matplotlib.use("Agg")                                          # no display in the sandbox
import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv("sales.csv")
df["date"] = pd.to_datetime(df["date"], errors="coerce")
df["amount"] = pd.to_numeric(df["amount"].astype(str).str.replace(r"[$,£€\s]", "", regex=True), errors="coerce")
before = len(df)
df = df.drop_duplicates().dropna(subset=["date", "amount"])
print(f"rows {before} -> {len(df)} after cleaning")
print(df.dtypes)

by_region = df.groupby("region")["amount"].sum().sort_values(ascending=False)
print(by_region.round(2))
monthly = df.set_index("date").resample("MS")["amount"].sum()
print(monthly.round(2))

fig, ax = plt.subplots(figsize=(8, 4.5))
by_region.plot.bar(ax=ax, color="#0E7C86")
ax.set_title(f"{by_region.index[0]} is {by_region.iloc[0] / by_region.sum():.0%} of sales")
ax.set_xlabel("Region"); ax.set_ylabel("Sales (USD)")
ax.spines[["top", "right"]].set_visible(False)
for i, v in enumerate(by_region.values):
    ax.annotate(f"{v:,.0f}", (i, v), ha="center", va="bottom", fontsize=9)
fig.tight_layout(); fig.savefig("sales_by_region.png", dpi=150); plt.close(fig)

fig, ax = plt.subplots(figsize=(8, 4.5))
monthly.plot(ax=ax, marker="o", color="#1F2937")
ax.set_title("Monthly sales"); ax.set_xlabel("Month"); ax.set_ylabel("Sales (USD)")
ax.grid(axis="y", alpha=.3); ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout(); fig.savefig("sales_monthly.png", dpi=150); plt.close(fig)
print("wrote sales_by_region.png, sales_monthly.png")
```

## Verify (required)

- Every number in your report appears in a printed pandas result in a
  tool output. No number from memory.
- `list_files` shows the PNGs with a size > 10 KB (an empty figure is ~2 KB).
- If you can see (read_image), open each PNG: labels readable, nothing
  cut off, the title states the finding.
- Cross-check one aggregate two ways (e.g. `groupby` total vs `df["amount"].sum()`).
