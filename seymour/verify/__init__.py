"""seymour.verify — the harness checks what the model made.

Verification is Seymour's actual differentiator (the brief, 1.3): a
skill can ASK the model to verify; the harness DOES it, after every
file write, and refuses to end a run on a page that still fails. Until
2026-09-11 that covered pages (check_page), Python, JavaScript and
JSON. This package finishes it:

    office.py   spreadsheets are verified by RECALCULATING them through
                LibreOffice and reading the real values back (a formula
                that produces #REF! or nothing fails); decks and
                documents are RENDERED (soffice → PDF → PNG per page
                with PyMuPDF, plus a contact sheet) and checked
                mechanically — empty placeholders, text that cannot fit
                its box, slide count — with the PNGs saved where a
                vision model or the eval judge can look at them.
    web.py      pages are exercised through Playwright: click / type /
                drag the things a prompt named and assert the DOM
                changed. "The button exists" is not "the button works".

Every check returns the same shape — {"kind", "verdict": PASS | FIX
NEEDED | not measured, "report", "images": [...]} — and its report is
appended to the tool result the model reads next (tools/verify.py). A
check that cannot run says "not measured"; it never claims PASS.

The rendering functions here are also what `evals/render/` calls, so a
deliverable renders the same way for the model, the person and the
judge.
"""
