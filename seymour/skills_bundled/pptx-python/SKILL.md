---
name: pptx-python
description: Build .pptx decks with python-pptx that are presentable — a layout discipline (grid, safe margins, one idea per slide, one palette defined once), charts from real data, speaker notes — and verify by rendering to PNG and checking overflow before finishing.
---

# Decks that do not embarrass you

Most local-model decks fail on two things: text that overflows its box,
and the default template's look (Calibri on white with "Click to add
text" showing). This skill is a discipline against exactly those.

## The layout discipline

- **One idea per slide.** A title (≤ 8 words) and 3–5 bullets of ≤ 12
  words, or one chart/figure with a one-line takeaway. More words → more
  slides, never a smaller font.
- **A grid and safe margins.** 16:9 (13.333 × 7.5 in). Content lives
  inside 0.6 in margins: title at (0.6, 0.4) × 12.1 wide; body from y=1.5
  to y=6.9. Two-column: each column 5.9 in wide with a 0.3 in gutter.
- **One palette, defined once**, used everywhere: a dark ink, a light
  background, one accent, one muted grey. No default theme colours.
- **Consistent fonts**: title 32 pt, body 18 pt, captions 12 pt. Every
  slide the same.
- **Never leave a placeholder empty** — use the BLANK layout (6) and add
  your own text boxes so nothing says "Click to add".
- **Speaker notes** on every content slide: what to say in 2–3 sentences.

## Worked example (copy, then adapt)

```python
import pandas as pd
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

INK, BG, ACCENT, MUTED = RGBColor(0x1F, 0x29, 0x37), RGBColor(0xFA, 0xFA, 0xF7), RGBColor(0x0E, 0x7C, 0x86), RGBColor(0x6B, 0x72, 0x80)
TITLE_PT, BODY_PT, CAPTION_PT = 32, 18, 12
W, H, M = 13.333, 7.5, 0.6

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(W), Inches(H)
BLANK = prs.slide_layouts[6]

def new_slide(title):
    slide = prs.slides.add_slide(BLANK)
    fill = slide.background.fill; fill.solid(); fill.fore_color.rgb = BG
    box = slide.shapes.add_textbox(Inches(M), Inches(0.4), Inches(W - 2 * M), Inches(0.9))
    p = box.text_frame.paragraphs[0]; p.text = title
    p.font.size, p.font.bold, p.font.color.rgb = Pt(TITLE_PT), True, INK
    bar = slide.shapes.add_shape(1, Inches(M), Inches(1.3), Inches(1.2), Inches(0.06))   # accent rule under the title
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
    return slide

def bullets(slide, items, left=M, top=1.6, width=W - 2 * M, height=5.2):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame; tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = "•  " + item; p.font.size = Pt(BODY_PT); p.font.color.rgb = INK; p.space_after = Pt(10)
    return box

def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text

# Title slide
s = prs.slides.add_slide(BLANK)
s.background.fill.solid(); s.background.fill.fore_color.rgb = INK
t = s.shapes.add_textbox(Inches(M), Inches(2.6), Inches(W - 2 * M), Inches(1.4)).text_frame
t.text = "Q3 sales review"; t.paragraphs[0].font.size = Pt(44); t.paragraphs[0].font.bold = True; t.paragraphs[0].font.color.rgb = BG
sub = s.shapes.add_textbox(Inches(M), Inches(4.1), Inches(W - 2 * M), Inches(0.8)).text_frame
sub.text = "What moved, and what we do next"; sub.paragraphs[0].font.size = Pt(20); sub.paragraphs[0].font.color.rgb = RGBColor(0xC9, 0xD1, 0xD9)

# A chart from the actual numbers
df = pd.read_csv("sales.csv")
by_region = df.groupby("region")["amount"].sum().sort_values(ascending=False)
s = new_slide("North carries the quarter")
data = CategoryChartData(); data.categories = list(by_region.index); data.add_series("Sales", [round(v, 0) for v in by_region.values])
chart = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(M), Inches(1.6), Inches(8.0), Inches(5.2), data).chart
chart.has_legend = False; chart.plots[0].series[0].format.fill.solid(); chart.plots[0].series[0].format.fill.fore_color.rgb = ACCENT
chart.category_axis.tick_labels.font.size = Pt(CAPTION_PT); chart.value_axis.tick_labels.font.size = Pt(CAPTION_PT)
bullets(s, [f"{by_region.index[0]} is {by_region.iloc[0] / by_region.sum():.0%} of sales",
            f"{len(by_region)} regions, total {by_region.sum():,.0f}"], left=9.0, top=1.8, width=3.7, height=4.5)
notes(s, "Lead with the concentration: one region is most of the quarter. Ask whether that is a strength or a risk.")

s = new_slide("Three things to do next")
bullets(s, ["Rebalance the pipeline toward the two smallest regions", "Repeat the north playbook in the east", "Review pricing on the gizmo line"])
notes(s, "Each of these has an owner and a date on the next slide.")
prs.save("review.pptx")
print("saved review.pptx", len(prs.slides), "slides")
```

Tables: `slide.shapes.add_table(rows, cols, left, top, width, height)`,
≤ 8 rows per slide, header row bold on the accent colour.
Images: `slide.shapes.add_picture(path, left, top, width=...)` — set only
width OR height to keep the aspect ratio.

## Verify by rendering (required)

1. `run_command python make_deck.py` — fix and rerun on any traceback.
2. Render and count:
   `run_command /opt/homebrew/bin/soffice --headless --convert-to pdf --outdir render review.pptx && python -c "import pymupdf; d=pymupdf.open('render/review.pdf'); print(len(d),'pages'); [p.get_pixmap(dpi=100).save(f'render/slide-{i+1:02d}.png') for i,p in enumerate(d)]"`
3. Check structure: `python -c "from pptx import Presentation; p=Presentation('review.pptx'); print(len(p.slides)); [print(i+1, [sh.text_frame.text[:40] for sh in s.shapes if sh.has_text_frame], 'notes' if s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip() else 'NO NOTES') for i,s in enumerate(p.slides)]"`
4. If the model can see (read_image works), look at `render/slide-01.png`
   and one content slide: text cut off at a box edge, overlapping shapes
   or a blank slide means fix and re-render.

The harness runs the same checks after every save (empty placeholders,
estimated overflow, slide count, a render to `.seymour/artifacts/render/`)
and appends the verdict to your tool result. Do not finish while it says
FIX NEEDED. Report the slide list you verified and where the PNGs are.
