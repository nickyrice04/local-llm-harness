"""Document text extraction: attached files → text the model can read.

One function per format, one dispatcher, everything bounded. The formats
are the ones a person actually attaches: pdf, docx, md, txt, csv, and
code/config files (which are just text). Anything else is refused with a
clear message rather than half-parsed.

docx is read with the standard library on purpose: a .docx is a zip of
XML, and pulling the text out of word/document.xml is ~15 lines — a
dependency would be heavier than the code it saved.
"""

import csv
import io
import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# The cap on extracted text per document. Beyond this the model wouldn't
# fit it in context anyway; the truncation is ANNOUNCED in the text.
MAX_DOC_CHARS = 24_000

# Plain-text extensions we pass through as-is (code and config included —
# "summarize this document" applies to a .py file too).
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql",
}


def extractable(filename: str) -> bool:
    """Can extract_text() handle this file? (The upload route asks first.)"""
    suffix = Path(filename).suffix.lower()
    return suffix in _TEXT_EXTENSIONS or suffix in (".pdf", ".docx", ".csv")


def extract_text(path: Path, original_name: str) -> str:
    """Extract readable text from one file, bounded and annotated.

    Never raises: a failed parse returns an error STRING — the text goes
    into the chat context either way, and "[could not read X]" is more
    useful to the model (and honest to the user) than a 500.
    """
    suffix = Path(original_name).suffix.lower()
    try:
        if suffix == ".pdf":
            text = _pdf_text(path)
        elif suffix == ".docx":
            text = _docx_text(path)
        elif suffix == ".csv":
            text = _csv_text(path)
        else:
            # Everything else in the allowlist is plain text.
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as error:
        logger.exception("extraction failed for %s", original_name)
        return f"[could not read {original_name}: {error}]"
    text = text.strip()
    if not text:
        return f"[{original_name} contained no extractable text]"
    if len(text) > MAX_DOC_CHARS:
        # Cut at a line boundary near the cap, and SAY that we cut —
        # silent truncation reads as "that was the whole document".
        cut = text.rfind("\n", 0, MAX_DOC_CHARS)
        cut = cut if cut > MAX_DOC_CHARS * 0.8 else MAX_DOC_CHARS
        text = (text[:cut]
                + f"\n[truncated at {cut} characters — the document continues]")
    return text


def _pdf_text(path: Path) -> str:
    """PDF → text via pypdf, page markers preserved for citability."""
    from pypdf import PdfReader          # imported lazily; pdf-only cost
    reader = PdfReader(str(path))
    pages: list[str] = []
    for number, page in enumerate(reader.pages[:200], start=1):  # bounded
        pages.append(f"[page {number}]\n{page.extract_text() or ''}")
    return "\n\n".join(pages)


def _docx_text(path: Path) -> str:
    """DOCX → text: unzip, read word/document.xml, keep paragraph breaks.

    Word stores each paragraph as a <w:p> and each text run as <w:t>;
    turning </w:p> into newlines before stripping tags preserves the
    document's paragraph structure without an XML library.
    """
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)          # paragraph ends → newlines
    xml = re.sub(r"<[^>]+>", "", xml)           # drop every tag
    return re.sub(r"\n{3,}", "\n\n", xml)       # tidy blank runs


def _csv_text(path: Path) -> str:
    """CSV → a readable table: header + rows, pipe-separated, bounded.

    The model reads "colA | colB" rows far more reliably than raw commas
    with quoting rules; 400 rows is plenty for "summarize this sheet".
    """
    out = io.StringIO()
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        for number, row in enumerate(csv.reader(f)):
            if number > 400:
                out.write(f"[{number}+ rows — table truncated]\n")
                break
            out.write(" | ".join(cell.strip() for cell in row) + "\n")
    return out.getvalue()
