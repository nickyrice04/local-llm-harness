---
name: docx-python
description: Write or edit Word documents (.docx) with python-docx — real heading styles, paragraphs, bullet lists, tables, images, page breaks — and verify by reloading and rendering to PDF/PNG before finishing.
---

# Documents with python-docx (installed in the venv)

A document a person opens in Word must use real STYLES (so the navigation
pane, table of contents and formatting all work), not bold text
pretending to be a heading.

## Worked example (copy, then adapt)

```python
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

doc = Document()
style = doc.styles["Normal"]; style.font.name = "Calibri"; style.font.size = Pt(11)

doc.add_heading("Quarterly report", level=0)                 # the title (style "Title")
p = doc.add_paragraph("Prepared for the leadership team · September 2026")
p.runs[0].italic = True

doc.add_heading("Summary", level=1)
doc.add_paragraph("Sales rose 12% quarter on quarter, driven by the north region. "
                  "Two risks remain: pricing on the gizmo line and a thin east pipeline.")
for point in ["North: +18%", "South: +6%", "East: −3%"]:
    doc.add_paragraph(point, style="List Bullet")

doc.add_heading("Figures", level=1)
table = doc.add_table(rows=1, cols=3)
table.style = "Light Grid Accent 1"
for cell, text in zip(table.rows[0].cells, ["Region", "Q2", "Q3"]):
    cell.text = text
    cell.paragraphs[0].runs[0].bold = True
for region, q2, q3 in [("North", 120, 142), ("South", 80, 85), ("East", 60, 58)]:
    row = table.add_row().cells
    row[0].text, row[1].text, row[2].text = region, f"{q2:,}", f"{q3:,}"
    row[1].paragraphs[0].alignment = row[2].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

doc.add_paragraph()                                           # breathing room
doc.add_picture("chart.png", width=Inches(5.5))               # a matplotlib PNG, made earlier
caption = doc.add_paragraph("Figure 1 — sales by region"); caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
caption.runs[0].font.size = Pt(9); caption.runs[0].font.color.rgb = RGBColor(0x6B, 0x72, 0x80)

doc.add_page_break()
doc.add_heading("Next steps", level=1)
for i, step in enumerate(["Rebalance the pipeline", "Repeat the north playbook", "Review gizmo pricing"], 1):
    doc.add_paragraph(step, style="List Number")
doc.save("report.docx")
print("saved report.docx")
```

Editing an existing document: `Document(path)`, walk `doc.paragraphs` /
`doc.tables`, change `run.text` (not `paragraph.text`, which drops the
run formatting), then save to a NEW name unless told to overwrite.

## Rules

- Headings via `add_heading(text, level)`; lists via the `List Bullet` /
  `List Number` styles; never fake either with bold or "- ".
- Keep paragraphs short; one idea each. A table for numbers, not prose.
- Images: make the PNG first (matplotlib, `dpi=150`), then `add_picture`
  with a width so it fits the page (≤ 6 in on Letter/A4 with default margins).

## Verify (required)

1. Reload: `run_command python -c "from docx import Document; d=Document('report.docx'); print(len(d.paragraphs),'paragraphs',len(d.tables),'tables'); [print(p.style.name, '|', p.text[:60]) for p in d.paragraphs if p.style.name.startswith(('Heading','Title'))]"`
   — every heading you intended must appear with its style.
2. `{"tool": "verify_file", "args": {"path": "report.docx"}}` — the harness
   loads the document and renders its pages to PNGs (outside your sandbox
   — do NOT run soffice yourself, it cannot run there); if you can see
   (read_image), look at page 1.

The harness also runs the same load-and-render on its own after any
command that writes a document and appends the verdict; do not finish
while it says FIX NEEDED.
