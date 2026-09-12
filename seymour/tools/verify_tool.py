"""verify_file — the harness's own verifier, on demand.

Deliverables are made by scripts (a workbook by openpyxl, a deck by
python-pptx), and the harness verifies what a command produced on its
own (run_executor watches the workspace). But a model that wants to
CHECK before it says done — the skills tell it to — needs a way to ask
that does not depend on running LibreOffice inside the sandbox, where
it exits silently (measured 2026-09-11: exit 0, no output, 0.17 s, and
the model spent thirty commands trying to make it recalculate). This
tool runs the same verifiers the harness runs (seymour.verify: recalc a
workbook, render and check a deck or a document, load a page) outside
the sandbox and returns the report.
"""

from seymour.tools import paths


async def verify_file(path: str) -> str:
    """Tool entry: run the kind-appropriate check on a workspace file."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    if not target.is_file():
        return f"Error: no such file: {path}"
    from seymour.tools import verify
    check = await verify.auto_check(paths.display(target))
    if check is None:
        return (f"No verifier for {target.suffix or 'this file'} (verify_file checks .xlsx, .pptx, .docx, "
                ".html, .py, .js, .json). Read it back yourself.")
    report = check["report"]
    if check.get("images"):
        report += "\nimages: " + ", ".join(paths.display(paths.resolve(i)) if i.startswith("/") else i for i in check["images"][:8])
        report += "\n(read_image one of them if the loaded model has vision.)"
    return report


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="verify_file",
        description=("Verify a deliverable the way the harness does: an .xlsx is RECALCULATED by LibreOffice "
                     "and its formula values read back (errors and empty formulas named); a .pptx is checked "
                     "for empty placeholders and overflow and rendered to PNGs; a .docx is loaded and "
                     "rendered; an .html is loaded in a browser. Call it before you say a file is done — "
                     "do not run soffice yourself (it cannot run inside your sandbox)."),
        args={"path": "workspace-relative path of the file"},
        tier="read", func=verify_file,
    ),
]
