"""evals/render — a deliverable becomes deterministic PNGs.

Seymour PRODUCES and RENDERS; the judge (a vision-capable subagent run
outside Seymour — see evals/judge/README.md) looks at the pictures.
These scripts are the rendering half, kept in the repo so every eval
run renders the same way and a later run can be diffed against an
earlier one. They call the same code the harness's own verifiers use
(seymour.verify), so what the model is told about a file and what the
judge sees come from one place.

    render_artifact(path, outdir, steps=[...])  → {"images": [...], "console": ..., "notes": [...]}

    .html  Playwright screenshots at 1440×900, 1024×768 and 375×812, at
           top / middle / bottom, and after each named interaction; the
           console log beside them
    .pptx  soffice → PDF → one PNG per slide at 150 dpi (PyMuPDF) plus a
           contact sheet of the whole deck
    .xlsx  recalculated through soffice, converted to PDF, rasterized —
           a spreadsheet is a document and it can look bad
    .docx  soffice → PDF → PNG per page

Callable from the eval runner (evals/compare/run.py, the `render` field
of a Task) and from the command line:

    .venv/bin/python -m evals.render <file> <outdir> [--steps "click #a; type #b hi"]
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from seymour.verify import office, web  # noqa: E402


async def render_artifact(path: Path, outdir: Path, steps: list[str] | None = None) -> dict:
    """Render one deliverable into outdir; never raises (notes carry failures)."""
    path = Path(path)
    outdir = Path(outdir)
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    result: dict = {"images": [], "console": "", "notes": []}
    if not path.exists():
        result["notes"].append(f"{path.name} does not exist")
        return result
    suffix = path.suffix.lower()
    try:
        if suffix in (".html", ".htm"):
            shots = await web.screenshot_set(path, outdir, steps=steps or [])
            result["images"] = shots["images"]
            result["console"] = shots["console"]
            if shots.get("error"):
                result["notes"].append(shots["error"])
            if shots.get("interaction") is not None:
                result["interaction"] = shots["interaction"].text()
                result["interaction_ok"] = shots["interaction"].ok
        elif suffix == ".pptx":
            pdf = await office.soffice_convert(path, "pdf", outdir)
            if pdf is None:
                result["notes"].append("soffice could not convert the deck")
            else:
                images = office.render_pdf(pdf, outdir, stem="slide")
                sheet = office.contact_sheet(images, outdir / "contact.png")
                result["images"] = [str(i) for i in images] + ([str(sheet)] if sheet else [])
        elif suffix == ".xlsx":
            recalculated = await office.soffice_convert(path, "xlsx", outdir / "recalc")
            source = recalculated or path
            if recalculated is None:
                result["notes"].append("soffice could not recalculate; rendering the file as saved")
            pdf = await office.soffice_convert(source, "pdf", outdir)
            if pdf is None:
                result["notes"].append("soffice could not convert the workbook")
            else:
                result["images"] = [str(i) for i in office.render_pdf(pdf, outdir, stem="sheet")]
        elif suffix == ".docx":
            pdf = await office.soffice_convert(path, "pdf", outdir)
            if pdf is None:
                result["notes"].append("soffice could not convert the document")
            else:
                result["images"] = [str(i) for i in office.render_pdf(pdf, outdir, stem="page")]
        elif suffix == ".png":
            target = outdir / path.name
            shutil.copy2(path, target)
            result["images"] = [str(target)]
        else:
            result["notes"].append(f"no renderer for {suffix}")
    except Exception as error:
        result["notes"].append(f"render failed: {type(error).__name__}: {error}")
    return result


def render_sync(path: Path, outdir: Path, steps: list[str] | None = None) -> dict:
    return asyncio.run(render_artifact(path, outdir, steps))
