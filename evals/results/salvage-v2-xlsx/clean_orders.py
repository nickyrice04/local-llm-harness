import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.utils import get_column_letter

# Load CSV
df = pd.read_csv('orders.csv')

# Remove junk TOTAL row
df = df[~df['order_id'].astype(str).str.contains('TOTAL', case=False, na=False)]

# Parse qty, drop if blank
df['qty'] = pd.to_numeric(df['qty'], errors='coerce')
df = df.dropna(subset=['qty'])

# Parse dates
df['date'] = pd.to_datetime(df['date'], dayfirst=True, errors='coerce')
df = df.dropna(subset=['date'])

# Parse amounts
df['amount'] = df['amount'].astype(str).str.replace('[€$,]', '', regex=True).str.strip()
df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
df = df.dropna(subset=['amount'])

# Normalize region
df['region'] = df['region'].astype(str).str.strip().str.title()

# Drop duplicates
df = df.drop_duplicates(subset=['order_id'], keep='first')

# Sort
df = df.sort_values('order_id').reset_index(drop=True)

# Write Excel
wb = Workbook()
ws = wb.active
ws.title = 'Data'
headers = ['order_id', 'date', 'region', 'product', 'qty', 'amount']
ws.append(headers)
for _, row in df.iterrows():
    ws.append([row['order_id'], row['date'].strftime('%Y-%m-%d'), row['region'], row['product'], int(row['qty']), row['amount']])

# Summary Sheet
ws_sum = wb.create_sheet('Summary')
ws_sum.append(['Region', '2026-01', '2026-02', '2026-03', 'Total'])
regions = sorted(df['region'].unique())
months = ['2026-01', '2026-02', '2026-03']

for i, region in enumerate(regions, start=2):
    ws_sum.cell(row=i, column=1).value = region
    for j, month in enumerate(months, start=2):
        m_int = int(month.split('-')[1])
        next_m = m_int + 1 if m_int < 12 else 1
        next_y = 2026 if m_int < 12 else 2027
        formula = f'=SUMIFS(Data!F:F, Data!C:C, A{i}, Data!B:B, ">="&DATE(2026,{m_int},1), Data!B:B, "<"&DATE({next_y},{next_m},1))'
        ws_sum.cell(row=i, column=j).value = formula
    ws_sum.cell(row=i, column=6).value = f'=SUM(B{i}:D{i})'

total_row = len(regions) + 2
ws_sum.cell(row=1, column=2).value = 'Total'
for j, month in enumerate(months, start=2):
    col = get_column_letter(j)
    ws_sum.cell(row=total_row, column=j).value = f'=SUM({col}2:{col}{total_row-1})'
ws_sum.cell(row=total_row, column=6).value = f'=SUM(F2:F{total_row-1})'

# Chart
chart = BarChart()
chart.type = 'col'
chart.title = 'Total Amount by Region'
chart.x_axis.title = 'Region'
chart.y_axis.title = 'Amount'
data = Reference(ws_sum, min_col=2, max_col=4, min_row=1, max_row=total_row-1)
cats = Reference(ws_sum, min_col=1, min_row=2, max_row=total_row-1)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
chart.shape = 4
ws_sum.add_chart(chart, "E10")

# Notes Sheet
ws_notes = wb.create_sheet('Notes')
orig_rows = len(pd.read_csv('orders.csv'))
clean_rows = len(df)
ws_notes.append(['Cleaning Notes'])
ws_notes.append([f"Original rows: {orig_rows}"])
ws_notes.append([f"Cleaned rows: {clean_rows}"])
ws_notes.append([f"Rows removed: {orig_rows - clean_rows}"])
ws_notes.append(["Removed: TOTAL row, rows with blank amounts/qty/dates, duplicate order_ids."])
ws_notes.append(["Normalized: dates to ISO, amounts to numeric, regions to Title Case stripped."])
print(f"Done. {orig_rows} -> {clean_rows} rows.")