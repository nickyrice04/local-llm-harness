---
name: pptx-python
description: Build .pptx decks with python-pptx (title slide, bullet slides, closing slide, notes); re-load the file to verify slide count and titles match the request exactly.
---

# Decks with python-pptx (installed in the venv)

1. Outline first: one line per slide with its title and 3–5 bullets. Match the requested slide COUNT exactly.
2. One script: `from pptx import Presentation`, `from pptx.util import Inches, Pt`. Layouts: `prs.slide_layouts[0]` title, `[1]` title + content, `[5]` title only, `[6]` blank. For each slide `slide.shapes.title.text = ...`; bullets go in `slide.placeholders[1].text_frame` (first bullet `tf.text`, then `p = tf.add_paragraph(); p.text = ...`). Never leave a placeholder empty (delete it or fill it). Speaker notes: `slide.notes_slide.notes_text_frame.text`. `prs.save(path)`.
3. `run_command python make_deck.py`; fix and rerun on any traceback.
4. VERIFY: `python -c "from pptx import Presentation; p=Presentation('deck.pptx'); print(len(p.slides)); [print(i+1, s.shapes.title.text if s.shapes.title else '(no title)') for i,s in enumerate(p.slides)]"` and compare count and titles with the request.
5. Report the slide list you verified.
