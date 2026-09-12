"""glob and read_structure — finding files, and reading a file's SHAPE.

glob: `list_files` + `grep` is not a file finder. A pattern over the
workspace (`**/*.py`, `src/**/test_*.ts`), newest first — dsh's
tool-fs-search. Trivial, used constantly.

read_structure: a 1,500-line module should read as its declarations
first — imports, classes, functions, top-level assignments, each with
its line number — so the model reads only the ranges it needs with
read_file offset/limit (ROADMAP #5). This is the regex-level version
for Python, JavaScript/TypeScript, Markdown (headings), HTML (ids,
script/style blocks) and CSS (selectors); tree-sitter only if this
proves insufficient.
"""

import fnmatch
import re
from pathlib import Path

from seymour.tools import paths
from seymour.tools.files import _read_text, _walk, compute_tag

GLOB_MAX = 200
STRUCTURE_MAX_LINES = 400

# Declaration patterns per language: (regex, kind). Bodies are elided by
# construction — only the matching line is shown.
_PY = [
    (re.compile(r"^(?:from\s+\S+\s+)?import\s+"), "import"),
    (re.compile(r"^\s*(?:async\s+)?def\s+\w+"), "def"),
    (re.compile(r"^\s*class\s+\w+"), "class"),
    (re.compile(r"^[A-Z_][A-Z0-9_]*\s*(?::\s*\S+)?\s*="), "const"),
    (re.compile(r"^\s*@\w"), "decorator"),
]
_JS = [
    (re.compile(r"^\s*(?:import\s|export\s+\*|export\s+\{|const\s+\w+\s*=\s*require\()"), "import"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*\w*"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+\w+"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"), "arrow fn"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Z_][A-Z0-9_]*\s*="), "const"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+\w+"), "type"),
    (re.compile(r"^\s{2,}(?:(?:public|private|protected|static|async|get|set)\s+)*\w+\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$"), "method"),
]
_MD = [(re.compile(r"^#{1,6}\s"), "heading")]
_HTML = [
    (re.compile(r"<(?:div|section|canvas|button|input|form|table|nav|main|header|footer|aside|svg|video|audio)\b[^>]*\bid=\"[^\"]+\""), "id"),
    (re.compile(r"^\s*<script\b|^\s*</script>|^\s*<style\b|^\s*</style>"), "block"),
    (re.compile(r"^\s*(?:async\s+)?function\s+\w+|^\s*class\s+\w+|^\s*(?:const|let|var)\s+\w+\s*=\s*(?:\([^)]*\)|\w+)\s*=>"), "js"),
]
_CSS = [(re.compile(r"^[^\s{][^{]*\{\s*$"), "rule"), (re.compile(r"^@(?:media|container|keyframes|font-face)"), "at-rule")]
_BY_SUFFIX = {".py": _PY, ".js": _JS, ".mjs": _JS, ".ts": _JS, ".tsx": _JS, ".jsx": _JS,
              ".md": _MD, ".html": _HTML, ".htm": _HTML, ".css": _CSS}


def outline(text: str, suffix: str) -> list[tuple[int, str, str]]:
    """(line number, kind, the line) for every declaration-shaped line."""
    patterns = _BY_SUFFIX.get(suffix.lower())
    if not patterns:
        return []
    out: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.split("\n"), 1):
        for regex, kind in patterns:
            if regex.search(line):
                out.append((number, kind, line.rstrip()[:160]))
                break
    return out


async def read_structure(path: str) -> str:
    """Tool entry: the file's declarations with line numbers, bodies elided."""
    try:
        target = paths.resolve(path)
        text = _read_text(target)
    except ValueError as error:
        return f"Error: {error}"
    rel = paths.display(target)
    tag = compute_tag(text)
    total = text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0
    rows = outline(text, target.suffix)
    if not rows:
        kinds = ", ".join(sorted(_BY_SUFFIX))
        return (f"[{rel}#{tag}] {total} lines — no structure recognised for {target.suffix or 'this file'} "
                f"(read_structure understands {kinds}). Use read_file.")
    shown = rows[:STRUCTURE_MAX_LINES]
    body = [f"{n}:{line}" for n, _kind, line in shown]
    footer = (f"({len(rows)} declarations in {total} lines; bodies elided — read_file with "
              f"offset=<line> limit=<n> to read a region. Line numbers and TAG are valid for edit_lines.)")
    if len(rows) > STRUCTURE_MAX_LINES:
        footer = f"(Showing {STRUCTURE_MAX_LINES} of {len(rows)} declarations.) " + footer
    return "\n".join([f"[{rel}#{tag}] structure", *body, footer])


async def glob(pattern: str, path: str = "") -> str:
    """Tool entry: files matching a glob, newest first."""
    pattern = (pattern or "").strip()
    if not pattern:
        return "Error: glob needs a pattern, e.g. **/*.py or src/*.ts"
    try:
        root = paths.resolve(path, allow_root=True)
    except ValueError as error:
        return f"Error: {error}"
    if not root.is_dir():
        return f"Error: no such folder: {paths.display(root)}"
    # `**/x` should also match a top-level x; fnmatch does not treat `**`
    # specially, so both spellings are tried against the relative path.
    alternatives = {pattern}
    if pattern.startswith("**/"):
        alternatives.add(pattern[3:])
    hits: list[tuple[float, str]] = []
    total = 0
    for file in _walk(root):
        rel = paths.display(file)
        inner = str(file.relative_to(root)) if root != paths.workspace() else rel
        if not any(fnmatch.fnmatch(inner, alt) or fnmatch.fnmatch(rel, alt) or fnmatch.fnmatch(file.name, alt)
                   for alt in alternatives):
            continue
        total += 1
        try:
            hits.append((file.stat().st_mtime, rel))
        except OSError:
            continue
    if not hits:
        return f"No files match {pattern!r}" + (f" under {paths.display(root)}" if path else "") + "."
    hits.sort(reverse=True)
    rows = [rel for _mtime, rel in hits[:GLOB_MAX]]
    header = f"{total} file(s) match {pattern!r} (newest first)"
    if total > GLOB_MAX:
        header += f" — showing {GLOB_MAX}"
    return "\n".join([header + ":", *rows])


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="glob",
        description=("Find files by glob pattern, newest first — e.g. **/*.py, src/**/test_*.ts, "
                     "*.csv. The file finder; use grep to search contents."),
        args={"pattern": "the glob (** matches folders)",
              "path": "folder to search under (default: whole workspace)"},
        optional=frozenset({"path"}),
        tier="read", func=glob,
    ),
    Tool(
        name="read_structure",
        description=("Read a file's SHAPE: its imports, classes, functions, top-level constants "
                     "(or headings / ids / rules for md, html, css) with line numbers, bodies "
                     "elided. Use it first on any file over ~200 lines, then read_file the "
                     "ranges you need with offset/limit."),
        args={"path": "workspace-relative path"},
        tier="read", func=read_structure,
    ),
]
